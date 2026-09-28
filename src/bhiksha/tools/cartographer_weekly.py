"""Existing morning owner: verified weekly import and Bhiksha-owned Sheet status."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3

from bhiksha.active_plan.compiler import load_operator_defaults_sheet_rows
from bhiksha.config.environment import load_dotenv
from bhiksha.integrations.cartographer_weekly import ROOT, controls, import_publication, atomic_json, execution_plans
from bhiksha.integrations.google_sheets import GoogleSheetTableClient

TAB = 'Cartographer_Status'
HEADERS = ['Symbol', 'State', 'Reason / last outcome', 'Entry condition', 'Tactical invalidation',
           'Valid through UTC', 'Author profile', 'Effective primary exit', 'Configured mode',
           'Loaded mode', 'Last evaluation UTC', 'Confirmation UTC', 'Trade ID', 'Scenario / branch',
           'Publication hash', 'Admitted UTC', 'As of UTC', 'Entry arm', 'Author confirmation']


def _condition(c):
    return f"{c['count']} × {c['timeframe']} {c['rule']} {c['price']}" if c else ''


def status_rows(admissions, policy, *, db_path='bhiksha.db', active_plan_path='artifacts/playbook/active_plan.json'):
    now = datetime.now(UTC).isoformat()
    loaded = {}
    states = {}
    outcomes = {}
    path = Path(db_path)
    if path.exists():
        with sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'weekly_chart_state' in tables:
                states = {r['deployment_id']: dict(r) for r in db.execute('SELECT * FROM weekly_chart_state')}
            if 'events' in tables:
                startup = db.execute("SELECT payload FROM events WHERE event_type='startup_config' ORDER BY id DESC LIMIT 1").fetchone()
                if startup:
                    loaded = {d['deployment_id']: d for d in json.loads(startup[0]).get('deployments', [])}
                for row in db.execute("SELECT payload FROM events WHERE event_type='signal_outcome' AND created_at>=? ORDER BY id DESC LIMIT 2000", (admissions.get('checked_at', now)[:10],)):
                    data = json.loads(row[0])
                    if str(data.get('deployment_id', '')).startswith('cw-'):
                        outcomes.setdefault(data['deployment_id'], data)
    rows = []
    for p in [*execution_plans(admissions), *admissions.get('unsupported', [])]:
        deployment_id = p.get('deployment_id', '')
        state = states.get(deployment_id, {})
        observed = json.loads(state.get('payload', '{}'))
        runtime = loaded.get(deployment_id, {})
        runtime_params = runtime.get('strategy', {}).get('params', {})
        outcome = outcomes.get(deployment_id, {})
        reason = p.get('admission_block') or p.get('reason') or ', '.join(outcome.get('rejection_reasons', [])) or outcome.get('outcome') or observed.get('reason') or 'Waiting for runtime evaluation'
        configured_exit = policy.get(f"exit_{str(p.get('author_profile', '')).lower()}", '')
        rows.append([p.get('symbol', ''), state.get('status', 'unsupported' if p.get('reason') else 'admitted'),
            reason, _condition(p.get('trigger')), _condition(p.get('tactical_invalidation')), p.get('valid_through', ''),
            p.get('author_profile', ''), runtime.get('exit', {}).get('management_exit') or configured_exit,
            policy['mode'], runtime_params.get('controls', {}).get('mode', 'not loaded'), observed.get('last_evaluation', ''),
            observed.get('confirmation_at', ''), state.get('trade_id') or '',
            f"{p.get('pack_id', '')}/{p.get('scenario_id', '')}/{p.get('branch_id', '')}", p.get('publication_hash', ''), p.get('admitted_at', ''), now, p.get('entry_arm', ''), _condition(p.get('author_trigger'))])
    return rows


def publish_status(*, root=ROOT, db_path='bhiksha.db'):
    load_dotenv()
    client = GoogleSheetTableClient(os.environ['GOOGLE_SHEET_ID'], 'Operator_Defaults_v1', Path(os.environ['GOOGLE_API_CREDENTIALS_PATH']))
    policy = controls(load_operator_defaults_sheet_rows(client.read_rows()))
    admissions = json.loads((root/'admissions.json').read_text()) if (root/'admissions.json').exists() else {}
    health = json.loads((root/'source_health.json').read_text()) if (root/'source_health.json').exists() else {'ok': False}
    rows = status_rows(admissions, policy, db_path=db_path)
    if not health.get('ok'):
        rows.insert(0, ['', 'source blocked', health.get('reason', 'Source health unavailable'), *(['']*13), datetime.now(UTC).isoformat(), '', ''])
    api = client.service.spreadsheets()
    meta = api.get(spreadsheetId=client.spreadsheet_id, fields='sheets.properties').execute()
    props = next((s['properties'] for s in meta['sheets'] if s['properties']['title']==TAB), None)
    if props is None:
        reply = api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={'requests':[{'addSheet':{'properties':{'title':TAB, 'gridProperties':{'rowCount':max(100,len(rows)+1), 'columnCount':len(HEADERS), 'frozenRowCount':1, 'frozenColumnCount':1, 'hideGridlines':True}}}}]}).execute()
        props = reply['replies'][0]['addSheet']['properties']
    sheet_id = props['sheetId']
    values = [HEADERS, *rows]
    # Own this generated tab only. Clear old generated cells in the same atomic update.
    grid = {'sheetId':sheet_id, 'startRowIndex':0, 'endRowIndex':max(len(values),props['gridProperties']['rowCount']), 'startColumnIndex':0,'endColumnIndex':len(HEADERS)}
    requests = []
    if len(HEADERS)>props['gridProperties']['columnCount']:
        requests.append({'appendDimension':{'sheetId':sheet_id,'dimension':'COLUMNS','length':len(HEADERS)-props['gridProperties']['columnCount']}})
    if len(values)>props['gridProperties']['rowCount']:
        requests.append({'appendDimension':{'sheetId':sheet_id,'dimension':'ROWS','length':len(values)-props['gridProperties']['rowCount']}})
    requests += [{'updateCells':{'range':grid, 'rows':[{'values':[{'userEnteredValue':{'stringValue':str(v)}} for v in row]} for row in values], 'fields':'userEnteredValue'}},
        {'repeatCell':{'range':{**grid,'endRowIndex':len(values)}, 'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP'}},'fields':'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
        {'repeatCell':{'range':{**grid,'endRowIndex':1},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.08,'green':.19,'blue':.36},'textFormat':{'bold':True,'foregroundColor':{'red':1,'green':1,'blue':1}}}},'fields':'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat'}},
        {'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':0,'endIndex':len(HEADERS)},'properties':{'pixelSize':180},'fields':'pixelSize'}},
        {'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':2,'endIndex':5},'properties':{'pixelSize':260},'fields':'pixelSize'}}]
    api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={'requests':requests}).execute()
    receipt={'status':'ok','tab':TAB,'rows':len(rows),'published_at':datetime.now(UTC).isoformat(),'configured_mode':policy['mode']}
    atomic_json(root/'sheet_receipt.json', receipt)
    return receipt


def publish_status_best_effort(**kwargs):
    try:
        return publish_status(**kwargs)
    except Exception as exc:
        receipt = {'status':'failed','error_type':type(exc).__name__,'attempted_at':datetime.now(UTC).isoformat()}
        atomic_json(ROOT/'sheet_last_attempt.json',receipt)
        return receipt


def main():
    load_dotenv()
    pointer=Path(os.environ.get('CARTOGRAPHER_WEEKLY_POINTER','/Users/sunny/Documents/market-cartographer/artifacts/weekly/latest.json'))
    try:
        client = GoogleSheetTableClient(os.environ['GOOGLE_SHEET_ID'], 'Operator_Defaults_v1', Path(os.environ['GOOGLE_API_CREDENTIALS_PATH']))
        policy = controls(load_operator_defaults_sheet_rows(client.read_rows()))
        admitted = import_publication(pointer, policy=policy)
    except Exception as exc:
        atomic_json(ROOT/'source_health.json', {'ok':False,'checked_at':datetime.now(UTC).isoformat(),'reason':f'weekly_import_failed:{type(exc).__name__}'})
        print(json.dumps({'status':'failed','reason':type(exc).__name__,'sheet':publish_status_best_effort()}))
        return 2
    sheet=publish_status_best_effort()
    print(json.dumps({'status':'ok' if sheet['status']=='ok' else 'failed','admitted':len(admitted['plans']),'unsupported':len(admitted['unsupported']),'sheet':sheet}))
    return 0 if sheet['status']=='ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())

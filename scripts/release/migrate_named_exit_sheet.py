"""One-time named-exit migration. Dry run by default; only --apply writes Sheets.

Run from the runtime checkout with its existing environment. Never submits orders.
All content changes are one Sheets batch; preimages and exact requests are saved.
"""
import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.config.exit_catalog import load_exit_profiles_sheet_rows
from bhiksha.integrations.google_sheets import GoogleSheetTableClient

PROFILE_ADDITIONS = {
    'dynamic_envelope_curv15': {'risk_envelope_activation_r': .5, 'risk_envelope_initial_floor_r': -1,
                              'risk_envelope_floor_at_t1_r': 0, 'risk_envelope_ratchet_step_r': .1},
    'profit_preservation_v1': {'profit_lock_arm_r': .75, 'profit_lock_floor_r': .25},
}
COMPARISONS = 'trend_continuation_balanced,flash_reversal_fast_snap,exhaustion_reversal_climax,range_expansion_swing,dynamic_envelope_curv15,profit_preservation_v1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    load_dotenv()
    tabs = ['Exit_Profiles_v1', 'manual_entry', 'Operator_Defaults_v1']
    clients = {name: GoogleSheetTableClient(spreadsheet_id=os.environ['GOOGLE_SHEET_ID'], sheet_name=name,
        credentials_path=Path(os.environ['GOOGLE_API_CREDENTIALS_PATH'])) for name in tabs}
    first = clients[tabs[0]]
    service = first.service.spreadsheets()
    metadata = service.get(spreadsheetId=first.spreadsheet_id,
                          fields='sheets.properties').execute()
    props = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    before = {k: c.read_rows() for k, c in clients.items()}
    headers = {k: c.read_headers() for k, c in clients.items()}
    requests = []
    cells = []
    for tab, client in clients.items():
        grid = service.get(spreadsheetId=first.spreadsheet_id, ranges=[f"'{client.sheet_name}'!A1:AZ60"],
            includeGridData=True, fields='sheets.data.rowData.values(userEnteredValue,dataValidation,userEnteredFormat)').execute()
        # Exact target metadata is retained; formulas/validation on overwritten
        # targets are checked below. Existing surrounding cells stay untouched.
        cells.append({'tab': tab, 'grid': grid})
    def edit(tab, row, key, value):
        h = headers[tab]
        if key not in h:
            h.append(key)
            edit(tab, 1, key, key)
        col = h.index(key)
        grid = cells[tabs.index(tab)]['grid']['sheets'][0].get('data', [{}])[0].get('rowData', [])
        old = grid[row-1].get('values', []) if row <= len(grid) else []
        old = old[col] if col < len(old) else {}
        if 'formulaValue' in old.get('userEnteredValue', {}) or old.get('dataValidation'):
            raise ValueError(f'{tab} row {row} {key}: target has formula/validation; migration cannot overwrite it')
        val = {'numberValue': value} if isinstance(value, (int, float)) else {'stringValue': str(value)}
        requests.append({'updateCells': {'range': {'sheetId': props[clients[tab].sheet_name]['sheetId'],
            'startRowIndex': row-1, 'endRowIndex': row, 'startColumnIndex': col, 'endColumnIndex': col+1},
            'rows': [{'values': [{'userEnteredValue': val}]}], 'fields':'userEnteredValue'}})
    proposed_profiles = []
    for row in before['Exit_Profiles_v1']:
        addition = PROFILE_ADDITIONS.get(row.get('exit_profile_id'), {})
        for key, value in addition.items():
            if str(row.get(key) or '').strip() and float(row[key]) != value:
                raise ValueError(f'Concurrent/customized profile setting: {row["exit_profile_id"]} {key}')
            if str(row.get(key) or '').strip() == '':
                edit('Exit_Profiles_v1', row['row_index'], key, value)
        proposed_profiles.append({**row, **addition})
    catalog = load_exit_profiles_sheet_rows(proposed_profiles)
    for required in COMPARISONS.split(','):
        if required not in catalog:
            raise ValueError(f'Missing comparison profile: {required}')
    for row in before['manual_entry']:
        if str(row.get('enabled')).lower() not in {'false', ''}:
            raise ValueError('Manual rows became enabled; do not migrate an armed trigger implicitly')
        if str(row.get('management_exit') or '').strip():
            continue  # Preserve a later operator assignment on an idempotent retry.
        edit('manual_entry', row['row_index'], 'management_exit', 'trend_continuation_balanced')
        edit('manual_entry', row['row_index'], 'compare_exits', COMPARISONS)
        edit('manual_entry', row['row_index'], 'management_policy_spec', '')
    defaults = before['Operator_Defaults_v1']
    next_row = max(r['row_index'] for r in defaults) + 1
    for key, value in [('management_exit', 'trend_continuation_balanced'), ('compare_exits', COMPARISONS)]:
        existing = [r for r in defaults if r.get('section') == 'profile__trend_continuation' and r.get('key') == key]
        if existing:
            if len(existing) != 1 or existing[0].get('value') != value:
                raise ValueError(f'Existing customized Cartographer default: {key}')
            continue
        for name, item in {'section':'profile__trend_continuation', 'key':key, 'value':value,
                           'source':'entry_exit_cutover_2026-09-20', 'what it controls':'Named exits copied into new Cartographer manual rows'}.items():
            edit('Operator_Defaults_v1', next_row, name, item)
        next_row += 1
    extensions=[]
    for tab, h in headers.items():
        prop = props[clients[tab].sheet_name]
        required = len(h) - prop['gridProperties']['columnCount']
        if required > 0:
            extensions.append({'appendDimension':{'sheetId':prop['sheetId'],'dimension':'COLUMNS','length':required}})
    requests = extensions + requests
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    receipt = {'as_of':stamp,'applied':False,'before':before,'cell_metadata':cells,'requests':requests}
    path = output / f'sheet-migration-{stamp}.json'
    path.write_text(json.dumps(receipt, indent=2)+'\n')
    if args.apply and requests:
        # Recheck row contents immediately before the atomic content batch.
        if any(c.read_rows() != before[k] for k,c in clients.items()):
            raise ValueError('Sheet changed during preview; no write performed')
        service.batchUpdate(spreadsheetId=first.spreadsheet_id, body={'requests':requests}).execute()
        receipt['applied'] = True
    after = {k:c.read_rows() for k,c in clients.items()}
    if args.apply:
        resolved = load_exit_profiles_sheet_rows(after['Exit_Profiles_v1'])
        for name, changes in PROFILE_ADDITIONS.items():
            for key, value in changes.items():
                assert getattr(resolved[name], key) == value
        for row in after['manual_entry']:
            assert str(row.get('enabled')).lower() == 'false'
            assert row.get('management_exit') and not row.get('management_policy_spec')
        for key in ['management_exit','compare_exits']:
            assert len([r for r in after['Operator_Defaults_v1'] if r.get('section')=='profile__trend_continuation' and r.get('key')==key]) == 1
    receipt['after'] = after
    receipt['verified'] = bool(args.apply)
    path.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({'applied':receipt['applied'], 'verified':receipt['verified'], 'request_count':len(requests), 'receipt':str(path)}))


if __name__ == '__main__':
    main()

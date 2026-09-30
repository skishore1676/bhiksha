"""Read-only status for the weekly intake used by the morning launchd owner.

Daily alpha receipts remain historical evidence, not this owner's health input.
Admission freshness follows the existing registry schedule and session boundary.
"""
from datetime import UTC, datetime
from pathlib import Path

from bhiksha.integrations.cartographer_weekly import ROOT, OWNER, execution_plans, stamp
from bhiksha.market_data.trading_calendar import CENTRAL
from bhiksha.tools.cartographer_evidence_status import _read, _projection_session, _after_compile_deadline


def build_status(*, repo_root: Path, active_plan_path: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    root = repo_root / ROOT
    health = _read(root / 'source_health.json') or {}
    admissions = _read(root / 'admissions.json') or {}
    sheet = _read(root / 'sheet_receipt.json') or {}
    attempt = _read(root / 'sheet_last_attempt.json') or {}
    plan = _read(active_plan_path) or {}
    expected, compiled = {}, {}
    try:
        expected = {p['deployment_id']: p for p in execution_plans(admissions)}
        compiled = {d['deployment_id']: (d.get('source', {}).get('metadata', {}).get('weekly_plan') or {})
                    for d in plan.get('deployments', [])
                    if d.get('source', {}).get('metadata', {}).get('source_owner') == OWNER}
        checked = stamp(health['checked_at'])
        day = checked.astimezone(CENTRAL).date().isoformat()
        session = _projection_session(day, now=now) if checked <= now else 'invalid'
        source = admissions['source']
        source_ok = (health.get('ok') is True
                     and admissions.get('schema') == 'bhiksha.weekly_admissions.v1'
                     and admissions.get('checked_at') == health['checked_at']
                     and all(source.get(k) and source[k] == health.get(k)
                             for k in ('publication_hash', 'run_id', 'published_at')))
        sheet_ok = (sheet.get('status') == 'ok' and stamp(sheet['published_at']) >= checked
                    and stamp(sheet['published_at']) <= now
                    and (not attempt or stamp(attempt['attempted_at']) <= stamp(sheet['published_at'])))
    except (ValueError, KeyError, TypeError, AttributeError):
        day, session, source_ok, sheet_ok = '', 'invalid', False, False
    matched = (set(expected) == set(compiled)
               and all(compiled[k].get('publication_hash') == p.get('publication_hash')
                       and compiled[k].get('admitted_at') == p.get('admitted_at')
                       for k, p in expected.items())
               and plan.get('trading_date') == day)
    if not source_ok:
        status, reason = 'blocked', health.get('reason') or 'weekly_source_missing_or_mismatched'
    elif not sheet_ok:
        status, reason = 'blocked', 'weekly_status_sheet_failed_or_missing'
    elif session in {'overdue', 'invalid'}:
        status, reason = 'blocked', 'weekly_intake_' + session
    elif session == 'awaiting_session':
        status, reason = 'awaiting_session', 'Awaiting the next scheduled weekly intake; prior-session evidence retained.'
    elif matched:
        status, reason = 'healthy', 'weekly_admissions_and_compiled_plan_matched'
    elif not _after_compile_deadline(day, now=now):
        status, reason = 'compile_pending', 'weekly_compile_pending'
    else:
        status, reason = 'blocked', 'weekly_compiled_plan_mismatch'
    return {'schema': 'bhiksha.cartographer_weekly_status.v1', 'status': status,
            'ok': status != 'blocked', 'attention_required': status == 'blocked',
            'reason': reason, 'updated_at': health.get('checked_at'),
            'producer': health or {'status': 'missing'},
            'projection': sheet or {'status': 'missing'},
            'compile': {'status': 'matched' if matched else 'unavailable_or_mismatched',
                        'expected_count': len(expected), 'compiled_count': len(compiled)},
            'blocked_deployments': health.get('blocked_deployments', [])}

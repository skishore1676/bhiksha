import json
from datetime import datetime

import pytest

from bhiksha.tools import launchd_status
from bhiksha.tools.cartographer_weekly_status import build_status


def fixture(root, day='2026-09-30'):
    path = root / 'artifacts/cartographer-weekly'
    path.mkdir(parents=True)
    checked = day + 'T12:40:00+00:00'
    source = {'publication_hash': 'sha256:weekly', 'run_id': 'weekly:1',
              'published_at': '2026-09-27T12:00:00+00:00'}
    plan = {'deployment_id': 'cw-one', 'publication_hash': source['publication_hash'],
            'admitted_at': '2026-09-28T12:40:00+00:00', 'trigger': {'timeframe': '39m'}}
    data = {'source_health.json': {'ok': True, 'checked_at': checked, **source},
            'admissions.json': {'schema': 'bhiksha.weekly_admissions.v1', 'checked_at': checked,
                                'source': source, 'plans': [plan], 'unsupported': []},
            'sheet_receipt.json': {'status': 'ok', 'published_at': day+'T12:41:00+00:00'}}
    for name, body in data.items():
        (path / name).write_text(json.dumps(body))
    active = root / 'active_plan.json'
    active.write_text(json.dumps({'trading_date': day, 'deployments': [
        {'deployment_id': 'cw-one', 'source': {'metadata': {'source_owner': 'cartographer_weekly',
                                                          'weekly_plan': plan}}}]}))
    return path, active


def status(root, active, when='2026-09-30T14:00:00+00:00'):
    return build_status(repo_root=root, active_plan_path=active, now=datetime.fromisoformat(when))


def test_current_weekly_receipts_not_retired_daily_alpha(tmp_path):
    _, active = fixture(tmp_path)
    target = tmp_path / 'artifacts/playbook/active_plan.json'
    target.parent.mkdir(parents=True)
    target.write_bytes(active.read_bytes())
    result = launchd_status._cartographer_semantic_status(tmp_path, now=datetime.fromisoformat('2026-09-30T14:00:00+00:00'))
    assert result['schema'] == 'bhiksha.cartographer_weekly_status.v1'
    assert result['status'] == 'healthy'
    assert result['compile']['expected_count'] == 1


@pytest.mark.parametrize('when,expected', [
    ('2026-10-01T11:30:00+00:00', 'awaiting_session'),
    ('2026-10-01T12:44:00+00:00', 'awaiting_session'),
    ('2026-10-01T12:46:00+00:00', 'blocked'),
])
def test_weekly_intake_uses_existing_retry_deadline(tmp_path, when, expected):
    _, active = fixture(tmp_path)
    assert status(tmp_path, active, when)['status'] == expected


@pytest.mark.parametrize('failure', ['source_failed', 'identity', 'sheet', 'later_sheet_failure', 'plan', 'future'])
def test_actual_failures_remain_visible(tmp_path, failure):
    root, active = fixture(tmp_path)
    if failure == 'plan':
        active.write_text(json.dumps({'trading_date': '2026-09-30', 'deployments': []}))
    elif failure == 'later_sheet_failure':
        (root / 'sheet_last_attempt.json').write_text(json.dumps({'status': 'failed', 'attempted_at': '2026-09-30T13:00:00+00:00'}))
    else:
        name = 'sheet_receipt.json' if failure == 'sheet' else 'source_health.json'
        body = json.loads((root / name).read_text())
        body.update({'source_failed': {'ok': False}, 'identity': {'run_id': 'other'},
                     'sheet': {'status': 'failed'}, 'future': {'checked_at': '2026-10-01T12:40:00+00:00'}}[failure])
        (root / name).write_text(json.dumps(body))
    assert status(tmp_path, active)['attention_required'] is True


def test_compile_pending_only_before_compile_deadline(tmp_path):
    _, active = fixture(tmp_path)
    active.write_text(json.dumps({'deployments': []}))
    assert status(tmp_path, active, '2026-09-30T13:00:00+00:00')['status'] == 'compile_pending'
    assert status(tmp_path, active)['status'] == 'blocked'

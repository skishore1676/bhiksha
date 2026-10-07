"""Prospective weekly close confirmations; durable admission and scenario ownership."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import exchange_calendars

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.integrations.cartographer_weekly import stamp

ET = ZoneInfo('America/New_York')
_RECOVERED = set()


@lru_cache(maxsize=512)
def session(day):
    calendar = exchange_calendars.get_calendar('XNYS')
    if not calendar.is_session(day):
        return None
    return (calendar.session_open(day).to_pydatetime(), calendar.session_close(day).to_pydatetime())


@contextmanager
def state(params):
    db = sqlite3.connect(params['state_db'], timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS weekly_chart_state (
            deployment_id TEXT PRIMARY KEY, scenario_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting', payload TEXT NOT NULL DEFAULT '{}',
            trade_id TEXT, frozen_deployment TEXT)''')
        db.execute('BEGIN IMMEDIATE')
        if 'entry_arm' not in {r[1] for r in db.execute('PRAGMA table_info(weekly_chart_state)')}:
            db.execute("ALTER TABLE weekly_chart_state ADD COLUMN entry_arm TEXT NOT NULL DEFAULT 'baseline'")
        db.execute('INSERT OR IGNORE INTO weekly_chart_state(deployment_id,scenario_key,entry_arm) VALUES (?,?,?)',
                   (params['deployment_id'], params['scenario_key'], params.get('entry_arm', 'baseline')))
        identity = (str(Path(params['state_db']).resolve()), params['deployment_id'])
        if identity not in _RECOVERED:
            row = db.execute('SELECT status,payload FROM weekly_chart_state WHERE deployment_id=?', (params['deployment_id'],)).fetchone()
            if row['status'] == 'pending':
                # Paper intents disappear with their process. The durable fill ledger wins
                # over a crash between recording the fill and consuming the scenario.
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                fill = db.execute("SELECT trade_id FROM trade_sessions WHERE deployment_id=? AND entry_order_id='SHADOW_ENTRY' AND entry_price>0 AND quantity>0 LIMIT 1", (params['deployment_id'],)).fetchone() if 'trade_sessions' in tables else None
                data = json.loads(row['payload'])
                data['recovery'] = 'filled_ledger_reconciled' if fill else 'paper_pending_cancelled_on_restart'
                db.execute('UPDATE weekly_chart_state SET status=?,trade_id=?,payload=? WHERE deployment_id=?', ('filled' if fill else 'waiting', fill[0] if fill else None, json.dumps(data), params['deployment_id']))
            _RECOVERED.add(identity)
        yield db
        db.commit()
    finally:
        db.close()


def source_block(params, now):
    if params['controls']['mode'] != 'SHADOW':
        return 'weekly_operator_off'
    if params.get('entry_arm') == 'early_1m' and params['controls'].get('entry_timing_comparison', 'OFF') != 'PAIRED':
        return 'weekly_early_arm_off'
    if params.get('admission_block'):
        return params['admission_block']
    if now >= stamp(params['valid_through']):
        return 'weekly_expired'
    try:
        health = json.loads(Path(params['source_health_path']).read_text())
        age = (now - stamp(health['checked_at'])).total_seconds()
        if not health['ok'] or not 0 <= age <= 24 * 3600:
            return 'weekly_source_unhealthy_or_stale'
        if params.get('base_deployment_id', params['deployment_id']) in health.get('blocked_deployments', []):
            return 'weekly_source_revision_requires_readmission'
    except (OSError, ValueError, KeyError, TypeError):
        return 'weekly_source_health_unavailable'
    return None


def completed(frame, condition, now, boundary=None, warnings=None):
    """Close rules require the actual closing minute at calendar-aligned bar end."""
    minutes = {}
    for row in frame.select('timestamp', 'close').iter_rows(named=True):
        t = row['timestamp'].astimezone(UTC)
        price = float(row['close'])
        if t.second == 0 and t.microsecond == 0 and math.isfinite(price) and price > 0:
            minutes[t] = price
    bars = []
    first = boundary or min(minutes, default=now)
    day = first.astimezone(ET).date()
    days = []
    while day <= now.astimezone(ET).date():
        days.append(day.isoformat())
        day += timedelta(days=1)
    for day in days:
        bounds = session(day)
        if not bounds:
            continue
        opening, closing = bounds
        width = int((closing-opening).total_seconds() / 60) if condition['timeframe'] == 'daily' else int(condition['timeframe'][:-1])
        start = opening
        while start < closing:
            end = min(start + timedelta(minutes=width), closing)
            if end > now:
                break
            # An interior hole cannot change a close-only rule. A missing closing
            # minute remains explicit; consecutive closes cannot bridge that hole.
            price = minutes.get(end - timedelta(minutes=1))
            bars.append((start, end, price))
            if warnings is not None and price is not None and start >= first:
                missing = sum(start + timedelta(minutes=i) not in minutes
                              for i in range(int((end-start).total_seconds()/60)-1))
                if missing:
                    warnings.append({'bar_start': start.isoformat(), 'bar_end': end.isoformat(),
                                     'missing_interior_minutes': missing})
            start = end
    return bars


def matches(price, condition):
    if price is None:
        return False
    return price > condition['price'] if condition['rule'] == 'close_above' else price < condition['price']


def confirmations(frame, condition, now, boundary):
    bars = completed(frame, condition, now, boundary)
    count = condition['count']
    return [bars[i][1] for i in range(count-1, len(bars))
            if all(b[0] >= boundary and matches(b[2], condition) for b in bars[i-count+1:i+1])]


def advance_coverage(frame, condition, now, boundary, checkpoint, warnings=None):
    """Checkpoint only a contiguous, proved prefix; keep consecutive-close state."""
    if checkpoint.get('condition') != condition:
        checkpoint.clear()
        checkpoint['condition'] = dict(condition)
    start_at = stamp(checkpoint['through']) if checkpoint.get('through') else boundary
    run = int(checkpoint.get('matching_count', 0))
    confirmed = []
    gap = None
    for start, end, price in completed(frame, condition, now, start_at, warnings=warnings):
        if start < start_at:
            continue
        if price is None:
            gap = start.isoformat()
            break
        run = run + 1 if matches(price, condition) else 0
        checkpoint.update(through=end.isoformat(), matching_count=min(run, condition['count']))
        if run >= condition['count']:
            confirmed.append(end)
    return confirmed, gap


def recovery_start(params):
    """Earliest unresolved bar, retained in existing scenario state."""
    with state(params) as db:
        row = db.execute('SELECT status,payload FROM weekly_chart_state WHERE deployment_id=?',
                         (params['deployment_id'],)).fetchone()
        if row['status'] not in {'waiting', 'pending'}:
            return None
        data = json.loads(row['payload'])
        return stamp(data['missing_history_from']) if data.get('missing_history_from') else None


def retry_anchor(params, confirmed):
    """Freeze retry time to the confirmation's authorized session, never observation time."""
    day = confirmed.astimezone(ET).date().isoformat()
    if params['trigger']['timeframe'] == 'daily':
        calendar = exchange_calendars.get_calendar('XNYS')
        day = calendar.next_session(day).date().isoformat()
    opening, _ = session(day)
    # Old unfrozen admissions predate the compiler's explicit window provenance;
    # 09:35 is their original compiled entry start, not a new policy default.
    window = params.get('entry_window_start_et') or '09:35'
    hour, minute = map(int, window.split(':')[:2])
    authorized_start = opening.astimezone(ET).replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(UTC)
    return max(confirmed, authorized_start)


def retry_time_block(params, data, now, frozen=None):
    if not data.get('confirmation_at'):
        return None
    if frozen:
        saved = json.loads(frozen)
        params = {**params, 'entry_window_start_et': saved.get('execution', {}).get('entry_window_start_et') or params.get('entry_window_start_et')}
    anchor = retry_anchor(params, stamp(data['confirmation_at']))
    if now < anchor:
        return 'weekly_waiting_entry_window'
    if (now - anchor).total_seconds() > params['controls']['retry_seconds']:
        return 'weekly_retry_window_expired'
    return None


def observe(frame, params, now):
    """Observe even while pending; invalidation takes precedence over a same-bar trigger."""
    boundary = max(stamp(params['published_at']), stamp(params['admitted_at']))
    latest = frame.sort('timestamp').tail(1).to_dicts()[0]
    close = float(latest['close'])
    observed = latest['timestamp'].astimezone(UTC) + timedelta(minutes=1)
    with state(params) as db:
        row = db.execute('SELECT * FROM weekly_chart_state WHERE deployment_id=?', (params['deployment_id'],)).fetchone()
        data = json.loads(row['payload'])
        status = row['status']
        coverage = data.setdefault('verified_coverage', {})
        quality = data.setdefault('interior_gap_warnings', {})
        invalid_warnings, trigger_warnings = [], []
        invalid, invalid_gap = advance_coverage(frame, params['tactical_invalidation'], now, boundary,
            coverage.setdefault('invalidation', {}), warnings=invalid_warnings)
        triggers, trigger_gap = advance_coverage(frame, params['trigger'], now, boundary,
            coverage.setdefault('trigger', {}), warnings=trigger_warnings)
        for role, warnings in (('invalidation', invalid_warnings), ('trigger', trigger_warnings)):
            if warnings:
                quality[role] = warnings[-10:]
        gaps = [g for g in (invalid_gap, trigger_gap) if g]
        missing_confirmation_bars = bool(gaps)
        data['missing_history_from'] = min(gaps) if gaps else None
        if invalid and status != 'filled':
            if status != 'pending':
                status = 'invalidated'
            data['invalidated_at'] = min(invalid).isoformat()
        if triggers and 'confirmation_at' not in data:
            data['confirmation_at'] = min(triggers).isoformat()
        # A daily confirmation has exactly one following session in which to enter.
        if params['trigger']['timeframe'] == 'daily' and triggers:
            data['confirmation_at'] = max(triggers).isoformat()
        underlying_age = (now-observed).total_seconds()
        data['close_evidence_policy'] = 'observed_calendar_bar_closing_minute_v1'
        data.update(last_evaluation=now.isoformat(), underlying_observed_at=observed.isoformat(), close=close,
                    underlying_age_seconds=underlying_age,
                    evaluation_coverage='incomplete' if gaps or not 0 <= underlying_age <= 120 else 'observed')
        reason = source_block(params, now)
        if data.get('invalidated_at') and status != 'filled':
            reason = 'weekly_invalidated'
        elif status in {'filled', 'invalidated', 'uncertain'}:
            reason = 'weekly_' + status
        elif now >= stamp(params['valid_through']):
            status, reason = 'expired', 'weekly_expired'
        elif not 0 <= (now-observed).total_seconds() <= 120:
            reason = 'weekly_underlying_stale'
        elif missing_confirmation_bars:
            reason = reason or 'weekly_confirmation_data_gap'
        elif not data.get('confirmation_at'):
            reason = reason or 'weekly_waiting_confirmation'
        else:
            confirmed = stamp(data['confirmation_at'])
            if params['trigger']['timeframe'] == 'daily':
                calendar = exchange_calendars.get_calendar('XNYS')
                next_day = calendar.next_session(confirmed.astimezone(ET).date().isoformat()).date()
                if now.astimezone(ET).date() != next_day:
                    reason = reason or 'weekly_waiting_next_session_confirmation'
            # Recompute deterministically from the original confirmation, including
            # old state whose clock began before the authorized entry window.
            anchor_params = params
            if row['frozen_deployment']:
                frozen = json.loads(row['frozen_deployment'])
                anchor_params = {**params, 'entry_window_start_et': frozen.get('execution', {}).get('entry_window_start_et') or params.get('entry_window_start_et')}
            anchor = retry_anchor(anchor_params, confirmed)
            data.update(retry_started_at=anchor.isoformat(), retry_confirmation=data['confirmation_at'])
            if now < anchor:
                reason = reason or 'weekly_waiting_entry_window'
            if data.get('retry_started_at') and (now-stamp(data['retry_started_at'])).total_seconds() > params['controls']['retry_seconds']:
                reason = reason or 'weekly_retry_window_expired'
            if not matches(close, params['trigger']):
                reason = reason or 'weekly_price_no_longer_beyond_trigger'
            if abs(close / params['trigger']['price'] - 1) > params['controls']['max_entry_distance_pct']:
                reason = reason or 'weekly_entry_distance_exceeded'
        data['reason'] = reason or 'weekly_confirmed'
        db.execute('UPDATE weekly_chart_state SET status=?,payload=? WHERE deployment_id=?',
                   (status, json.dumps(data), params['deployment_id']))
    return data, status


def reserve(deployment, now, open_count):
    params = deployment.strategy.params
    reason = source_block(params, now)
    if reason:
        return reason
    if not deployment.execution.shadow_only:
        return 'weekly_live_not_armed'
    with state(params) as db:
        row = db.execute('SELECT * FROM weekly_chart_state WHERE deployment_id=?', (deployment.deployment_id,)).fetchone()
        data = json.loads(row['payload'])
        if row['status'] != 'waiting' or data.get('reason') != 'weekly_confirmed':
            return 'weekly_not_eligible'
        reason = retry_time_block(params, data, now, row['frozen_deployment'])
        if reason:
            return reason
        if (now-stamp(data['last_evaluation'])).total_seconds() > 120:
            return 'weekly_evaluation_stale'
        occupied = db.execute("SELECT count(*) FROM weekly_chart_state WHERE scenario_key=? AND entry_arm=? AND status IN ('pending','filled','uncertain')", (params['scenario_key'], params.get('entry_arm', 'baseline'))).fetchone()[0]
        pending = db.execute("SELECT count(*) FROM weekly_chart_state WHERE entry_arm=? AND status IN ('pending','uncertain')", (params.get('entry_arm', 'baseline'),)).fetchone()[0]
        if occupied:
            return 'weekly_scenario_already_reserved_or_consumed'
        if pending + open_count >= params['controls']['max_open_positions']:
            return 'weekly_max_open_positions'
        db.execute("UPDATE weekly_chart_state SET status='pending', frozen_deployment=? WHERE deployment_id=?", (deployment.model_dump_json(), deployment.deployment_id))
    return None


def transition(deployment, status, trade_id=None):
    if getattr(getattr(deployment, 'strategy', None), 'key', None) != 'weekly_chart':
        return
    params = deployment.strategy.params
    with state(params) as db:
        # Never undo consumption or a confirmed invalidation on a no-fill callback.
        if status == 'waiting':
            row = db.execute('SELECT payload FROM weekly_chart_state WHERE deployment_id=?', (deployment.deployment_id,)).fetchone()
            terminal = 'invalidated' if json.loads(row['payload']).get('invalidated_at') else 'waiting'
            db.execute("UPDATE weekly_chart_state SET status=? WHERE deployment_id=? AND status='pending'", (terminal, deployment.deployment_id))
        else:
            db.execute('UPDATE weekly_chart_state SET status=?,trade_id=COALESCE(?,trade_id) WHERE deployment_id=?', (status, trade_id, deployment.deployment_id))


def pending_block(deployment, now):
    params = deployment.strategy.params
    reason = source_block(params, now)
    if reason:
        return reason
    with state(params) as db:
        row = db.execute('SELECT * FROM weekly_chart_state WHERE deployment_id=?', (deployment.deployment_id,)).fetchone()
        data = json.loads(row['payload'])
        if row['status'] != 'pending':
            return 'weekly_' + row['status']
        if data.get('reason') != 'weekly_confirmed':
            return data.get('reason') or 'weekly_underlying_stale'
        reason = retry_time_block(params, data, now, row['frozen_deployment'])
        if reason:
            return reason
        if (now-stamp(data['last_evaluation'])).total_seconds() > 120:
            return 'weekly_underlying_stale'
    return None


class WeeklyChartStrategy:
    key = 'weekly_chart'

    def required_features(self, params):
        return {'timestamp', 'symbol', 'close'}

    def evaluate_entry(self, frame, deployment_id, params):
        now = datetime.now(UTC)
        data, status = observe(frame, params, now)
        return SignalDecision(deployment_id=deployment_id, symbol=params['symbol'], timestamp=now,
            signal=data['reason'] == 'weekly_confirmed' and status == 'waiting',
            direction=SignalDirection.LONG if params['direction']=='long' else SignalDirection.SHORT,
            reason=[data['reason']], features={'close': data['close'], 'confirmation_at': data.get('confirmation_at'),
                'entry_arm': params.get('entry_arm', 'baseline'), 'scenario_key': params['scenario_key'], 'publication_hash': params['publication_hash'],
                'evaluation_coverage': data['evaluation_coverage'],
                'close_evidence_policy': data['close_evidence_policy'],
                'interior_gap_warnings': data.get('interior_gap_warnings', {}),
                'missing_history_from': data.get('missing_history_from'), 'underlying_observed_at': data.get('underlying_observed_at')})

    def evaluate_exit(self, frame, deployment_id, params, position):
        now = datetime.now(UTC)
        boundary = position.entry_timestamp or stamp(params['admitted_at'])
        invalid = confirmations(frame, params['tactical_invalidation'], now, boundary)
        return ExitDecision(deployment_id=deployment_id, symbol=params['symbol'], timestamp=now,
            exit=bool(invalid), action='square_off' if invalid else 'hold',
            reason=['weekly_completed_bar_tactical_invalidation' if invalid else 'weekly_tactical_valid'])


def frozen_deployment(params):
    """Read an already reserved policy without mutating state during compilation."""
    path = Path(params['state_db'])
    if not path.exists():
        return None
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='weekly_chart_state'").fetchone():
            return None
        row = db.execute("SELECT frozen_deployment FROM weekly_chart_state WHERE deployment_id=? AND status IN ('pending','filled')", (params['deployment_id'],)).fetchone()
        if row and row[0]:
            result = json.loads(row[0])
            result['strategy']['params']['controls']['mode'] = params['controls']['mode']
            result['strategy']['params']['controls']['entry_timing_comparison'] = params['controls'].get('entry_timing_comparison', 'OFF')
            result['strategy']['params']['admission_block'] = params.get('admission_block')
            return result
    return None

"""Read-only weekly source admission; Sheet controls, existing compiler/executor."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path('artifacts/cartographer-weekly')
OWNER = 'cartographer_weekly'
PROFILE_MAP = {'TREND_CONTINUATION': 'trend_continuation_balanced',
               'FLASH_REVERSAL': 'flash_reversal_fast_snap',
               'EXHAUSTION_REVERSAL': 'exhaustion_reversal_climax',
               'RANGE_EXPANSION': 'trend_continuation_balanced'}
DEFAULTS = {'mode': 'SHADOW', 'entry_timing_comparison': 'OFF', 'max_contracts': 1, 'max_trade_premium_usd': 400,
            'max_open_positions': 2, 'entry_execution_profile': 'balanced',
            'max_entry_distance_pct': 0.01, 'retry_seconds': 600,
            'dte_min': 7, 'dte_max': 21, 'dte_fallback_max': 28,
            'compare_exits': 'trend_continuation_balanced,flash_reversal_fast_snap,exhaustion_reversal_climax,range_expansion_swing,dynamic_envelope_curv15,profit_preservation_v1',
            **{f'exit_{k.lower()}': v for k, v in PROFILE_MAP.items()}}


def stamp(value: str) -> datetime:
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(UTC)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temp.replace(path)


def controls(defaults: dict) -> dict:
    raw = defaults.get(OWNER, {})
    if not raw:
        return {**DEFAULTS, 'mode': 'OFF'}
    c = {**DEFAULTS, **raw}
    c['mode'] = str(c['mode']).upper()
    if c['mode'] not in {'OFF', 'SHADOW'}:
        raise ValueError('cartographer_weekly supports OFF/SHADOW only; LIVE is not armed')
    c['entry_timing_comparison'] = str(c['entry_timing_comparison']).upper()
    if c['entry_timing_comparison'] not in {'OFF', 'PAIRED'}:
        raise ValueError('weekly entry_timing_comparison must be OFF/PAIRED')
    for key in ['max_contracts', 'max_open_positions', 'retry_seconds', 'dte_min', 'dte_max', 'dte_fallback_max']:
        val = float(c[key])
        if not math.isfinite(val) or val <= 0 or not val.is_integer():
            raise ValueError(f'invalid weekly {key}')
        c[key] = int(val)
    for key in ['max_trade_premium_usd', 'max_entry_distance_pct']:
        c[key] = float(c[key])
        if not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f'invalid weekly {key}')
    if not c['dte_min'] <= c['dte_max'] <= c['dte_fallback_max']:
        raise ValueError('weekly DTE bounds are not ordered')
    c['max_trade_premium_usd'] = min(c['max_trade_premium_usd'], float(defaults.get('max_trade_premium_usd', c['max_trade_premium_usd'])))
    return c


def load_publication(pointer: Path) -> tuple[dict, dict, dict]:
    latest = json.loads(pointer.read_text())
    run = Path(latest['run_dir']).resolve()
    if not run.is_relative_to(pointer.parent.resolve() / 'runs'):
        raise ValueError('weekly publication outside source runs')
    receipt = json.loads((run / 'receipt.json').read_text())
    if receipt.get('status') != 'succeeded':
        raise ValueError('weekly publication not successful')
    hashes = {i['path']: i['sha256'] for i in receipt['artifacts']}
    docs = {}
    for name in ['weekly-book.json', 'analyst-packet.json', 'evidence.json']:
        raw = (run / name).read_bytes()
        if hashes.get(name) != 'sha256:' + hashlib.sha256(raw).hexdigest():
            raise ValueError(f'weekly receipt mismatch: {name}')
        docs[name] = json.loads(raw)
    book, packet = docs['weekly-book.json'], docs['analyst-packet.json']
    if book.get('schema') != 'market_cartographer.weekly_market_book.v1':
        raise ValueError('unsupported weekly book schema')
    for field in ['pack_id', 'observation_time', 'information_cutoff']:
        if not book.get(field) or any(v.get(field) != book[field] for v in [packet, receipt]):
            raise ValueError(f'weekly source identity mismatch: {field}')
    mode = str(docs['evidence.json'].get('data_mode') or '').lower()
    if not mode or any(x in mode for x in ['fixture', 'demo', 'synthetic']):
        raise ValueError('weekly evidence is not real market data')
    published = stamp(latest['updated_at'])
    if published < stamp(book['information_cutoff']) or published > datetime.now(UTC):
        raise ValueError('invalid publication clock')
    return book, packet, {'publication_hash': hashes['weekly-book.json'],
                          'published_at': published.isoformat(), 'run_id': receipt['run_id'],
                          'source_path': str(run / 'weekly-book.json')}


def resolve_condition(raw: dict, anchors: dict, *, structural: bool = False) -> dict:
    if raw.get('rule') not in {'close_above', 'close_below'}:
        raise ValueError('unsupported confirmation rule')
    if raw.get('confirmation_timeframe') not in ({'5m', '39m', 'daily', 'weekly'} if structural else {'5m', '39m', 'daily'}):
        raise ValueError('unsupported confirmation timeframe')
    anchor = anchors[raw['anchor_id']]
    price = float(anchor['price'])
    count = raw.get('minimum_completed_bars')
    if not math.isfinite(price) or price <= 0 or not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 3:
        raise ValueError('invalid source price/confirmation count')
    return {'rule': raw['rule'], 'timeframe': raw['confirmation_timeframe'],
            'count': count, 'price': price, 'anchor_id': raw['anchor_id']}


def resolve_plans(book: dict, packet: dict, provenance: dict, admitted_at: str) -> tuple[list[dict], list[dict]]:
    candidates = {c['symbol']: c for c in packet['candidates']}
    plans, unsupported = [], []
    for scenario in book['scenarios']:
        common = {**provenance, 'pack_id': book['pack_id'], 'scenario_id': scenario['scenario_id'],
                  'symbol': scenario['symbol'], 'author_profile': scenario.get('management_profile'),
                  'setup_type': scenario.get('setup_type'), 'thesis': scenario.get('thesis', ''),
                  'admitted_at': admitted_at}
        # Producer pack identity is the lineage boundary; do not merge independent weekly ideas by ticker.
        group = 'cw-' + digest([book['pack_id'], scenario['scenario_id'], scenario['symbol']])[:20]
        common['scenario_key'] = group
        branches = scenario.get('what_if', {}).get('branches', [])
        if scenario.get('disposition') != 'scenario' or not branches:
            unsupported.append({**common, 'reason': 'no_executable_directional_branch'})
            continue
        for branch in branches:
            row = {**common, 'branch_id': branch['id'], 'deployment_id': group + '-' + digest(branch['id'])[:8]}
            try:
                if branch.get('prerequisite') is not None:
                    raise ValueError('prerequisite/retest requires an explicit supported state machine')
                if branch.get('bias') not in {'bull', 'bear'}:
                    raise ValueError('neutral branch is not a single-leg directional plan')
                if scenario['management_profile'] not in PROFILE_MAP:
                    raise ValueError('unsupported author management profile')
                anchors = {a['anchor_id']: a for a in candidates[scenario['symbol']]['what_if_anchors']}
                for key in ['trigger', 'tactical_invalidation', 'structural_invalidation']:
                    row[key] = resolve_condition(branch[key], anchors, structural=key == "structural_invalidation")
                row['direction'] = 'long' if branch['bias'] == 'bull' else 'short'
                if row['trigger']['rule'] != ('close_above' if row['direction'] == 'long' else 'close_below'):
                    raise ValueError('branch direction disagrees with trigger')
                if row['tactical_invalidation']['rule'] == row['trigger']['rule']:
                    raise ValueError('tactical invalidation must oppose the directional trigger')
                row['valid_through'] = stamp(branch['expires_at']).isoformat()
                if stamp(row['valid_through']) <= stamp(admitted_at):
                    raise ValueError('source branch expired before admission')
                plans.append(row)
            except (ValueError, KeyError, TypeError) as exc:
                unsupported.append({**row, 'reason': str(exc)})
    return plans, unsupported


def import_publication(pointer: Path, root: Path = ROOT, now: datetime | None = None, *, policy: dict | None = None) -> dict:
    now = now or datetime.now(UTC)
    path = root / 'admissions.json'
    prior = json.loads(path.read_text()) if path.exists() else {'plans': [], 'unsupported': []}
    book, packet, provenance = load_publication(pointer)
    old = {p['deployment_id']: p for p in prior['plans']}
    plans, unsupported = resolve_plans(book, packet, provenance, now.isoformat())
    for plan in plans:
        previous = old.get(plan['deployment_id'])
        if previous and previous['publication_hash'] == plan['publication_hash']:
            plan = previous  # Restart/import must not move admission time.
        elif previous:
            # A revision cannot mutate already admitted execution plans. Surface it for the next review.
            unsupported.append({**plan, 'reason': 'revision_after_admission_retained_frozen_plan'})
            plan = {**previous, 'admission_block': 'weekly_source_revision_requires_readmission'}
        old[plan['deployment_id']] = plan
    for plan in old.values():
        if plan['pack_id'] == book['pack_id'] and plan['publication_hash'] != provenance['publication_hash']:
            plan['admission_block'] = 'weekly_source_revision_requires_readmission'
    if policy and policy['mode'] == 'SHADOW' and policy['entry_timing_comparison'] == 'PAIRED':
        for plan in old.values():
            if not plan.get('admission_block') and stamp(plan['valid_through']) > now:
                plan.setdefault('early_admitted_at', now.isoformat())
    result = {'schema': 'bhiksha.weekly_admissions.v1', 'checked_at': now.isoformat(),
              'source': provenance, 'plans': list(old.values()), 'unsupported': unsupported}
    atomic_json(path, result)
    atomic_json(root / 'source_health.json', {'ok': True, 'checked_at': now.isoformat(), **provenance,
        'blocked_deployments': [p['deployment_id'] for p in old.values() if p.get('admission_block')]})
    return result



EARLY_SUFFIX = '-early1m'


def arm_for_id(deployment_id: str) -> str:
    return 'early_1m' if deployment_id.endswith(EARLY_SUFFIX) else 'baseline'


def execution_plans(admissions: dict) -> list[dict]:
    """Keep baseline identities; admission of the extra arm is durable and prospective."""
    result = []
    for p in admissions.get('plans', []):
        base = {**p, 'base_deployment_id': p['deployment_id'], 'entry_arm': 'baseline',
                'author_trigger': p['trigger']}
        result.append(base)
        if p.get('early_admitted_at'):
            result.append({**base, 'deployment_id': p['deployment_id'] + EARLY_SUFFIX,
                'entry_arm': 'early_1m', 'admitted_at': p['early_admitted_at'],
                'trigger': {**p['trigger'], 'timeframe': '1m', 'count': 1}})
    return result


def build_rows(defaults: dict, root: Path = ROOT, db_path: str = 'bhiksha.db') -> list:
    from bhiksha.active_plan.compiler import ActivePlanSheetRow
    c = controls(defaults)
    path = root / 'admissions.json'
    if not path.exists():
        if c['mode'] == 'OFF':
            return []
        return []  # The weekly owner publishes failure; unrelated scanner compilation continues.
    registry = json.loads(path.read_text())
    rows = []
    for p in execution_plans(registry):
        policy = c[f"exit_{p['author_profile'].lower()}"]
        params = {**p, 'controls': c, 'state_db': str(Path(db_path).resolve()),
                  'source_health_path': str((root / 'source_health.json').resolve())}
        rows.append(ActivePlanSheetRow(
            row_id=p['deployment_id'], row_type='manual', enabled=True, symbol=p['symbol'],
            authorization_mode='shadow', manual_setup_type='manual_trigger', direction=p['direction'],
            trigger_price=p['trigger']['price'], trigger_direction='ABOVE' if p['direction']=='long' else 'BELOW',
            management_exit=policy, compare_exits=[v.strip() for v in str(c['compare_exits']).split(',') if v.strip()],
            strategy_class='weekly_chart_' + p['setup_type'] + '__' + p['entry_arm'],
            execution_overrides={'dte_min': c['dte_min'], 'dte_max': c['dte_max'],
                'dte_fallback_policy': 'allow_nearest_after', 'dte_fallback_max': c['dte_fallback_max'],
                'target_abs_delta_min': float(defaults.get('delta_min', 0.15)), 'target_abs_delta_max': float(defaults.get('delta_max', 0.35)),
                'min_open_interest': 0, 'max_bid_ask_spread_pct': None,
                'preferred_min_open_interest': int(defaults.get('min_open_interest', 100)),
                'preferred_max_bid_ask_spread_pct': float(defaults.get('max_bid_ask_spread_pct', 0.20)),
                'entry_pricing_require_open_interest': True,
                'entry_execution_profile': c['entry_execution_profile'], 'entry_reprice_max_chase_pct': 0.15,
                'entry_window_start_et': '09:35', 'entry_window_end_et': '15:45'},
            risk_overrides={'max_contracts': c['max_contracts'], 'max_trade_premium_usd': c['max_trade_premium_usd']},
            source_metadata={'source_owner': OWNER, 'weekly_plan': params},
        ))
    return rows

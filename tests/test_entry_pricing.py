"""Frozen midpoint policy scenarios, independent of LIVE versus modeled fills."""
from datetime import UTC, datetime, timedelta
import pytest
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.pricing import (
    ENTRY_EXECUTION_PROFILES, build_entry_profile_comparison,
    resolve_entry_reprice_max_chase_pct, select_entry_limit, floor_entry_price,
)
NOW = datetime(2026, 10, 8, 14, tzinfo=UTC)


def market(bid=2.7, ask=2.9, oi=550, at=NOW):
    return PublicQuote('OPT', bid=bid, ask=ask, open_interest=oi,
        quote_timestamp=at.isoformat(), quote_timestamp_field='quoteTimestamp')


def test_normal_market_starts_midpoint_even_with_low_oi():
    for oi in (1, 550):
        result=select_entry_limit(market(oi=oi), {'entry_execution_profile':'balanced',
            'preferred_min_open_interest':100}, observed_at=NOW)
        assert result.approved and result.limit_price==2.8
        assert result.max_entry_price==pytest.approx(3.22)
        assert not result.wide_market


def test_rblx_bargain_does_not_limit_midpoint_replacement():
    params={'entry_execution_profile':'balanced'}
    initial=select_entry_limit(market(.35,.89),params,observed_at=NOW)
    assert initial.limit_price==.48 and initial.original_mid==pytest.approx(.62)
    assert initial.max_entry_price==pytest.approx(.713)
    later=select_entry_limit(market(.35,.89),{**params,'entry_reprice_step':True,
        'entry_original_mid':initial.original_mid,'entry_price_ceiling':initial.max_entry_price,
        'entry_wide_market':initial.wide_market},observed_at=NOW)
    assert later.limit_price==.62
    moved=select_entry_limit(market(.8,1.1),{**params,'entry_reprice_step':True,
        'entry_original_mid':initial.original_mid,'entry_price_ceiling':initial.max_entry_price,
        'entry_wide_market':True},observed_at=NOW)
    assert moved.limit_price==.71  # clipped, never reset from the later midpoint


def test_normal_reprice_clips_to_frozen_ceiling_and_current_ask():
    result=select_entry_limit(market(1,1.1),{'entry_execution_profile':'balanced'},observed_at=NOW)
    params={'entry_reprice_step':True,'entry_original_mid':result.original_mid,
        'entry_price_ceiling':result.max_entry_price,'entry_wide_market':False}
    assert select_entry_limit(market(1.3,1.6),params,observed_at=NOW).limit_price==1.2
    assert select_entry_limit(market(.95,1),params,observed_at=NOW).limit_price==1
    assert select_entry_limit(market(1,1.1),{**params,'entry_price_through_target':1.02},observed_at=NOW).limit_price==1.02


def test_smh_buy_ceiling_is_floored_to_valid_tick():
    assert floor_entry_price(3.64,.05)==3.60
    assert floor_entry_price(3.65,.05)==3.65


@pytest.mark.parametrize('change,reason',[
    ({'at':NOW-timedelta(seconds=8.001)},'public_quote_stale_or_unproven'),
    ({'bid':None},'public_quote_missing_bid_ask'),
    ({'bid':3,'ask':2},'public_quote_crossed_bid_ask'),
    ({'oi':0},'public_open_interest_missing'),
    ({'oi':None},'public_open_interest_missing'),
    ({'bid':.01,'ask':2},'public_spread_absurd'),
])
def test_real_quote_and_sanity_limits_still_block(change,reason):
    result=select_entry_limit(market(**change),observed_at=NOW)
    assert not result.approved and reason in result.block_reasons


def test_profiles_have_one_step_and_existing_patience_and_chase():
    assert ENTRY_EXECUTION_PROFILES['patient'].reprice_checkpoints_seconds==(60,)
    assert ENTRY_EXECUTION_PROFILES['balanced'].reprice_checkpoints_seconds==(30,)
    assert ENTRY_EXECUTION_PROFILES['urgent'].reprice_checkpoints_seconds==(15,)
    assert [ENTRY_EXECUTION_PROFILES[p].cancel_after_seconds for p in ('patient','balanced','urgent')]==[300,150,60]
    assert resolve_entry_reprice_max_chase_pct({'entry_execution_profile':'patient','entry_reprice_max_chase_pct':.05})==.05
    comparisons=build_entry_profile_comparison(market(at=datetime.now(UTC)),{},open_interest_percentile=.1)
    assert all(v['quote_limit_price']==2.8 for v in comparisons.values())
    assert comparisons['patient']['max_entry_price']==pytest.approx(3.08)

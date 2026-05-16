"""Unit tests for dashboard_builder biting math.

Mirrors Passivbot's selector clamp from passivbot-rust/src/coin_selection.rs:
   keep = min(max(round(n*(1-drop)), n_pos), n)

Run:
    cd ~/Projects/trading-bots/passivbot
    ./venv/bin/python -m pytest daemon/tests/test_dashboard_biting.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))

from dashboard_builder import (  # noqa: E402
    _coins_count_from_csv,
    _selector_keep,
    biting_metrics,
)


def test_coins_count_numeric_label():
    assert _coins_count_from_csv("14,coins") == 14
    assert _coins_count_from_csv("14,coins,suite") == 14
    assert _coins_count_from_csv("3,coins") == 3


def test_coins_count_ticker_list():
    assert _coins_count_from_csv("BTC,ETH,SOL") == 3
    assert _coins_count_from_csv("ARB,BTC,ETH,SOL") == 4
    assert _coins_count_from_csv("BTC") == 1


def test_coins_count_empty_or_none():
    assert _coins_count_from_csv(None) is None
    assert _coins_count_from_csv("") is None
    assert _coins_count_from_csv(",,,") is None


def test_selector_keep_clamped_to_n_pos():
    # 14 candidates, n_pos=4, drop=1.0 → round(14*0)=0 → bumped to 1 → max(1,4)=4
    assert _selector_keep(14, 4, 1.0) == 4
    # Same with drop=0.95 → round(14*0.05)=1 → max(1,4)=4
    assert _selector_keep(14, 4, 0.95) == 4


def test_selector_keep_clamped_to_n_cand():
    # 3 candidates, n_pos=4 → can never exceed 3 even if n_pos asks for 4
    assert _selector_keep(3, 4, 0.0) == 3
    assert _selector_keep(3, 4, 0.5) == 3
    assert _selector_keep(3, 4, 0.99) == 3


def test_selector_keep_normal_range():
    # 14 candidates, n_pos=4, drop=0.44 → round(14*0.56)=8 → max(8,4)=8
    assert _selector_keep(14, 4, 0.44) == 8
    # drop=0.18 → round(14*0.82)=11
    assert _selector_keep(14, 4, 0.18) == 11
    # drop=0.0 → keep everything
    assert _selector_keep(14, 4, 0.0) == 14


def test_biting_14_coins_4_pos_filter_bites_when_volume_loose_and_vol_nonzero():
    # Top-Sharpe NSGA2 candidate from 2026-05-15 run: vol_drop=0.099, volm=0.44
    bm = biting_metrics(14, 4, volume_drop=0.44, vol_drop=0.099)
    assert bm["keep_v"] == 8           # round(14*0.56)=8
    assert bm["keep_t"] == 7           # round(8*0.901)=7
    assert bm["biting"] is True


def test_biting_14_coins_4_pos_filter_does_not_bite_with_tight_volume():
    # User-noted "0.12 cluster" with tight volume_drop=0.957: vol_drop is a passenger
    bm = biting_metrics(14, 4, volume_drop=0.957, vol_drop=0.122)
    assert bm["keep_v"] == 4           # round(14*0.043)=1 → bumped to 4
    assert bm["keep_t"] == 4           # round(4*0.878)=4
    assert bm["biting"] is False


def test_biting_3_coins_4_pos_can_never_bite():
    # n_cand < n_pos: selector clamps both keeps to n_cand → no slack ever
    for volm in [0.0, 0.5, 0.99]:
        for vol in [0.0, 0.5, 0.99]:
            bm = biting_metrics(3, 4, volume_drop=volm, vol_drop=vol)
            assert bm["keep_v"] == 3, (volm, vol, bm)
            assert bm["keep_t"] == 3, (volm, vol, bm)
            assert bm["biting"] is False, (volm, vol, bm)


def test_biting_zero_vol_drop_never_bites():
    # vol_drop=0 means vol stage does nothing regardless of keep_v slack
    for volm in [0.0, 0.18, 0.44, 0.7, 0.99]:
        bm = biting_metrics(14, 4, volume_drop=volm, vol_drop=0.0)
        assert bm["biting"] is False, (volm, bm)


def test_biting_high_vol_drop_with_slack_bites():
    # volume_drop=0.18 leaves keep_v=11; high vol_drop reduces to 5 → bites
    bm = biting_metrics(14, 4, volume_drop=0.18, vol_drop=0.5)
    assert bm["keep_v"] == 11
    assert bm["keep_t"] == 6  # round(11*0.5)=6 → max(6,4)=6
    assert bm["biting"] is True


def test_biting_returns_none_on_missing_inputs():
    assert biting_metrics(None, 4, 0.5, 0.1)["biting"] is None
    assert biting_metrics(14, None, 0.5, 0.1)["biting"] is None
    assert biting_metrics(14, 4, None, 0.1)["biting"] is None


def test_biting_treats_missing_vol_drop_as_zero():
    # vol_drop=None (unbounded short side, etc.) should produce a sensible
    # "not biting" answer rather than None — vol filter just doesn't fire.
    bm = biting_metrics(14, 4, volume_drop=0.44, vol_drop=None)
    assert bm["keep_v"] == 8
    assert bm["biting"] is False

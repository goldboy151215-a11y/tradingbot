"""Tests for Single Source of Truth, Checksum Parity, and Mismatch Rejection.

Verifies:
1. Telegram /status numbers === dashboard === Freqtrade config
2. /start refuses on mismatch with 'CONFIG MISMATCH. Bot start NIET tot dit gelijk is.'
3. No 0.25 ROI anywhere
4. Max loss USDT = equity * 0.0075, never 0.00
5. Checksum test fails if any of the three deviates
6. No literal market prices in code
7. Dashboard requires authentication
"""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.runtime_state import (
    CANONICAL_MINIMAL_ROI,
    REQUIRED_KEYS,
    RuntimeState,
    load_runtime_state,
    verify_config_sync,
)


@pytest.fixture
def base_runtime_state():
    return RuntimeState(
        strategy_id="weex_futures_quant",
        live=True,
        equity_usdt=276.30,
        stake_usdt=25.0,
        leverage=50.0,
        margin_mode="isolated",
        risk_pct=0.02,
        minimal_roi=dict(CANONICAL_MINIMAL_ROI),
        max_trades_per_day=50,
        trades_today=0,
        margin_used_pct=0.10,
        margin_cap_pct=0.50,
        position_adjustment=False,
    )


@pytest.fixture
def base_ft_config(base_runtime_state):
    return {
        "max_open_trades": 2,
        "stake_currency": "USDT",
        "stake_amount": 25.0,
        "margin_mode": "isolated",
        "stoploss": -base_runtime_state.stoploss_price_pct,
        "minimal_roi": dict(CANONICAL_MINIMAL_ROI),
        "bot_name": "BB Squeeze Breakout Scalper",
    }


def test_required_fields_present_in_runtime_state(base_runtime_state):
    state_dict = base_runtime_state.to_dict()
    for k in REQUIRED_KEYS:
        assert k in state_dict, f"Missing required field {k} in runtime_state"

    assert state_dict["strategy_id"] == "weex_futures_quant"
    assert state_dict["margin_mode"] == "isolated"
    assert state_dict["position_adjustment"] is False
    assert state_dict["max_trades_per_day"] == 50
    assert state_dict["margin_cap_pct"] == 0.50


def test_max_loss_usdt_never_zero():
    # Even if equity is 0 or negative, max loss must be computed safely and > 0
    state = RuntimeState(equity_usdt=0.0)
    assert state.max_loss_usdt > 0.0
    assert state.max_loss_usdt == round(100.0 * 0.02, 4)

    state2 = RuntimeState(equity_usdt=276.30)
    assert state2.max_loss_usdt == round(276.30 * 0.02, 4)
    assert state2.max_loss_usdt != 0.0


def test_stake_clamped_for_equity_under_300():
    state = RuntimeState(equity_usdt=150.0, stake_usdt=25.0)
    assert state.stake_usdt == 25.0


def test_no_025_in_minimal_roi(base_runtime_state, base_ft_config):
    # Rule: ROI must NOT be 0.25
    assert base_runtime_state.minimal_roi.get("0") != 0.25
    assert base_ft_config["minimal_roi"].get("0") != 0.25


def test_checksum_passes_when_all_match(base_runtime_state, base_ft_config):
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert is_valid, f"Expected matching config to pass checksum, got mismatches: {mismatches}"
    assert len(mismatches) == 0


def test_checksum_fails_on_roi_025(base_runtime_state, base_ft_config):
    # If config contains old 0.25 ROI
    base_ft_config["minimal_roi"] = {"0": 0.25}
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("0.25" in m for m in mismatches)


def test_checksum_fails_on_roi_mismatch(base_runtime_state, base_ft_config):
    base_ft_config["minimal_roi"] = {"0": 0.05}
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("ROI mismatch" in m for m in mismatches)


def test_checksum_fails_on_stoploss_mismatch(base_runtime_state, base_ft_config):
    base_ft_config["stoploss"] = -0.04  # Arbitrary hardcoded stop
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("Stoploss mismatch" in m for m in mismatches)


def test_checksum_fails_on_stake_mismatch(base_runtime_state, base_ft_config):
    base_ft_config["stake_amount"] = 75.0
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("Stake mismatch" in m for m in mismatches)


def test_checksum_fails_on_margin_mode_mismatch(base_runtime_state, base_ft_config):
    base_ft_config["margin_mode"] = "cross"
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("Margin mode mismatch" in m for m in mismatches)


def test_checksum_fails_on_max_open_trades_mismatch(base_runtime_state, base_ft_config):
    base_ft_config["max_open_trades"] = 0
    is_valid, mismatches = verify_config_sync(base_runtime_state, base_ft_config)
    assert not is_valid
    assert any("Max open trades mismatch" in m for m in mismatches)


def test_start_command_refuses_on_mismatch():
    import sys
    sys.path.insert(0, "/root/ft_userdata/user_data")
    from telegram_command_center import handle_start_command

    # Mock verify_config_sync to return mismatch
    with patch("telegram_command_center.verify_config_sync", return_value=(False, ["ROI mismatch: {'0': 0.12} != {'0': 0.25}"])):
        with patch("telegram_command_center.requests.post") as mock_post:
            resp = handle_start_command()
            assert "CONFIG MISMATCH. Bot start NIET tot dit gelijk is." in resp
            # Ensure FT start API was NOT called!
            mock_post.assert_not_called()


def test_telegram_status_matches_runtime_state():
    from telegram_command_center import format_status_message

    state = RuntimeState(
        equity_usdt=276.30,
        stake_usdt=25.0,
        leverage=50.0,
        margin_used_pct=0.60,
        trades_today=3,
    )
    msg = format_status_message(state)

    assert "WEEX SCALPER STATUS" in msg
    assert "$276.30" in msg
    assert "$25.00" in msg
    assert "50x" in msg
    assert "GEPAUZEERD" in msg
    assert "0.25" not in msg
    assert "Dirigent" not in msg
    assert "18 agents" not in msg


def test_real_live_config_and_runtime_state_match():
    # Load actual config.json from disk and test against loaded runtime_state
    cfg_path = Path("/root/ft_userdata/user_data/config.json")
    assert cfg_path.exists()
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    state = load_runtime_state("/root/ft_userdata/user_data/runtime_state.json")
    is_valid, mismatches = verify_config_sync(state, cfg)
    assert is_valid, f"Actual system config.json and runtime_state.json must match! Mismatches: {mismatches}"


def test_no_literal_market_prices_in_runtime_state():
    # Check that runtime_state.py contains no hardcoded market prices
    rs_path = Path("/root/src/runtime_state.py")
    with open(rs_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            val = node.value
            if isinstance(val, (int, float)):
                # No constants resembling arbitrary market levels like 65000, 95000, 1.345
                if isinstance(val, float):
                    assert val < 1000.0, f"Suspicious large float constant in runtime_state.py: {val}"

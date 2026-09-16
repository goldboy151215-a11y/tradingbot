"""Tests for risk management, sizing, caps, cooldowns, and trailing stops."""

import pytest
from src.risk import (
    ActivePosition,
    PositionSizingResult,
    RiskManager,
    RiskParameters,
    TradeState,
    calculate_position_size,
)
from src.strategy import SignalType


def test_position_sizing_standard_calculation():
    """Verify size = (equity * risk_pct) / abs(entry - stop)."""
    equity = 10000.0
    entry = 100.0
    stop = 95.0
    params = RiskParameters(risk_pct=0.0075, max_notional=100000.0)

    # Risk amount: 10000 * 0.0075 = 75.0
    # Stop distance: 5.0
    # Expected size: 75.0 / 5.0 = 15.0
    res = calculate_position_size(equity, entry, stop, params)
    assert res.is_valid is True
    assert res.risk_amount == pytest.approx(75.0)
    assert res.size == pytest.approx(15.0)
    assert res.notional == pytest.approx(1500.0)


def test_position_sizing_leverage_and_notional_caps():
    """Position sizing adheres to leverage cap (max 3x) and max notional."""
    equity = 1000.0
    entry = 100.0
    stop = 99.9  # Very tight stop would request huge position
    params = RiskParameters(risk_pct=0.0075, max_notional=2500.0)

    # With requested leverage 5x, it must be capped at 3x (equity * 3 = 3000)
    # But max_notional is 2500.0, so effective cap is 2500.0
    # Max size = 2500.0 / 100.0 = 25.0
    res = calculate_position_size(equity, entry, stop, params, requested_leverage=5.0)
    assert res.effective_leverage == 3.0
    assert res.notional == pytest.approx(2500.0)
    assert res.size == pytest.approx(25.0)


def test_daily_trade_limit_max_two_per_pair():
    """Pair is blocked after 2 trades on the same UTC day."""
    rm = RiskManager(RiskParameters(max_daily_trades_per_pair=2))
    pair = "BTC/USDT"
    today = "2026-09-13"

    can_open, _ = rm.can_open_trade(pair, today)
    assert can_open is True

    # First trade opened and closed
    pos1 = ActivePosition(
        pair=pair,
        direction=SignalType.LONG,
        entry_price=100.0,
        size=10.0,
        initial_stop_loss=95.0,
        current_stop_loss=95.0,
        tp1=105.0,
        tp2=110.0,
        remaining_size=10.0,
        tp1_hit=False,
        entry_timestamp=today,
        state=TradeState.OPEN,
    )
    rm.record_trade_opened(pos1, today)
    rm.active_positions.pop(pair)  # simulate closed

    can_open, _ = rm.can_open_trade(pair, today)
    assert can_open is True

    # Second trade opened and closed
    pos2 = ActivePosition(
        pair=pair,
        direction=SignalType.SHORT,
        entry_price=100.0,
        size=10.0,
        initial_stop_loss=105.0,
        current_stop_loss=105.0,
        tp1=95.0,
        tp2=90.0,
        remaining_size=10.0,
        tp1_hit=False,
        entry_timestamp=today,
        state=TradeState.OPEN,
    )
    rm.record_trade_opened(pos2, today)
    rm.active_positions.pop(pair)  # simulate closed

    # Third trade attempt must be rejected
    can_open, reason = rm.can_open_trade(pair, today)
    assert can_open is False
    assert "Daily trade limit reached" in reason

    # Next UTC day trade should be allowed
    can_open_next_day, _ = rm.can_open_trade(pair, "2026-09-14")
    assert can_open_next_day is True


def test_cooldown_after_stop_loss():
    """Stop-loss hit triggers 8-candle cooldown."""
    rm = RiskManager(RiskParameters(cooldown_candles=8))
    pair = "ETH/USDT"
    today = "2026-09-13"

    pos = ActivePosition(
        pair=pair,
        direction=SignalType.LONG,
        entry_price=100.0,
        size=5.0,
        initial_stop_loss=95.0,
        current_stop_loss=95.0,
        tp1=105.0,
        tp2=110.0,
        remaining_size=5.0,
        tp1_hit=False,
        entry_timestamp=today,
        state=TradeState.OPEN,
    )
    rm.record_trade_opened(pos, today)
    rm.record_stop_loss_hit(pair)

    assert rm.cooldowns[pair] == 8
    can_open, reason = rm.can_open_trade(pair, today)
    assert can_open is False
    assert "in cooldown" in reason

    # Elapse 8 candles
    for _ in range(8):
        rm.on_candle_close(pair)

    assert pair not in rm.cooldowns
    can_open, _ = rm.can_open_trade(pair, today)
    assert can_open is True


def test_stop_loss_tightening_only():
    """SL can only tighten or stay the same, never widen."""
    rm = RiskManager()
    pair = "SOL/USDT"
    pos = ActivePosition(
        pair=pair,
        direction=SignalType.LONG,
        entry_price=100.0,
        size=1.0,
        initial_stop_loss=90.0,
        current_stop_loss=90.0,
        tp1=110.0,
        tp2=120.0,
        remaining_size=1.0,
        tp1_hit=False,
        entry_timestamp="2026-09-13",
        state=TradeState.OPEN,
    )
    rm.active_positions[pair] = pos

    # For LONG: tighten to 92.0 (higher) -> should update
    updated_sl = rm.update_stop_loss_tightening(pair, 92.0)
    assert updated_sl == 92.0

    # Attempt to widen to 85.0 (lower) -> should be ignored
    updated_sl = rm.update_stop_loss_tightening(pair, 85.0)
    assert updated_sl == 92.0


def test_tp1_hit_moves_stop_to_break_even_and_closes_half():
    """When TP1 is hit, close 50% and move SL to entry price."""
    rm = RiskManager()
    pair = "BTC/USDT"
    pos = ActivePosition(
        pair=pair,
        direction=SignalType.LONG,
        entry_price=100.0,
        size=10.0,
        initial_stop_loss=90.0,
        current_stop_loss=90.0,
        tp1=105.0,
        tp2=115.0,
        remaining_size=10.0,
        tp1_hit=False,
        entry_timestamp="2026-09-13",
        state=TradeState.OPEN,
    )
    rm.active_positions[pair] = pos

    # Candle reaches TP1
    state, closed_qty = rm.evaluate_tp_and_sl(pair, current_high=106.0, current_low=101.0)
    assert state == TradeState.TP1_HIT
    assert closed_qty == 5.0
    assert pos.remaining_size == 5.0
    assert pos.current_stop_loss == 100.0  # Break-even!


def test_state_serialization_and_restore():
    """Verify risk manager serialize to dict and restore."""
    rm1 = RiskManager()
    pair = "BTC/USDT"
    pos = ActivePosition(
        pair=pair,
        direction=SignalType.SHORT,
        entry_price=100.0,
        size=4.0,
        initial_stop_loss=110.0,
        current_stop_loss=110.0,
        tp1=95.0,
        tp2=85.0,
        remaining_size=4.0,
        tp1_hit=False,
        entry_timestamp="2026-09-13",
        state=TradeState.OPEN,
    )
    rm1.record_trade_opened(pos, "2026-09-13")
    rm1.cooldowns["ETH/USDT"] = 4

    data = rm1.to_dict()

    rm2 = RiskManager()
    rm2.from_dict(data)

    assert rm2.cooldowns["ETH/USDT"] == 4
    assert rm2.daily_trades[pair]["2026-09-13"] == 1
    assert pair in rm2.active_positions
    assert rm2.active_positions[pair].direction == SignalType.SHORT

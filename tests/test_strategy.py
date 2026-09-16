"""Tests for strategy mathematical invariants, fib levels, zones, and signals."""

import numpy as np
import pandas as pd
import pytest

from src.strategy import (
    DealingRange,
    FibonacciZones,
    SignalType,
    calculate_atr,
    calculate_rsi,
    calculate_stops,
    calculate_working_range,
    calculate_zones,
    detect_pivots,
    evaluate_signal,
    fib,
    is_trade_invalidated_by_range,
)


def test_fib_zero_and_one_boundaries():
    """fib(0) must equal range_low, fib(1) must equal range_high."""
    for base in [10.0, 50.0, 1000.0]:
        span = 25.0
        low = base
        high = base + span

        assert pytest.approx(fib(low, high, 0.0)) == low
        assert pytest.approx(fib(low, high, 1.0)) == high


def test_poi_short_closer_to_range_high_than_poi_long():
    """POI_SHORT is always closer to range_high than POI_LONG."""
    low = 100.0
    high = 200.0
    zones = calculate_zones(low, high)

    dist_poi_short_to_high = high - zones.poi_short[1]
    dist_poi_long_to_high = high - zones.poi_long[1]

    assert dist_poi_short_to_high < dist_poi_long_to_high

    # Distance to low: POI_LONG is closer to range_low than POI_SHORT
    dist_poi_long_to_low = zones.poi_long[0] - low
    dist_poi_short_to_low = zones.poi_short[0] - low
    assert dist_poi_long_to_low < dist_poi_short_to_low


def test_range_doubling_doubles_zone_widths_proportionally():
    """When dealing range doubles, POI zone widths double proportionally."""
    low1 = 100.0
    high1 = 200.0
    zones1 = calculate_zones(low1, high1)

    width_long1 = zones1.poi_long[1] - zones1.poi_long[0]
    width_short1 = zones1.poi_short[1] - zones1.poi_short[0]
    width_mid1 = zones1.mid[1] - zones1.mid[0]

    # Double the range size
    low2 = 100.0
    high2 = 300.0
    zones2 = calculate_zones(low2, high2)

    width_long2 = zones2.poi_long[1] - zones2.poi_long[0]
    width_short2 = zones2.poi_short[1] - zones2.poi_short[0]
    width_mid2 = zones2.mid[1] - zones2.mid[0]

    assert pytest.approx(width_long2) == 2.0 * width_long1
    assert pytest.approx(width_short2) == 2.0 * width_short1
    assert pytest.approx(width_mid2) == 2.0 * width_mid1


def test_mid_range_gives_no_signal():
    """Price action strictly inside the MID zone must never generate a signal."""
    low = 100.0
    high = 200.0
    zones = calculate_zones(low, high)
    dr = DealingRange(
        range_high=high,
        range_low=low,
        is_valid=True,
        range_size=high - low,
        atr_4h=5.0,
        pivot_high=high,
        pivot_low=low,
    )

    # Candle entirely within MID zone [0.382, 0.786]
    mid_low = zones.mid[0] + 5.0
    mid_high = zones.mid[1] - 5.0
    mid_open = mid_low + 2.0
    mid_close = mid_high - 2.0

    # Test bullish in mid with long-favorable RSI
    res_bullish = evaluate_signal(
        candle_open=mid_open,
        candle_high=mid_high,
        candle_low=mid_low,
        candle_close=mid_close,
        rsi_val=40.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res_bullish.signal == SignalType.NONE

    # Test bearish in mid with short-favorable RSI
    res_bearish = evaluate_signal(
        candle_open=mid_close,
        candle_high=mid_high,
        candle_low=mid_low,
        candle_close=mid_open,
        rsi_val=60.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res_bearish.signal == SignalType.NONE


def test_long_and_short_cannot_be_open_simultaneously():
    """Long and short cannot be open or generated simultaneously."""
    low = 100.0
    high = 200.0
    zones = calculate_zones(low, high)
    dr = DealingRange(
        range_high=high,
        range_low=low,
        is_valid=True,
        range_size=high - low,
        atr_4h=5.0,
        pivot_high=high,
        pivot_low=low,
    )

    # Massive volatile candle that touches BOTH POI_LONG and POI_SHORT
    res = evaluate_signal(
        candle_open=low,
        candle_high=high,
        candle_low=low,
        candle_close=high,
        rsi_val=50.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res.signal == SignalType.NONE
    assert res.reason == "Simultaneous POI touch"

    # If already in open position, signal must be NONE
    res_with_pos = evaluate_signal(
        candle_open=zones.poi_long[0],
        candle_high=zones.poi_long[1],
        candle_low=zones.poi_long[0],
        candle_close=zones.poi_long[1],
        rsi_val=40.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=True,
        in_cooldown=False,
    )
    assert res_with_pos.signal == SignalType.NONE
    assert res_with_pos.reason == "Position already open"


def test_long_signal_valid_flow():
    """Valid LONG signal when all conditions are satisfied."""
    low = 100.0
    high = 200.0
    zones = calculate_zones(low, high)
    dr = DealingRange(
        range_high=high,
        range_low=low,
        is_valid=True,
        range_size=high - low,
        atr_4h=5.0,
        pivot_high=high,
        pivot_low=low,
    )

    c_open = zones.poi_long[0] + 1.0
    c_close = zones.poi_long[1] - 1.0
    c_low = zones.poi_long[0] + 0.5
    c_high = zones.poi_long[1]

    res = evaluate_signal(
        candle_open=c_open,
        candle_high=c_high,
        candle_low=c_low,
        candle_close=c_close,
        rsi_val=45.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res.signal == SignalType.LONG
    assert res.tp1 == pytest.approx(fib(low, high, 0.50))
    assert res.tp2 == pytest.approx(zones.tp2_long)
    expected_stop = min(low, zones.poi_long[0]) - 0.25 * 2.0
    assert res.stop_loss == pytest.approx(expected_stop)


def test_short_signal_valid_flow():
    """Valid SHORT signal when all conditions are satisfied."""
    low = 100.0
    high = 200.0
    zones = calculate_zones(low, high)
    dr = DealingRange(
        range_high=high,
        range_low=low,
        is_valid=True,
        range_size=high - low,
        atr_4h=5.0,
        pivot_high=high,
        pivot_low=low,
    )

    c_open = zones.poi_short[1] - 1.0
    c_close = zones.poi_short[0] + 1.0
    c_high = zones.poi_short[1]
    c_low = zones.poi_short[0] - 0.5

    res = evaluate_signal(
        candle_open=c_open,
        candle_high=c_high,
        candle_low=c_low,
        candle_close=c_close,
        rsi_val=55.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=2.0,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res.signal == SignalType.SHORT
    assert res.tp1 == pytest.approx(fib(low, high, 0.50))
    assert res.tp2 == pytest.approx(zones.tp2_short)
    expected_stop = max(high, zones.poi_short[1]) + 0.25 * 2.0
    assert res.stop_loss == pytest.approx(expected_stop)


def test_invalid_range_filters_signals():
    """When range is invalid (size < 1.5 * ATR), no signals generated."""
    dr = DealingRange(
        range_high=102.0,
        range_low=100.0,
        is_valid=False,
        range_size=2.0,
        atr_4h=5.0,
        pivot_high=102.0,
        pivot_low=100.0,
    )
    zones = calculate_zones(100.0, 102.0)
    res = evaluate_signal(
        candle_open=100.2,
        candle_high=100.7,
        candle_low=100.1,
        candle_close=100.6,
        rsi_val=40.0,
        dealing_range=dr,
        zones=zones,
        atr_exec=0.5,
        has_open_position=False,
        in_cooldown=False,
    )
    assert res.signal == SignalType.NONE
    assert res.reason == "Range invalid"


def test_detect_pivots_logic():
    """Pivots require 10 candles left and 10 candles right."""
    n = 35
    lows = [50.0] * n
    highs = [100.0] * n

    # Create a pivot high at index 15 (left=10, right=10 -> confirmed if n >= 26)
    highs[15] = 120.0
    # Create a pivot low at index 18
    lows[18] = 30.0

    df = pd.DataFrame({"high": highs, "low": lows, "close": highs, "open": lows})
    p_high, p_low = detect_pivots(df, left=10, right=10)

    assert p_high == 120.0
    assert p_low == 30.0


def test_trade_invalidation_by_range():
    """Trade is invalidated when price breaks range against position."""
    assert is_trade_invalidated_by_range(SignalType.LONG, 99.0, 100.0, 200.0) is True
    assert is_trade_invalidated_by_range(SignalType.LONG, 101.0, 100.0, 200.0) is False
    assert is_trade_invalidated_by_range(SignalType.SHORT, 201.0, 100.0, 200.0) is True
    assert is_trade_invalidated_by_range(SignalType.SHORT, 199.0, 100.0, 200.0) is False

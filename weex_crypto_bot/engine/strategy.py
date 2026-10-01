"""Dynamic Dealing Range and Fibonacci POI Strategy.

Pure mathematical functions for calculating pivots, ranges, Fibonacci levels,
points of interest (POI), signals, stop-loss levels, and take-profit targets.
All price levels are computed dynamically from candle swings without any hardcoded prices.
"""

from dataclasses import dataclass
from enum import Enum
import numpy as np
import pandas as pd

# Strategy Constants (Ratios, Multipliers and Lookbacks only)
PIVOT_LOOKBACK_LEFT: int = 10
PIVOT_LOOKBACK_RIGHT: int = 10
ATR_PERIOD: int = 14
RSI_PERIOD: int = 14
COOLDOWN_CANDLES_COUNT: int = 8

MIN_RANGE_ATR_MULTIPLIER: float = 1.5
STOP_ATR_MULTIPLIER: float = 0.25

FIB_POI_LONG_LOWER: float = 0.236
FIB_POI_LONG_UPPER: float = 0.382
FIB_MID_LOWER: float = 0.382
FIB_MID_UPPER: float = 0.786
FIB_POI_SHORT_LOWER: float = 0.786
FIB_POI_SHORT_UPPER: float = 0.886

FIB_TP1_RATIO: float = 0.50
FIB_TP2_LONG_RATIO: float = 0.786
FIB_TP2_SHORT_RATIO: float = 0.236
TP1_POSITION_FRACTION: float = 0.50

RSI_LONG_MIN: float = 30.0
RSI_LONG_MAX: float = 55.0
RSI_SHORT_MIN: float = 45.0
RSI_SHORT_MAX: float = 70.0


class SignalType(str, Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class DealingRange:
    range_high: float
    range_low: float
    is_valid: bool
    range_size: float
    atr_4h: float
    pivot_high: float | None
    pivot_low: float | None


@dataclass(frozen=True)
class FibonacciZones:
    poi_long: tuple[float, float]
    poi_short: tuple[float, float]
    mid: tuple[float, float]
    tp1: float
    tp2_long: float
    tp2_short: float


@dataclass(frozen=True)
class SignalResult:
    signal: SignalType
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    reason: str


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate Average True Range (ATR) using standard Wilder smoothing."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.Series:
    """Calculate Relative Strength Index (RSI) using standard Wilder smoothing."""
    close = df["close"]
    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.fillna(50.0)
    return rsi


def detect_pivots(
    df_4h: pd.DataFrame,
    left: int = PIVOT_LOOKBACK_LEFT,
    right: int = PIVOT_LOOKBACK_RIGHT,
) -> tuple[float | None, float | None]:
    """Detect the most recent confirmed pivot high and pivot low on 4H candles.

    A pivot at candle index i is valid only when:
    - left completed candles before i and right completed candles after i are present.
    - index i is greater than all candles in [i - left, i + right] for high (or lower for low).
    - Scan backwards from the latest confirmed candle (N - 1 - right) to find the latest pivots.
    """
    n = len(df_4h)
    min_required = left + right + 1
    if n < min_required:
        return None, None

    highs = df_4h["high"].to_numpy()
    lows = df_4h["low"].to_numpy()

    latest_pivot_high: float | None = None
    latest_pivot_low: float | None = None

    # Latest confirmed candle index is n - 1 - right
    max_test_idx = n - 1 - right

    for i in range(max_test_idx, left - 1, -1):
        if latest_pivot_high is None:
            val = highs[i]
            left_window = highs[i - left : i]
            right_window = highs[i + 1 : i + right + 1]
            if np.all(val > left_window) and np.all(val >= right_window):
                latest_pivot_high = float(val)

        if latest_pivot_low is None:
            val = lows[i]
            left_window = lows[i - left : i]
            right_window = lows[i + 1 : i + right + 1]
            if np.all(val < left_window) and np.all(val <= right_window):
                latest_pivot_low = float(val)

        if latest_pivot_high is not None and latest_pivot_low is not None:
            break

    return latest_pivot_high, latest_pivot_low


def calculate_working_range(
    pivot_high: float | None,
    pivot_low: float | None,
    current_high: float,
    current_low: float,
    atr_4h: float,
) -> DealingRange:
    """Compute the dynamic working range and determine validity.

    range_high = max(latest_confirmed_pivot_high, current_4h_high)
    range_low  = min(latest_confirmed_pivot_low, current_4h_low)
    Invalid if:
    - pivot_high or pivot_low is missing
    - range_high <= range_low
    - range_size < 1.5 * ATR(14) on 4H
    """
    if pivot_high is None or pivot_low is None:
        return DealingRange(
            range_high=0.0,
            range_low=0.0,
            is_valid=False,
            range_size=0.0,
            atr_4h=atr_4h,
            pivot_high=pivot_high,
            pivot_low=pivot_low,
        )

    range_high = max(float(pivot_high), float(current_high))
    range_low = min(float(pivot_low), float(current_low))
    range_size = range_high - range_low

    is_valid = True
    if range_high <= range_low:
        is_valid = False
    elif range_size < (MIN_RANGE_ATR_MULTIPLIER * atr_4h):
        is_valid = False

    return DealingRange(
        range_high=range_high,
        range_low=range_low,
        is_valid=is_valid,
        range_size=range_size,
        atr_4h=atr_4h,
        pivot_high=pivot_high,
        pivot_low=pivot_low,
    )


def fib(range_low: float, range_high: float, ratio: float) -> float:
    """Calculate dynamic Fibonacci level: range_low + ratio * (range_high - range_low)."""
    return range_low + ratio * (range_high - range_low)


def calculate_zones(range_low: float, range_high: float) -> FibonacciZones:
    """Compute POI and target zones from the working range."""
    poi_long = (
        fib(range_low, range_high, FIB_POI_LONG_LOWER),
        fib(range_low, range_high, FIB_POI_LONG_UPPER),
    )
    poi_short = (
        fib(range_low, range_high, FIB_POI_SHORT_LOWER),
        fib(range_low, range_high, FIB_POI_SHORT_UPPER),
    )
    mid = (
        fib(range_low, range_high, FIB_MID_LOWER),
        fib(range_low, range_high, FIB_MID_UPPER),
    )
    tp1 = fib(range_low, range_high, FIB_TP1_RATIO)
    tp2_long = fib(range_low, range_high, FIB_TP2_LONG_RATIO)
    tp2_short = fib(range_low, range_high, FIB_TP2_SHORT_RATIO)

    return FibonacciZones(
        poi_long=poi_long,
        poi_short=poi_short,
        mid=mid,
        tp1=tp1,
        tp2_long=tp2_long,
        tp2_short=tp2_short,
    )


def calculate_stops(
    direction: SignalType,
    range_low: float,
    range_high: float,
    zones: FibonacciZones,
    atr_exec: float,
) -> float:
    """Calculate dynamic stop loss.

    LONG stop  = min(range_low, bottom of POI_LONG) - 0.25 * ATR(14 exec)
    SHORT stop = max(range_high, top of POI_SHORT) + 0.25 * ATR(14 exec)
    """
    buffer_amt = STOP_ATR_MULTIPLIER * atr_exec
    if direction == SignalType.LONG:
        bottom_poi_long = zones.poi_long[0]
        base_level = min(range_low, bottom_poi_long)
        return base_level - buffer_amt
    elif direction == SignalType.SHORT:
        top_poi_short = zones.poi_short[1]
        base_level = max(range_high, top_poi_short)
        return base_level + buffer_amt
    raise ValueError("Invalid direction for stop calculation")


def evaluate_signal(
    candle_open: float,
    candle_high: float,
    candle_low: float,
    candle_close: float,
    rsi_val: float,
    dealing_range: DealingRange,
    zones: FibonacciZones,
    atr_exec: float,
    has_open_position: bool,
    in_cooldown: bool,
) -> SignalResult:
    """Evaluate execution candle for entry signals.

    LONG conditions (ALL must be true):
    - candle touches POI_LONG: low <= fib(0.382) and high >= fib(0.236)
    - bullish candle: close > open
    - RSI between 30 and 55
    - no open position
    - range valid
    - not in cooldown
    - not simultaneously touching POI_SHORT

    SHORT conditions (ALL must be true):
    - candle touches POI_SHORT: high >= fib(0.786) and low <= fib(0.886)
    - bearish candle: close < open
    - RSI between 45 and 70
    - no open position
    - range valid
    - not in cooldown
    - not simultaneously touching POI_LONG
    """
    if not dealing_range.is_valid:
        return SignalResult(SignalType.NONE, 0.0, 0.0, 0.0, 0.0, "Range invalid")

    if has_open_position:
        return SignalResult(SignalType.NONE, 0.0, 0.0, 0.0, 0.0, "Position already open")

    if in_cooldown:
        return SignalResult(SignalType.NONE, 0.0, 0.0, 0.0, 0.0, "In cooldown")

    touches_poi_long = (
        candle_low <= zones.poi_long[1] and candle_high >= zones.poi_long[0]
    )
    touches_poi_short = (
        candle_high >= zones.poi_short[0] and candle_low <= zones.poi_short[1]
    )

    # Exclude simultaneous touch of both POIs
    if touches_poi_long and touches_poi_short:
        return SignalResult(
            SignalType.NONE, 0.0, 0.0, 0.0, 0.0, "Simultaneous POI touch"
        )

    # Evaluate LONG
    if touches_poi_long:
        bullish = candle_close > candle_open
        rsi_ok = RSI_LONG_MIN <= rsi_val <= RSI_LONG_MAX
        if bullish and rsi_ok:
            stop = calculate_stops(
                SignalType.LONG,
                dealing_range.range_low,
                dealing_range.range_high,
                zones,
                atr_exec,
            )
            return SignalResult(
                signal=SignalType.LONG,
                entry_price=candle_close,
                stop_loss=stop,
                tp1=zones.tp1,
                tp2=zones.tp2_long,
                reason="POI_LONG touched, bullish candle, RSI within bounds",
            )

    # Evaluate SHORT
    if touches_poi_short:
        bearish = candle_close < candle_open
        rsi_ok = RSI_SHORT_MIN <= rsi_val <= RSI_SHORT_MAX
        if bearish and rsi_ok:
            stop = calculate_stops(
                SignalType.SHORT,
                dealing_range.range_low,
                dealing_range.range_high,
                zones,
                atr_exec,
            )
            return SignalResult(
                signal=SignalType.SHORT,
                entry_price=candle_close,
                stop_loss=stop,
                tp1=zones.tp1,
                tp2=zones.tp2_short,
                reason="POI_SHORT touched, bearish candle, RSI within bounds",
            )

    return SignalResult(SignalType.NONE, 0.0, 0.0, 0.0, 0.0, "No signal conditions met")


def is_trade_invalidated_by_range(
    direction: SignalType,
    candle_close: float,
    range_low: float,
    range_high: float,
) -> bool:
    """Check if the dealing range broke against the open trade position.

    If a range breaks against the position, close on the close of the signal candle.
    For LONG: closed below range_low.
    For SHORT: closed above range_high.
    """
    if direction == SignalType.LONG:
        return candle_close < range_low
    elif direction == SignalType.SHORT:
        return candle_close > range_high
    return False

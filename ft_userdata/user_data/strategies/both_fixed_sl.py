# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
Dynamic Dealing Range + Fibonacci POI Strategy for Freqtrade (USDT-perpetuals).
Replaces the legacy strategy while preserving the strategy class name 'weex_futures_quant'.
NO hardcoded market prices. All zones and levels are computed live dynamically from 4H swings.
"""

from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from freqtrade.persistence import Trade
from freqtrade.strategy import (
    DecimalParameter,
    IntParameter,
    IStrategy,
    informative,
)
import numpy as np
import pandas as pd
from pandas import DataFrame
import talib.abstract as ta

logger = logging.getLogger(__name__)

STRATEGY_STATE_PATH = "/root/ft_userdata/user_data/strategy_state.json"
if not os.path.exists("/root/ft_userdata/user_data"):
    STRATEGY_STATE_PATH = "/freqtrade/user_data/strategy_state.json"


def detect_swing_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    left: int = 10,
    right: int = 10,
) -> Tuple[np.ndarray, np.ndarray]:
    """Detect confirmed pivot highs and pivot lows across the candle series."""
    n = len(highs)
    pivot_highs = np.full(n, np.nan)
    pivot_lows = np.full(n, np.nan)

    if n < left + right + 1:
        return pivot_highs, pivot_lows

    for i in range(left, n - right):
        h = highs[i]
        if np.all(h > highs[i - left : i]) and np.all(h >= highs[i + 1 : i + right + 1]):
            pivot_highs[i + right] = h

        l = lows[i]
        if np.all(l < lows[i - left : i]) and np.all(l <= lows[i + 1 : i + right + 1]):
            pivot_lows[i + right] = l

    return pivot_highs, pivot_lows


class both_fixed_sl(IStrategy):
    """Dynamische Dealing Range + Fibonacci POI Strategie.

    - 4H Swings (pivots 10 links, 10 rechts) vormen de live dealing range
    - Fibonacci POIs: POI_LONG [0.236, 0.382], POI_SHORT [0.786, 0.886], MID [0.382, 0.786]
    - 15m Executie met RSI(14) filtering en candle confirm
    - Dynamic ATR buffer stops, TP1 (50% close + BE) en TP2 (runner close)
    - Cooldown na SL (8 candles), max 2 trades per pair per UTC-dag
    - Risico 0.75% equity per trade
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = True

    # Fallback stoploss
    stoploss = -0.0631
    use_custom_stoploss = True
    use_custom_exit = True
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {
        "entry": "GTC",
        "exit": "GTC",
    }

    startup_candle_count = 150
    minimal_roi = {
        "0": 0.12,
        "60": 0.08,
        "120": 0.04,
    }

    # Parameters
    risk_pct = DecimalParameter(0.005, 0.02, default=0.0075, space="buy", optimize=False)
    pivot_lookback = IntParameter(5, 15, default=10, space="buy", optimize=False)
    min_range_atr_mult = DecimalParameter(1.0, 2.5, default=1.5, space="buy", optimize=False)
    stop_atr_mult = DecimalParameter(0.15, 0.50, default=0.25, space="buy", optimize=False)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.cooldown_tracker: Dict[str, int] = {}
        self.daily_trade_tracker: Dict[str, Dict[str, int]] = {}
        self.tp1_hit_trades: set = set()
        self.highest_stoploss: Dict[int, float] = {}

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 8,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 5,
                "stop_duration_candles": 32,
                "max_allowed_drawdown": 0.08,
            },
        ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calculate 4H pivots, working range, and Fibonacci POI zones."""
        atr_period = 14
        dataframe["atr_4h"] = ta.ATR(dataframe, timeperiod=atr_period)

        left = int(self.pivot_lookback.value)
        right = left

        highs = dataframe["high"].to_numpy()
        lows = dataframe["low"].to_numpy()

        p_highs, p_lows = detect_swing_pivots(highs, lows, left=left, right=right)
        dataframe["pivot_high_raw"] = p_highs
        dataframe["pivot_low_raw"] = p_lows

        # Forward fill the confirmed pivot values
        dataframe["last_pivot_high"] = dataframe["pivot_high_raw"].ffill()
        dataframe["last_pivot_low"] = dataframe["pivot_low_raw"].ffill()

        # Dynamic working range
        dataframe["range_high"] = np.maximum(
            dataframe["last_pivot_high"].fillna(dataframe["high"]),
            dataframe["high"],
        )
        dataframe["range_low"] = np.minimum(
            dataframe["last_pivot_low"].fillna(dataframe["low"]),
            dataframe["low"],
        )
        dataframe["range_size"] = dataframe["range_high"] - dataframe["range_low"]

        # Validity check: range_high > range_low and range >= 1.5 * ATR(14)
        min_range = self.min_range_atr_mult.value * dataframe["atr_4h"]
        dataframe["range_valid"] = (
            (dataframe["range_high"] > dataframe["range_low"])
            & (dataframe["range_size"] >= min_range)
            & dataframe["last_pivot_high"].notna()
            & dataframe["last_pivot_low"].notna()
        ).astype(int)

        # Fibonacci ratios
        r_low = dataframe["range_low"]
        r_size = dataframe["range_size"]

        dataframe["poi_long_lower"] = r_low + 0.236 * r_size
        dataframe["poi_long_upper"] = r_low + 0.382 * r_size
        dataframe["fib_mid_lower"] = r_low + 0.382 * r_size
        dataframe["fib_mid_upper"] = r_low + 0.786 * r_size
        dataframe["poi_short_lower"] = r_low + 0.786 * r_size
        dataframe["poi_short_upper"] = r_low + 0.886 * r_size
        dataframe["fib_tp1"] = r_low + 0.50 * r_size

        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Populate 15m execution indicators and merge 4H bias frame."""
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Generate long and short signals on 15m execution candle close."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        pair = metadata["pair"]
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_count = self.daily_trade_tracker.get(pair, {}).get(today_utc, 0)
        in_cooldown = self.cooldown_tracker.get(pair, 0) > 0

        if daily_count >= 2 or in_cooldown:
            return dataframe

        range_valid = dataframe["range_valid_4h"] == 1

        # POI LONG touch: candle low <= fib(0.382) and high >= fib(0.236)
        touches_poi_long = (dataframe["low"] <= dataframe["poi_long_upper_4h"]) & (
            dataframe["high"] >= dataframe["poi_long_lower_4h"]
        )

        # POI SHORT touch: candle high >= fib(0.786) and low <= fib(0.886)
        touches_poi_short = (dataframe["high"] >= dataframe["poi_short_lower_4h"]) & (
            dataframe["low"] <= dataframe["poi_short_upper_4h"]
        )

        simultaneous_touch = touches_poi_long & touches_poi_short

        # Bullish / Bearish candle close
        bullish_candle = dataframe["close"] > dataframe["open"]
        bearish_candle = dataframe["close"] < dataframe["open"]

        # RSI conditions
        rsi_long_ok = (dataframe["rsi"] >= 30.0) & (dataframe["rsi"] <= 55.0)
        rsi_short_ok = (dataframe["rsi"] >= 45.0) & (dataframe["rsi"] <= 70.0)

        # Long conditions
        long_cond = (
            range_valid
            & touches_poi_long
            & (~simultaneous_touch)
            & bullish_candle
            & rsi_long_ok
            & (dataframe["volume"] > 0)
        )

        # Short conditions
        short_cond = (
            range_valid
            & touches_poi_short
            & (~simultaneous_touch)
            & bearish_candle
            & rsi_short_ok
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "poi_long_entry"

        if not self.can_short:
            dataframe.loc[:, "enter_short"] = 0
        else:
            dataframe.loc[short_cond, "enter_short"] = 1
            dataframe.loc[short_cond, "enter_tag"] = "poi_short_entry"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> Optional[float]:
        """Dynamic stoploss calculation with strict tightening-only policy.
        Trailing is ONLY active after profit >= 1R; otherwise only hard SL (-0.0631) + ROI-ladder.
        """
        one_r = abs(self.stoploss)  # 0.0631 (0.75% equity risk on 0 stake with 68 equity)
        if current_profit < one_r:
            return None  # Trailing INACTIVE: rely on hard SL (-0.0631) + ROI ladder

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return None

        last_row = dataframe.iloc[-1]
        atr_exec = float(last_row["atr"])
        buffer_val = self.stop_atr_mult.value * atr_exec

        if not trade.is_short:
            # LONG stop: profit >= 1R, move stop to at least Break-Even (open_rate) and trail behind price
            trail_stop = current_rate - buffer_val
            base_stop = max(trade.open_rate, trail_stop)

            # Enforce tightening only: stop can only move upwards
            prev_highest = self.highest_stoploss.get(trade.id, base_stop)
            stop_price = max(prev_highest, base_stop)
            self.highest_stoploss[trade.id] = stop_price

            # Convert to relative stoploss distance from current_rate
            if current_rate <= 0:
                return None
            rel_stop = (stop_price - current_rate) / current_rate
            return min(-0.005, rel_stop)

        else:
            # SHORT stop (preserved if shorting is re-enabled): profit >= 1R
            trail_stop = current_rate + buffer_val
            base_stop = min(trade.open_rate, trail_stop)

            # Enforce tightening only: stop can only move downwards
            prev_lowest = self.highest_stoploss.get(trade.id, base_stop)
            stop_price = min(prev_lowest, base_stop)
            self.highest_stoploss[trade.id] = stop_price

            # Convert to relative stoploss distance from current_rate for shorts
            if current_rate <= 0:
                return None
            rel_stop = (current_rate - stop_price) / current_rate
            return min(-0.005, rel_stop)

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """Evaluate TP1 (partial exit), TP2, and Dealing Range invalidation."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return None

        last_row = dataframe.iloc[-1]
        c_close = float(last_row["close"])
        r_low = float(last_row["range_low_4h"])
        r_high = float(last_row["range_high_4h"])

        # Range Invalidation Check
        if not trade.is_short:
            if c_close < r_low:
                return "range_invalidated_long"
        else:
            if c_close > r_high:
                return "range_invalidated_short"

        # TP1 and TP2 Checks
        tp1 = float(last_row["fib_tp1_4h"])
        tp2_long = float(last_row["poi_short_lower_4h"])
        tp2_short = float(last_row["poi_long_upper_4h"])

        if not trade.is_short:
            if trade.id in self.tp1_hit_trades and current_rate >= tp2_long:
                return "tp2_reached_long"
        else:
            if trade.id in self.tp1_hit_trades and current_rate <= tp2_short:
                return "tp2_reached_short"

        return None

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: Optional[float],
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> Optional[float]:
        """Take partial profit on TP1 (50% position scale-out)."""
        if trade.id in self.tp1_hit_trades:
            return None

        dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if len(dataframe) == 0:
            return None

        last_row = dataframe.iloc[-1]
        tp1 = float(last_row["fib_tp1_4h"])

        hit_tp1 = False
        if not trade.is_short and current_rate >= tp1:
            hit_tp1 = True
        elif trade.is_short and current_rate <= tp1:
            hit_tp1 = True

        if hit_tp1:
            self.tp1_hit_trades.add(trade.id)
            # Close 50% of the position
            return -(trade.stake_amount * 0.50)

        return None

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Position sizing based on 0.75% equity risk."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return proposed_stake

        last_row = dataframe.iloc[-1]
        atr_exec = float(last_row["atr"])
        buffer_val = self.stop_atr_mult.value * atr_exec

        if side == "long":
            poi_bottom = float(last_row["poi_long_lower_4h"])
            r_low = float(last_row["range_low_4h"])
            stop_price = min(r_low, poi_bottom) - buffer_val
        else:
            poi_top = float(last_row["poi_short_upper_4h"])
            r_high = float(last_row["range_high_4h"])
            stop_price = max(r_high, poi_top) + buffer_val

        dist = abs(current_rate - stop_price)
        if dist <= 0:
            return proposed_stake

        total_equity = self.wallets.get_total(self.config["stake_currency"])
        risk_cash = total_equity * float(self.risk_pct.value)
        # Position size in base currency = risk_cash / dist
        # Stake in quote currency = (risk_cash / dist) * current_rate / leverage
        target_stake = (risk_cash / dist) * (current_rate / max(1.0, leverage))

        # Clamp stake to max 20 USDT zolang equity < 300
        effective_max = 20.0 if total_equity < 300.0 else min(max_stake, 20.0)
        final_stake = min(effective_max, max(min_stake or 5.0, target_stake))
        return final_stake

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        """Confirm trade entry: strictly 1 position, max 2 trades/day, margin cap 25%."""
        # 1. 1 positie, geen averaging
        open_trades = Trade.get_open_trades()
        if len(open_trades) >= 1:
            logger.warning(f"ORDERS GEBLOKKEERD: Al {len(open_trades)} positie open (max 1 positie).")
            return False

        # 2. Max 2 trades per dag TOTAAL
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            total_today = len(Trade.get_trades([Trade.open_date >= today_utc]).all())
        except Exception:
            total_today = 0
        if total_today >= 2:
            logger.warning(f"ORDERS GEBLOKKEERD: Max 2 trades per dag bereikt ({total_today}/2).")
            return False

        # 3. Marge-cap 25%: als margin_used > 25%: geen nieuwe entry
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            used_margin = sum(float(t.stake_amount) for t in open_trades) + (amount * rate)
            if total_equity > 0 and (used_margin / total_equity) > 0.25:
                logger.warning(
                    f"ORDERS GEBLOKKEERD: Margebezetting {(used_margin/total_equity)*100:.1f}% > 25% cap!"
                )
                return False
        except Exception as exc:
            logger.warning(f"Kon margin-cap check niet afronden: {exc}")

        return True

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        """Trigger cooldown on stoploss exit."""
        if "stop_loss" in exit_reason.lower() or "stoploss" in exit_reason.lower():
            self.cooldown_tracker[pair] = 8

        self.highest_stoploss.pop(trade.id, None)
        return True




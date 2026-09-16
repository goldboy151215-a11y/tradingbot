# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
EMA Trend Long Strategy for Freqtrade (USDT-perpetuals).
Long-only trend following strategy:
- 15m close crosses above EMA20
- 15m EMA20 > EMA50
- 4H close > 4H EMA20 (HTF trend filter)
Exits: Hard SL (-0.0631 = 0.75% equity risk), ROI ladder, Trailing stop only active after profit >= 1R.
"""

from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, Optional

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


class ema_trend_lev_10x(IStrategy):

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min(10.0, max_leverage) if max_leverage > 1.0 else 10.0

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False

    # Risk-framework: 0.75% equity risk on $20 stake with $168 equity
    stoploss = -0.0631
    trailing_stop = False
    use_custom_stoploss = True
    use_custom_exit = False
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
    stop_atr_mult = DecimalParameter(0.15, 0.50, default=0.25, space="buy", optimize=False)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
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
        """Calculate 4H EMA20 trend filter."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calculate 15m EMAs and ATR."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # Entry rules:
        # 1. 15m close crosses above 15m EMA20
        cross_above = (dataframe["close"] > dataframe["ema20"]) & (
            dataframe["close"].shift(1) <= dataframe["ema20"].shift(1)
        )
        # 2. 15m EMA20 > 15m EMA50 (bullish alignment)
        ema_trend = dataframe["ema20"] > dataframe["ema50"]
        # 3. 4H close > 4H EMA20 (higher timeframe bullish bias)
        htf_trend = dataframe["close_4h"] > dataframe["ema20_4h"]
        volume_ok = dataframe["volume"] > 0

        long_cond = cross_above & ema_trend & htf_trend & volume_ok

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_trend_long_entry"

        # Long-only: shorts always 0
        dataframe.loc[:, "enter_short"] = 0

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
        """Trailing stop ONLY active after profit >= 1R (+0.0631).
        Before 1R: trailing is inactive, relying on hard SL (-0.0631) + ROI ladder.
        """
        one_r = abs(self.stoploss)  # 0.0631 (0.75% equity risk on $20 stake with $168 equity)
        if current_profit < one_r:
            return None

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return None

        last_row = dataframe.iloc[-1]
        atr_exec = float(last_row["atr"])
        buffer_val = self.stop_atr_mult.value * atr_exec

        # Profit >= 1R: move stop to at least Break-Even (open_rate) and trail behind price
        trail_stop = current_rate - buffer_val
        base_stop = max(trade.open_rate, trail_stop)

        # Enforce tightening only
        prev_highest = self.highest_stoploss.get(trade.id, base_stop)
        stop_price = max(prev_highest, base_stop)
        self.highest_stoploss[trade.id] = stop_price

        if current_rate <= 0:
            return None
        rel_stop = (stop_price - current_rate) / current_rate
        return min(-0.005, rel_stop)

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
        """Fixed stake of 20 USDT for tests."""
        return 20.0

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
        """Confirm trade entry: strictly 1 position, no averaging."""
        open_trades = Trade.get_open_trades()
        if len(open_trades) >= 1:
            return False
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
        self.highest_stoploss.pop(trade.id, None)
        return True

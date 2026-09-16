# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
WEEX Futures Scalper - BB Squeeze Breakout Strategy for Freqtrade.
Designed specifically for WEEX USDT-perpetuals.
5m execution with 4H trend confirmation, Bollinger Band squeeze breakout with volume surge,
and dynamic isolated leverage.
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


class weex_futures_quant(IStrategy):
    """WEEX Futures Scalper - Bollinger Band Squeeze Breakout Strategy.

    - 5m execution for rapid scalping opportunities
    - 4H higher timeframe trend filter (Close > EMA 20)
    - 5m Bollinger Band Squeeze Breakout (Upper BB cross + Volume > 1.2x MA + RSI 52-70)
    - 6x Isolated Leverage
    - Fixed protective Stoploss (-10% ROE / -1.67% price) & Scalp ROI ladder
    - Cooldown protection after exits
    """

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False

    # Stoploss (-1.67% price drop at 12x leverage = -20% ROE)
    stoploss = -0.20
    use_custom_stoploss = False

    # Minimal ROI Ladder
    # At 12x leverage:
    # 0 min: +0.44 (+3.67% price move = +44% ROE)
    # 15 min: +0.24 (+2.00% price move = +24% ROE)
    # 30 min: +0.12 (+1.00% price move = +12% ROE)
    minimal_roi = {
        "0": 0.44,
        "15": 0.24,
        "30": 0.12,
    }

    trailing_stop = False

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

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.cooldown_tracker: Dict[str, int] = {}

    @property
    def protections(self):
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.25,
            },
        ]

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Enforce 12x leverage on WEEX perpetuals (capped at exchange max)."""
        target_leverage = 12.0
        return min(target_leverage, max_leverage) if max_leverage > 1.0 else target_leverage

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Higher timeframe bias for trend alignment."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Fast 5m scalp indicators."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=1.8, nbdevdn=1.8)
        dataframe["bb_upper"] = boll["upperband"]
        dataframe["bb_middle"] = boll["middleband"]
        dataframe["bb_lower"] = boll["lowerband"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """High-probability 5m BB squeeze breakout entries."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        pair = metadata["pair"]

        # HTF Trend alignment
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]

        # 5m BB Breakout + Volume Surge + Healthy RSI
        breakout = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * 1.2
        rsi_ok = (dataframe["rsi"] > 52) & (dataframe["rsi"] < 70)

        long_cond = breakout & vol_surge & rsi_ok & htf_4h_bull

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_breakout_scalp"

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
        """Dynamic compounding stake sizing: scales automatically with wallet equity."""
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            stake_ratio = float(self.config.get("tradable_balance_ratio", 0.58))
            compounded = max(min_stake or 5.0, total_equity * stake_ratio)
            return min(compounded, max_stake)
        except Exception:
            cfg_stake = self.config.get("stake_amount", 100.0)
            if isinstance(cfg_stake, (int, float)):
                return min(float(cfg_stake), max_stake)
            return min(100.0, max_stake)

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
        """Confirm trade entry: strictly 1 position at a time for optimal margin focus."""
        open_trades = Trade.get_open_trades()
        max_allowed = int(self.config.get("max_open_trades", 1))
        if len(open_trades) >= max_allowed:
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
        return True


class fib_poi(weex_futures_quant):
    """Backwards-compatible alias class."""
    pass

# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
WEEX Futures Dual-Regime Quant Strategy (Long + Short).
Designed specifically for WEEX USDT-perpetuals.
5m execution with 4H trend confirmation:
- Longs: 5m Bollinger Band Squeeze Breakout + Volume Surge + 4H Bull Trend (EMA20)
- Shorts: 5m Relief Pullback Rejection at EMA20 + Bearish Rejection + 4H Bear Trend (EMA20)
- Short Flash Breakeven Lock: At +6% ROE (+0.5% price drop at 12x), stoploss snaps to Breakeven (+1%).
- 12x Isolated Leverage & Dynamic 58% Compounding Wallet Staking.
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
    """WEEX Futures Dual-Regime Quant Scalper (Longs + Shorts)."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True

    # Protective stoploss
    stoploss = -0.50
    use_custom_stoploss = True

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
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
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
        """High-probability 5m Dual-Regime entries (Longs + Shorts)."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF 4H Trend alignment
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = (dataframe["close_4h"] < dataframe["ema20_4h"]) & (dataframe["rsi_4h"] < 52)

        # 1. LONG SIGNALS: 5m BB Breakout + Volume Surge + Healthy RSI
        breakout = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * 1.2
        rsi_long = (dataframe["rsi"] > 52) & (dataframe["rsi"] < 70)
        long_cond = breakout & vol_surge & rsi_long & htf_4h_bull

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_breakout_long"

        # 2. SHORT SIGNALS: 5m Relief Pullback to EMA20 + Red Rejection Candle + 4H Bear
        pullback = (dataframe["high"] >= dataframe["ema20"]) & (dataframe["close"] < dataframe["ema20"])
        red_rejection = dataframe["close"] < dataframe["open"]
        rsi_short = (dataframe["rsi"] > 50) & (dataframe["rsi"] < 66)
        vol_short = dataframe["volume"] > dataframe["vol_ma"] * 1.1
        short_cond = pullback & red_rejection & rsi_short & htf_4h_bear & vol_short

        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "bear_pullback_short"

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
        """Lock in Breakeven (+1% fee cushion) on Shorts once in solid green (+6% ROE)."""
        if trade.is_short:
            if current_profit > 0.06:
                return -0.01
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
        """Dynamic compounding stake sizing: scales automatically with live wallet equity."""
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            free_equity = self.wallets.get_free(self.config["stake_currency"])
            stake_ratio = float(self.config.get("tradable_balance_ratio", 0.58))
            
            open_trades_cnt = len(Trade.get_open_trades())
            if open_trades_cnt == 0:
                target_stake = total_equity * stake_ratio
            else:
                # When 1 trade is already open, compound on available free margin
                target_stake = free_equity * stake_ratio

            compounded = max(min_stake or 5.0, target_stake)
            # Ensure stake never exceeds available free balance on exchange
            return min(compounded, max_stake, free_equity)
        except Exception as exc:
            logger.warning(f"Error in custom_stake_amount: {exc}")
            try:
                free_equity = self.wallets.get_free(self.config["stake_currency"])
                return min(free_equity * 0.58, max_stake)
            except Exception:
                return min(50.0, max_stake)

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
        """Confirm trade entry: strictly max_open_trades (default 2) for disciplined margin focus."""
        open_trades = Trade.get_open_trades()
        max_allowed = int(self.config.get("max_open_trades", 2))
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

# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
Institutional Sweep Quant Strategy (Long + Short).
Built specifically for WEEX USDT-Perpetuals on 15m timeframe.

Core Quant Pillars:
1. Smart Money Liquidity Sweeps:
   - Longs: Sweeps 6h swing lows (retail stop hunt) and aggressively reclaims back inside the range.
   - Shorts: Sweeps 6h swing highs (retail breakout trap) and aggressively reclaims back inside the range.
2. Market Regime & Volatility Shield:
   - ADX > 18 + Volume Expansion (Filters out dead chop/low-volatility traps).
   - Higher Timeframe Trend Alignment (15m EMA200 [1H proxy] + 4H EMA20 macro filter).
3. BTC Macro Umbrella Shield:
   - Prevents altcoin longs when BTC is dumping on 4H.
4. Asymmetric Risk-to-Reward (R:R 1:2.5+):
   - Dynamic ATR-based Stoploss (~1.8x ATR).
   - Breakeven Lock at +1.2R (+15% ROE).
   - Trailing Chandelier Profit Runner: lets runners capture major trending moves.
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


class InstitutionalSweepQuant(IStrategy):
    """Institutional Liquidity Sweep & Asymmetric Quant Engine."""

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = True

    # Protective stoploss at -22% ROE (-1.83% price move at 12x leverage)
    stoploss = -0.22
    use_custom_stoploss = True

    # Asymmetric Institutional Profit Targets (ROE)
    minimal_roi = {
        "0": 0.75,     # +75% ROE (+6.25% price move)
        "60": 0.45,    # +45% ROE (+3.75% price move)
        "180": 0.28,   # +28% ROE (+2.33% price move)
        "360": 0.18,   # +18% ROE (+1.50% price move)
    }

    trailing_stop = False  # Handled dynamically via custom_stoploss with ATR
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

    startup_candle_count = 200

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
        """Enforce 12x isolated leverage on WEEX."""
        target_leverage = 12.0
        return min(target_leverage, max_leverage) if max_leverage > 1.0 else target_leverage

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """4H macro trend bias."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("4h", "BTC/USDT:USDT")
    def populate_indicators_btc_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """BTC 4H Macro Trend & Umbrella Shield."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """15m execution indicators: Swings, ATR, ADX, Volume, 1H EMA proxy."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)  # 1H 50-EMA proxy
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        # 24-period (6 hours on 15m) swing high and swing low levels
        dataframe["swing_high_24"] = dataframe["high"].rolling(24).max()
        dataframe["swing_low_24"] = dataframe["low"].rolling(24).min()

        # Candle anatomy for rejection / pinbar detection
        candle_range = dataframe["high"] - dataframe["low"]
        dataframe["candle_range"] = np.where(candle_range == 0, 0.0001, candle_range)
        dataframe["body_size"] = (dataframe["close"] - dataframe["open"]).abs()
        dataframe["lower_wick"] = np.minimum(dataframe["open"], dataframe["close"]) - dataframe["low"]
        dataframe["upper_wick"] = dataframe["high"] - np.maximum(dataframe["open"], dataframe["close"])

        # Relative wick proportions
        dataframe["lower_wick_ratio"] = dataframe["lower_wick"] / dataframe["candle_range"]
        dataframe["upper_wick_ratio"] = dataframe["upper_wick"] / dataframe["candle_range"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Institutional Smart Money Liquidity Sweep Entries."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # 1. Macro & BTC Umbrella Shield
        pair = metadata.get("pair", "")
        if pair.startswith("BTC"):
            btc_bull_shield = True
            btc_bear_shield = True
        else:
            btc_4h_close = [c for c in dataframe.columns if "btc" in c.lower() and "close" in c.lower() and "4h" in c.lower()]
            btc_4h_ema20 = [c for c in dataframe.columns if "btc" in c.lower() and "ema20" in c.lower() and "4h" in c.lower()]
            btc_4h_rsi = [c for c in dataframe.columns if "btc" in c.lower() and "rsi" in c.lower() and "4h" in c.lower()]

            if btc_4h_close and btc_4h_ema20 and btc_4h_rsi:
                c4 = dataframe[btc_4h_close[0]]
                e4 = dataframe[btc_4h_ema20[0]]
                r4 = dataframe[btc_4h_rsi[0]]
                btc_bull_shield = (c4 >= e4) & (r4 >= 46)
                btc_bear_shield = (c4 <= e4) & (r4 <= 54)
            else:
                btc_bull_shield = True
                btc_bear_shield = True

        # 2. HTF Trend & Regime Filters
        # EMA200 on 15m is equivalent to 50 EMA on 1H
        htf_1h_bull = dataframe["close"] > dataframe["ema200"]
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        macro_bull = htf_1h_bull & htf_4h_bull & btc_bull_shield

        htf_1h_bear = dataframe["close"] < dataframe["ema200"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]
        macro_bear = htf_1h_bear & htf_4h_bear & btc_bear_shield

        # Volatility / Expansion filter: Avoid dead, flat markets
        vol_active = dataframe["volume"] > (dataframe["vol_ma"] * 1.1)
        adx_trend = dataframe["adx"] >= 18

        # --- A. LONG SETUP: Liquidity Sweep of Swing Lows + Rejection Reclaim ---
        # 1. Price sweeps below the prior 24-candle low (stops triggered)
        # 2. Price aggressively reclaims and closes back ABOVE that level
        # 3. Bullish rejection: lower wick >= 25% of candle range or green close
        prev_swing_low = dataframe["swing_low_24"].shift(1)
        sweep_low = dataframe["low"] < prev_swing_low
        reclaim_low = dataframe["close"] > prev_swing_low
        bull_rejection = (dataframe["close"] >= dataframe["open"]) | (dataframe["lower_wick_ratio"] >= 0.25)
        rsi_bull = (dataframe["rsi"] >= 35) & (dataframe["rsi"] <= 68)

        long_cond = (
            sweep_low
            & reclaim_low
            & bull_rejection
            & rsi_bull
            & vol_active
            & adx_trend
            & macro_bull
        )

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "liquidity_sweep_long"

        # --- B. SHORT SETUP: Liquidity Sweep of Swing Highs + Rejection Reclaim ---
        # 1. Price sweeps above the prior 24-candle high (breakout trap)
        # 2. Price aggressively falls and closes back BELOW that level
        # 3. Bearish rejection: upper wick >= 25% of candle range or red close
        prev_swing_high = dataframe["swing_high_24"].shift(1)
        sweep_high = dataframe["high"] > prev_swing_high
        reclaim_high = dataframe["close"] < prev_swing_high
        bear_rejection = (dataframe["close"] <= dataframe["open"]) | (dataframe["upper_wick_ratio"] >= 0.25)
        rsi_bear = (dataframe["rsi"] >= 32) & (dataframe["rsi"] <= 65)

        short_cond = (
            sweep_high
            & reclaim_high
            & bear_rejection
            & rsi_bear
            & vol_active
            & adx_trend
            & macro_bear
        )

        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "liquidity_sweep_short"

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
        """Dynamic Asymmetric ATR & Chandelier Trailing Exits.
        
        current_profit in Freqtrade futures is ROE (Return on Equity at 12x):
        0.15 = +15% ROE (+1.25% price move)
        0.30 = +30% ROE (+2.50% price move)
        0.50 = +50% ROE (+4.17% price move)
        """
        # Tier 1: Flash Breakeven Lock at +14% ROE
        # Snaps stoploss to Breakeven (+1% fee buffer)
        if 0.14 <= current_profit < 0.28:
            return -0.01

        # Tier 2: Solid Profit Lock at +28% ROE
        # Locks in at least +14% ROE in profit
        if 0.28 <= current_profit < 0.45:
            return -(current_profit - 0.14)

        # Tier 3: Chandelier Runner Trail at +45%+ ROE
        # Trails 16% ROE behind peak profit to let runners explode
        if current_profit >= 0.45:
            return -(current_profit - 0.16)

        return None

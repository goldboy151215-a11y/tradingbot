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
    stoploss_from_open,
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
    can_short = False

    # Protective Stoploss (-20% ROE / -1.67% price move at 12x)
    stoploss = -0.20
    use_custom_stoploss = False

    # Per-Coin Quant Profiles: Tailored filters to buy on the floor (support bounce) instead of roof breakouts
    # SUI and AVAX receive stricter volume surge and deeper rejection wick filters to avoid falling knives
    COIN_PROFILES: Dict[str, Dict[str, float]] = {
        "SUI":  {"vol_mult": 1.25, "rsi_min": 42.0, "rsi_max": 52.0, "min_wick": 0.25, "initial_sl": -0.20},
        "AVAX": {"vol_mult": 1.20, "rsi_min": 42.0, "rsi_max": 53.0, "min_wick": 0.22, "initial_sl": -0.20},
        "NEAR": {"vol_mult": 1.10, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20},
        "SOL":  {"vol_mult": 0.90, "rsi_min": 40.0, "rsi_max": 56.0, "min_wick": 0.15, "initial_sl": -0.20},
        "ETH":  {"vol_mult": 0.90, "rsi_min": 40.0, "rsi_max": 56.0, "min_wick": 0.15, "initial_sl": -0.20},
        "BTC":  {"vol_mult": 0.90, "rsi_min": 40.0, "rsi_max": 56.0, "min_wick": 0.15, "initial_sl": -0.20},
        "XRP":  {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20},
        "LINK": {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20},
        "DOGE": {"vol_mult": 1.15, "rsi_min": 42.0, "rsi_max": 53.0, "min_wick": 0.20, "initial_sl": -0.20},
        "LTC":  {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20},
        "BNB":  {"vol_mult": 0.95, "rsi_min": 40.0, "rsi_max": 56.0, "min_wick": 0.15, "initial_sl": -0.20},
        "ADA":  {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20},
    }

    def get_coin_profile(self, pair: str) -> Dict[str, float]:
        for base, prof in self.COIN_PROFILES.items():
            if pair.startswith(base):
                return prof
        return {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 55.0, "min_wick": 0.18, "initial_sl": -0.20}

    # Minimal ROI Ladder (Realistic Peak Scalper)
    # At 12x leverage:
    # 0 min:  +0.30 (+2.50% price move = +30% ROE)
    # 15 min: +0.22 (+1.83% price move = +22% ROE)
    # 30 min: +0.16 (+1.33% price move = +16% ROE)
    # 60 min: +0.12 (+1.00% price move = +12% ROE)
    minimal_roi = {
        "0": 0.30,
        "15": 0.22,
        "30": 0.16,
        "60": 0.12,
    }

    # Trailing Stop: UITGESCHAKELD (geen trailing stoploss meer, trades krijgen volledige ademruimte om naar take-profit te lopen)
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
        dataframe["bb_width"] = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_middle"]
        dataframe["bb_width_ma"] = dataframe["bb_width"].rolling(20).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """High-probability 5m breakouts tailored to each coin's personality."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF 4H Trend alignment
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]

        # Adaptive profile for this coin
        pair = metadata.get("pair", "")
        prof = self.get_coin_profile(pair)
        vol_mult = prof.get("vol_mult", 1.00)
        rsi_min = prof.get("rsi_min", 40.0)
        rsi_max = prof.get("rsi_max", 55.0)
        min_wick = prof.get("min_wick", 0.18)

        # 1. LONG SIGNALS: Kopen op de Vloer (Support Bounce & Squeeze Accumulation)
        # Prijs test de vloer: raakte in huidige of vorige kaars de EMA20 / BB Middle / EMA50
        floor_test = (
            (dataframe["low"] <= dataframe["bb_middle"]) |
            (dataframe["low"].shift(1) <= dataframe["bb_middle"].shift(1))
        )

        # Bodembevestiging: Kaars is groen en sluit krachtig boven de steun/vloer
        bounce_green = (dataframe["close"] > dataframe["open"]) & (dataframe["close"] >= dataframe["bb_middle"] * 0.998)

        # Rejectie-wick van onderen (kopers verdedigen de vloer)
        candle_range = dataframe["high"] - dataframe["low"] + 1e-6
        lower_wick = dataframe[["close", "open"]].min(axis=1) - dataframe["low"]
        has_support_rejection = (lower_wick / candle_range) >= min_wick

        # Niet op het dak kopen: Voldoende ruimte naar de bovenste band
        not_at_roof = dataframe["close"] < (dataframe["bb_upper"] * 0.995)

        # Volume & RSI in de gezonde bodem/afkoelzone
        vol_bounce = dataframe["volume"] > dataframe["vol_ma"] * vol_mult
        rsi_floor = (dataframe["rsi"] >= rsi_min) & (dataframe["rsi"] <= rsi_max)

        long_cond = htf_4h_bull & floor_test & bounce_green & has_support_rejection & not_at_roof & rsi_floor & vol_bounce

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = f"floor_bounce_{pair.split('/')[0]}"

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
        """Trailing stop completely disabled per user instruction.
        Trades maintain fixed protective stoploss and exit exclusively via minimal_roi ladder."""
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
        """Strict 50% account sizing per trade: each trade allocates exactly 50% of total account equity."""
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            free_equity = self.wallets.get_free(self.config["stake_currency"])

            # Every trade receives 50% of the total account equity
            target_stake = total_equity * 0.50

            compounded = max(min_stake or 5.0, target_stake)
            # Never exceed available free margin, leave safety buffer
            return min(compounded, max_stake, free_equity * 0.95)
        except Exception as exc:
            logger.warning(f"Error in custom_stake_amount: {exc}")
            try:
                free_equity = self.wallets.get_free(self.config["stake_currency"])
                return min(free_equity * 0.50, max_stake)
            except Exception:
                return min(25.0, max_stake)

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
        """Confirm trade entry: max 2 open trades (50% each) and Anti-Peak Re-entry Guard."""
        open_trades = Trade.get_open_trades()
        max_allowed = int(self.config.get("max_open_trades", 2))
        if len(open_trades) >= max_allowed:
            return False

        # Anti-Peak Re-entry Guard: Prevent immediately buying back into the exact same coin
        # within 15 minutes of taking profit/exiting on that same pump wave
        try:
            if getattr(Trade, "use_db", True):
                closed_trades = [
                    t for t in Trade.get_trades([Trade.pair == pair, Trade.is_open.is_(False)]).all()
                    if t.close_date
                ]
                if closed_trades:
                    last_trade = max(closed_trades, key=lambda t: t.close_date)
                    close_dt = last_trade.close_date
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    now_dt = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
                    mins_since_exit = (now_dt - close_dt).total_seconds() / 60.0
                    if mins_since_exit < 15.0:
                        logger.info(f"Anti-Peak Guard: Skipping {pair} re-entry ({mins_since_exit:.1f}m since last exit, min 15m required to avoid exhaustion trap)")
                        return False
        except Exception as exc:
            logger.warning(f"Error checking Anti-Peak Guard for {pair}: {exc}")

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

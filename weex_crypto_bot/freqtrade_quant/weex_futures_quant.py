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
- 8x Isolated Leverage & Dynamic 58% Compounding Wallet Staking.
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
    """WEEX Futures 15m Momentum Trend & Breakout Engine (80%-120% Runners)."""

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False

    # Protective Stoploss (-25% ROE / -2.5% price move at 10x)
    stoploss = -0.25
    use_custom_stoploss = True

    # High-Beta Volatile Profiles on WEEX (Support bounce tuning)
    COIN_PROFILES: Dict[str, Dict[str, float]] = {
        "AVAX":   {"vol_mult": 1.05, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "SOL":    {"vol_mult": 0.90, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "NEAR":   {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "LINK":   {"vol_mult": 0.95, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "XRP":    {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "WIF":    {"vol_mult": 1.10, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.18},
        "FET":    {"vol_mult": 1.05, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "DOGE":   {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "APT":    {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "SEI":    {"vol_mult": 1.05, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
        "TAO":    {"vol_mult": 0.95, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15},
    }

    def get_coin_profile(self, pair: str) -> Dict[str, float]:
        for base, prof in self.COIN_PROFILES.items():
            if pair.startswith(base):
                return prof
        return {"vol_mult": 1.00, "rsi_min": 40.0, "rsi_max": 58.0, "min_wick": 0.15}

    # Minimal ROI Ladder - Geoptimaliseerd voor Runners via Trailing Lock (10x Leverage)
    minimal_roi = {
        "0": 0.60,    # Hard take-profit bij +60% ROE (+6.0% koersmove)
        "60": 0.40,   # Na 1 uur: +40% ROE
        "180": 0.25,  # Na 3 uur: +25% ROE
        "360": 0.15,  # Na 6 uur: +15% ROE
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
        """Enforce 10x leverage on WEEX perpetuals (capped at exchange max)."""
        target_leverage = 10.0
        return min(target_leverage, max_leverage) if max_leverage > 1.0 else target_leverage

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """1H timeframe for market structure & trend expansion."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Higher timeframe bias for macro trend alignment."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("4h", "BTC/USDT:USDT")
    def populate_indicators_btc_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """BTC Macro Bull Shield: 4H trend indicators for Bitcoin to protect against altcoin market selloffs."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """15m floor bounce & support accumulation indicators."""
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = boll["upperband"]
        dataframe["bb_middle"] = boll["middleband"]
        dataframe["bb_lower"] = boll["lowerband"]
        dataframe["bb_width"] = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_middle"]
        dataframe["bb_width_ma"] = dataframe["bb_width"].rolling(20).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """High-probability support floor bounce (Buy the Dip/Floor, never buy the top)."""
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend Alignment (4H and 1H Bullish macro structure)
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_1h_bull = dataframe["close_1h"] > dataframe["ema20_1h"]

        # BTC Macro Bull Shield: Alleen altcoin longs toestaan als BTC boven zijn 4H EMA20 zit
        btc_col = [c for c in dataframe.columns if "btc" in c.lower() and "ema20" in c]
        btc_close_col = [c for c in dataframe.columns if "btc" in c.lower() and "close" in c]
        btc_bull = True
        if btc_col and btc_close_col:
            btc_bull = dataframe[btc_close_col[0]] > dataframe[btc_col[0]]

        # Adaptive profile for this coin
        pair = metadata.get("pair", "")
        prof = self.get_coin_profile(pair)
        vol_mult = prof.get("vol_mult", 1.00)
        rsi_min = prof.get("rsi_min", 40.0)
        rsi_max = prof.get("rsi_max", 58.0)
        min_wick = prof.get("min_wick", 0.15)

        # 1. Floor Test: Prijs test de bodem/steun (EMA20 of Bollinger Middle) in huidige of vorige kaars
        touched_floor = (
            (dataframe["low"] <= dataframe["ema20"]) |
            (dataframe["low"] <= dataframe["bb_middle"]) |
            (dataframe["low"].shift(1) <= dataframe["ema20"].shift(1)) |
            (dataframe["low"].shift(1) <= dataframe["bb_middle"].shift(1))
        )

        # 2. Bodembevestiging: Groene kaars die boven of op de steun sluit
        bounce_green = (dataframe["close"] > dataframe["open"]) & (dataframe["close"] >= dataframe["ema20"] * 0.998)

        # 3. Rejectie-wick van onderen (kopers verdedigen de vloer krachtig)
        candle_range = dataframe["high"] - dataframe["low"] + 1e-6
        lower_wick = dataframe[["close", "open"]].min(axis=1) - dataframe["low"]
        has_support_rejection = (lower_wick / candle_range) >= min_wick

        # 4. Absoluut NIET op het dak kopen: Voldoende ruimte naar de bovenste band
        not_at_roof = dataframe["close"] < (dataframe["bb_upper"] * 0.995)

        # 5. Gezonde afkoelzone voor RSI (niet overbought)
        rsi_floor = (dataframe["rsi"] >= rsi_min) & (dataframe["rsi"] <= rsi_max)

        # 6. Volumebevestiging op de bounce
        vol_bounce = dataframe["volume"] > (dataframe["vol_ma"] * vol_mult * 0.85)

        # 7. Trendstructuur op 15m (EMA20 boven EMA50)
        ema_aligned = dataframe["ema20"] > dataframe["ema50"]

        floor_bounce_cond = (
            htf_4h_bull &
            htf_1h_bull &
            btc_bull &
            touched_floor &
            bounce_green &
            has_support_rejection &
            not_at_roof &
            rsi_floor &
            vol_bounce &
            ema_aligned
        )

        dataframe.loc[floor_bounce_cond, "enter_long"] = 1
        dataframe.loc[floor_bounce_cond, "enter_tag"] = f"floor_bounce_{pair.split('/')[0]}"

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
        """
        Dynamische Trailing Stop & Flash Breakeven Lock voor 10x Leverage:
        - Vanaf +8% ROE: Flash Breakeven Lock (Stoploss naar Entry + 1.2% fee buffer) -> 100% RISICOLOOS!
        - Vanaf +16% ROE: Lock minimaal +10% ROE winst.
        - Vanaf +25% ROE: Dynamische Trailing Lock (trail 5% onder piek, minimaal +18% gelockt).
          Als de trade doorstoot naar +40% of +60%, rijdt de stoploss automatisch mee omhoog!
        """
        lev = trade.leverage or 10.0

        # Tier 3: Trailing Runner Lock (vanaf +25% ROE trail 5% achter piek aan, minimaal +18%)
        if current_profit >= 0.25:
            trail_offset = max(0.18, current_profit - 0.05)
            return stoploss_from_open(trail_offset, current_profit, is_short=trade.is_short, leverage=lev)

        # Tier 2: Lock +10% winst zodra +16% bereikt is
        if current_profit >= 0.16:
            return stoploss_from_open(0.10, current_profit, is_short=trade.is_short, leverage=lev)

        # Tier 1: Flash Breakeven Lock (+8% ROE bereikt -> lock entry + 1.2% fees)
        if current_profit >= 0.08:
            return stoploss_from_open(0.012, current_profit, is_short=trade.is_short, leverage=lev)

        return None

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """
        Stagnatie- en Time-out Exits (Voorkomt dat trades dagenlang vastzitten):
        1. Stagnatie Exit: Als een trade na 4 uur nauwelijks beweegt (-5% tot +5%), direct sluiten.
        2. Max Hold Timeout: Als een trade na 10 uur nog niet in winst staat (< +10%), sluiten.
        3. Trend Invalidation: Als 1H trend omslaat naar bear en de trade verlieslatend is, direct sluiten.
        """
        open_dt = trade.open_date_utc
        now_dt = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        duration_mins = (now_dt - open_dt).total_seconds() / 60.0

        # 1. Stagnatie Exit na 4 uur (240 min = 16 15m-candles) bij chop/zijwaarts
        if duration_mins >= 240 and -0.05 <= current_profit <= 0.05:
            return "stagnation_timeout_4h"

        # 2. Maximum Hold Timeout na 10 uur (600 min) indien winst < 10%
        if duration_mins >= 600 and current_profit < 0.10:
            return "max_duration_timeout_10h"

        # 3. HTF 1H Trend Invalidation
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if len(dataframe) > 0:
                last_row = dataframe.iloc[-1]
                if "close_1h" in last_row and "ema20_1h" in last_row:
                    if last_row["close_1h"] < last_row["ema20_1h"] and current_profit < -0.08:
                        return "1h_trend_broken"
        except Exception:
            pass

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
        """Strict 25% account sizing per trade: each trade allocates exactly 25% of total account equity."""
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            free_equity = self.wallets.get_free(self.config["stake_currency"])

            # Every trade receives 25% of the total account equity
            target_stake = total_equity * 0.25

            compounded = max(min_stake or 5.0, target_stake)
            # Never exceed available free margin, leave safety buffer
            return min(compounded, max_stake, free_equity * 0.95)
        except Exception as exc:
            logger.warning(f"Error in custom_stake_amount: {exc}")
            try:
                free_equity = self.wallets.get_free(self.config["stake_currency"])
                return min(free_equity * 0.25, max_stake)
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
        """Confirm trade entry: max 3 open trades (25% each) and Anti-Peak Re-entry Guard."""
        open_trades = Trade.get_open_trades()
        max_allowed = int(self.config.get("max_open_trades", 3))
        if len(open_trades) >= max_allowed:
            return False

        # Anti-Whipsaw Cooldown (45 min na verlies) & Anti-Peak Guard (15 min na winst)
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

                    # 1. Anti-Whipsaw Cooldown: Als vorige trade verlies was, 45 min wachten om vallend mes te vermijden
                    if (last_trade.close_profit or 0.0) < 0.0 and mins_since_exit < 45.0:
                        logger.info(
                            f"Anti-Whipsaw Cooldown: Skipping {pair} re-entry ({mins_since_exit:.1f}m since loss exit, min 45m required to avoid knife catching)"
                        )
                        return False

                    # 2. Anti-Peak Guard: Na winstgevende exit minimaal 15 min wachten
                    if mins_since_exit < 15.0:
                        logger.info(
                            f"Anti-Peak Guard: Skipping {pair} re-entry ({mins_since_exit:.1f}m since last exit, min 15m required)"
                        )
                        return False
        except Exception as exc:
            logger.warning(f"Error checking Anti-Whipsaw / Anti-Peak Guard for {pair}: {exc}")

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

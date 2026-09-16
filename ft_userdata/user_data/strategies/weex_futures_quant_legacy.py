# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
import json
import logging
import math
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    informative,
    stoploss_from_open,
)
from freqtrade.persistence import Trade

import sys
for _p in ["/freqtrade/user_data", "/root/ft_userdata/user_data"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from orbit_engine import DeDirigent, Orbit0Conductor

logger = logging.getLogger(__name__)

META_WEIGHTS_PATH = "/freqtrade/user_data/meta_weights.json"
if not os.path.exists("/freqtrade/user_data"):
    META_WEIGHTS_PATH = "/root/ft_userdata/user_data/meta_weights.json"


class weex_futures_quant_legacy(IStrategy):
    """
    Weex Futures Quant Trading Systeem:
    - Track 1: Trend Core (1h EMA 200, Choppiness Index < 61.8, Confluence >= 74% / 82%)
    - Track 2: Sweep Hunter (Liquidity wick sweep, micro-risk 0.3%-0.6%, 1:3+ R:R)
    - Trade Management:
        * Entry via Limit Maker
        * Scale-out Track 1: 40% @ +1R (daarna BE), 35% @ +2R, 25% Runner
        * Scale-out Track 2: 60% @ +1.5R, 40% @ +2.5R
        * Structurele Stops achter Swing High/Low / ATR (geen 0.2% ruis)
    - Conductor (De Dirigent): Master consensus, thesis gate & kill switches.
    - Exchange: Weex Futures (USDT perpetual swaps, 5x leverage)
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = True  # Dual-direction futures

    # Risk settings (afgestemd op 3x-7x hefboom en account-% risico)
    stoploss = -0.04  # 4.0% veilige fallback stoploss (bij 5x hefboom = -20% marge)
    use_custom_stoploss = True
    use_custom_exit = True
    trailing_stop = False  # Wordt beheerd via Sentinel trailing in custom_stoploss

    # Position Adjustment (voor 40/35/25 Scale-Out)
    position_adjustment_enable = True
    max_entry_position_adjustment = 3  # Max 1 pyramid additie

    # Post-Only Maker Execution
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {
        "entry": "GTC",  # GTC: Weex ondersteunt geen Post-Only via Freqtrade
        "exit": "GTC",
    }

    startup_candle_count = 210

    # Minimal ROI vangnet (exits worden direct beheerd door adjust_trade_position en Sentinel)
    minimal_roi = {
        "0": 0.25,
    }

    @property
    def protections(self):
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 288,  # 3 dagen op 15m
                "trade_limit": 5,
                "stop_duration_candles": 48,
                "max_allowed_drawdown": 0.08,  # 8% hard stop
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,  # 6 uur op 15m
                "trade_limit": 5,
                "stop_duration_candles": 8,
                "only_per_pair": False,
            },
        ]

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._peak_equity: float = 0.0
        self._consecutive_losses: int = 0
        self._meta_weights: Dict[str, Any] = {
            "confluence_threshold_long": 74.0,
            "confluence_threshold_short": 82.0,
            "trend_core_weight": 1.0,
            "sweep_hunter_weight": 1.0,
            "chop_threshold": 61.8,
        }
        # In-memory tracking van trade life cycle fases
        self._pyramided_trades: set = set()
        self._tp1_executed_trades: set = set()
        # Orbit Multi-Agent Engines (Geleid door De Dirigent)
        self.dirigent = DeDirigent(META_WEIGHTS_PATH)
        self.risk_sentinel = self.dirigent.risk_sentinel
        self.sentinel = self.risk_sentinel
        self.orbit0 = self.dirigent
        self.orbit1 = self.dirigent.sensory
        self.orbit2 = self.dirigent.alpha

        self._load_meta_weights()

        logger.info("Weex Futures Quant System with De Dirigent (Master Conductor) & Risk Sentinel Initialized.")

    def _load_meta_weights(self) -> None:
        """Laadt dynamisch geoptimaliseerde gewichten van de background learning agent."""
        try:
            if os.path.exists(META_WEIGHTS_PATH):
                with open(META_WEIGHTS_PATH, "r") as f:
                    weights = json.load(f)
                    self._meta_weights.update(weights)
                    self.risk_sentinel.update_settings(weights)
                    logger.info(f"[Learning Agent] Meta-weights loaded: {self._meta_weights}")
        except Exception as e:
            logger.warning(f"Could not load meta weights: {e}")

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs) -> float:
        """
        Gecontroleerde hefboom van 3x - 7x (standaard 5x) conform quant risicomodel.
        50x cross margin is uitgeschakeld.
        """
        target_lev = getattr(self.risk_sentinel, 'default_leverage', 5.0)
        if max_leverage and max_leverage > 1.0:
            target_lev = min(target_lev, max_leverage)
        return float(target_lev)

    # --------------------------------------------------------------------------
    # Pilaar 1: Post-Only 4s Timeout Escape
    # --------------------------------------------------------------------------
    def check_entry_timeout(self, pair: str, trade: Trade, order: dict,
                            order_tag: Optional[str], current_time: datetime,
                            **kwargs) -> bool:
        order_date = order.get("transact_time_utc") or trade.open_date_utc or trade.open_date
        if order_date:
            if order_date.tzinfo is None:
                order_date = order_date.replace(tzinfo=timezone.utc)
            now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            if (now - order_date).total_seconds() >= 4:
                return True  # Cancel unfilled post-only
        return False

    def adjust_entry_price(self, trade: Trade, order: dict, pair: str,
                           current_time: datetime, proposed_rate: float,
                           current_order_rate: float, entry_tag: Optional[str],
                           side: str, **kwargs) -> float:
        return proposed_rate

    # --------------------------------------------------------------------------
    # Informative 1h Timeframe: Echte 1-uurs EMA 200
    # --------------------------------------------------------------------------
    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Echte 1-uurs EMA 200 over voldoende candles
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr1h"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    # --------------------------------------------------------------------------
    # 15m Indicatoren: Choppiness Index, Sweeps & Confluence
    # --------------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1. Choppiness Index (n=14)
        atr1 = ta.TRANGE(dataframe)
        sum_atr14 = atr1.rolling(14).sum()
        high14 = dataframe["high"].rolling(14).max()
        low14 = dataframe["low"].rolling(14).min()
        range14 = (high14 - low14).replace(0, np.nan)
        dataframe["chop"] = 100 * np.log10(sum_atr14 / range14) / np.log10(14)
        dataframe["chop"] = dataframe["chop"].fillna(50.0)

        # 2. ATR voor trailing stop
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        # 3. Korte termijn EMA's
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # 4. Swing High / Low (voor Track 2 Sweep Hunter)
        dataframe["swing_high"] = dataframe["high"].rolling(16).max()
        dataframe["swing_low"] = dataframe["low"].rolling(16).min()
        dataframe["prev_swing_high"] = dataframe["swing_high"].shift(1)
        dataframe["prev_swing_low"] = dataframe["swing_low"].shift(1)

        # 4b. 24h Structurele Swing High / Low (96 candles op 15m)
        dataframe["swing_high_24h"] = dataframe["high"].rolling(96).max()
        dataframe["swing_low_24h"] = dataframe["low"].rolling(96).min()

        # 5. Sweep Hunter Detectie
        # Bullish sweep: low schiet onder prev_swing_low maar candle sluit boven de swing low (wick sweep)
        dataframe["bullish_sweep"] = (
            (dataframe["low"] < dataframe["prev_swing_low"]) &
            (dataframe["close"] > dataframe["prev_swing_low"]) &
            (dataframe["close"] > dataframe["open"])
        )
        # Bearish sweep: high schiet boven prev_swing_high maar sluit eronder
        dataframe["bearish_sweep"] = (
            (dataframe["high"] > dataframe["prev_swing_high"]) &
            (dataframe["close"] < dataframe["prev_swing_high"]) &
            (dataframe["close"] < dataframe["open"])
        )

        # Structurele Stoploss levels bij sweep (achter wick met buffer, geen 0.2% ruis)
        dataframe["sweep_stop_long"] = dataframe["low"] * 0.985
        dataframe["sweep_stop_short"] = dataframe["high"] * 1.015

        # 6. Confluence Score Berekening (0 tot 100)
        # Long Confluence factoren:
        # - 1h Trend bullish (close_1h > ema200_1h): 30pt
        # - 15m Trend bullish (ema20 > ema50): 20pt
        # - RSI tussen 40 en 60 (pullback/momentum): 20pt
        # - Volume boven moving average: 15pt
        # - Anti-chop bevestiging (CHOP < 50): 15pt
        vol_ma = dataframe["volume"].rolling(20).mean()
        
        long_conf = (
            (dataframe["close_1h"] > dataframe["ema200_1h"]).astype(int) * 30 +
            (dataframe["ema20"] > dataframe["ema50"]).astype(int) * 20 +
            ((dataframe["rsi"] >= 40) & (dataframe["rsi"] <= 65)).astype(int) * 20 +
            (dataframe["volume"] > vol_ma).astype(int) * 15 +
            (dataframe["chop"] < 50.0).astype(int) * 15
        )
        dataframe["confluence_long"] = long_conf

        short_conf = (
            (dataframe["close_1h"] < dataframe["ema200_1h"]).astype(int) * 30 +
            (dataframe["ema20"] < dataframe["ema50"]).astype(int) * 20 +
            ((dataframe["rsi"] >= 35) & (dataframe["rsi"] <= 60)).astype(int) * 20 +
            (dataframe["volume"] > vol_ma).astype(int) * 15 +
            (dataframe["chop"] < 50.0).astype(int) * 15
        )
        dataframe["confluence_short"] = short_conf

        # 7. Orbit Multi-Agent Pipeline Indicatoren (Orbit 1 & Orbit 2)
        dataframe = self.orbit1.calculate_liquidation_cascade(dataframe)
        dataframe = self.orbit1.calculate_imbalance_absorption(dataframe)
        dataframe = self.orbit1.calculate_vpin_toxicity(dataframe)
        dataframe = self.orbit2.calculate_lead_lag(dataframe)
        dataframe = self.orbit2.calculate_squeeze_divergence(dataframe)
        dataframe = self.orbit2.calculate_htf_structure(dataframe)
        dataframe = self.orbit2.classify_market_regime(dataframe)

        return dataframe

    # --------------------------------------------------------------------------
    # Entry Signalen: Track 1 (Trend Core) & Track 2 (Sweep Hunter)
    # --------------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop_limit = self._meta_weights.get("chop_threshold", 61.8)
        conf_long_req = self._meta_weights.get("confluence_threshold_long", 74.0)
        conf_short_req = self._meta_weights.get("confluence_threshold_short", 82.0)

        strictness = self._meta_weights.get("trend_filter_strictness", "balanced")
        if strictness == "relaxed":
            long_trend_filter = dataframe["close_1h"] > (dataframe["ema200_1h"] * 0.995)
            short_trend_filter = dataframe["close_1h"] < (dataframe["ema200_1h"] * 1.005)
        elif strictness == "strict":
            long_trend_filter = (dataframe["close_1h"] > dataframe["ema200_1h"]) & (dataframe["close"] > dataframe["ema200"])
            short_trend_filter = (dataframe["close_1h"] < dataframe["ema200_1h"]) & (dataframe["close"] < dataframe["ema200"])
        else:  # balanced
            long_trend_filter = dataframe["close_1h"] > dataframe["ema200_1h"]
            short_trend_filter = dataframe["close_1h"] < dataframe["ema200_1h"]

        # --- TRACK 1: TREND CORE (Long & Short) ---
        trend_long = (
            long_trend_filter &
            (dataframe["chop"] < chop_limit) &
            (dataframe["confluence_long"] >= conf_long_req) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[trend_long, ["enter_long", "enter_tag"]] = (1, "trend_core_long")

        trend_short = (
            short_trend_filter &
            (dataframe["chop"] < chop_limit) &
            (dataframe["confluence_short"] >= conf_short_req) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[trend_short, ["enter_short", "enter_tag"]] = (1, "trend_core_short")

        # --- TRACK 2: SWEEP HUNTER (Micro-Risk Wick Hunt) ---
        sweep_long = (
            (dataframe["bullish_sweep"]) &
            (dataframe["chop"] < chop_limit) &
            (dataframe["close_1h"] >= dataframe["ema200_1h"] * 0.985) &  # Niet diep contra-trend
            (dataframe["volume"] > 0)
        )
        dataframe.loc[sweep_long & ~trend_long, ["enter_long", "enter_tag"]] = (1, "sweep_hunter_long")

        sweep_short = (
            (dataframe["bearish_sweep"]) &
            (dataframe["chop"] < chop_limit) &
            (dataframe["close_1h"] <= dataframe["ema200_1h"] * 1.015) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[sweep_short & ~trend_short, ["enter_short", "enter_tag"]] = (1, "sweep_hunter_short")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_entry_price(self, pair: str, current_time: datetime, proposed_rate: float,
                           entry_tag: Optional[str], side: str, **kwargs) -> float:
        if proposed_rate and not np.isnan(proposed_rate) and proposed_rate > 0:
            return proposed_rate
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is not None and not dataframe.empty:
            return float(dataframe['close'].iloc[-1])
        return proposed_rate

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> Optional[Union[str, bool]]:
        """
        Orbit 4 Sentinel: Bewaakt actieve trades en dwingt 1:3 R:R Take Profit af (+3.0R = +150% ROI bij 50x),
        of een vroege noodkap bij extreme adverse flow toxiciteit.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        toxicity = 0.5
        chop = 50.0
        if dataframe is not None and not dataframe.empty:
            if "sensory_vpin_toxicity" in dataframe.columns:
                toxicity = float(dataframe["sensory_vpin_toxicity"].iloc[-1])
            if "chop" in dataframe.columns:
                chop = float(dataframe["chop"].iloc[-1])

        # Update Sentinel realtime trade health status
        self.sentinel.evaluate_trade_health(
            trade=trade,
            current_rate=current_rate,
            current_profit=current_profit,
            vpin_toxicity=toxicity,
            chop_index=chop,
            current_time=current_time
        )

        # Controleer of Sentinel een exit triggert (1:3 R:R of adverse flow noodkap)
        sentinel_exit = self.sentinel.check_custom_exit(
            trade=trade,
            current_rate=current_rate,
            current_profit=current_profit,
            vpin_toxicity=toxicity,
            chop_index=chop,
            current_time=current_time
        )
        if sentinel_exit:
            return sentinel_exit

        return None

    def custom_exit_price(self, pair: str, trade: Trade,
                          current_time: datetime, proposed_rate: float,
                          current_profit: float, exit_tag: Optional[str],
                          **kwargs) -> float:
        if proposed_rate and not np.isnan(proposed_rate) and proposed_rate > 0:
            return proposed_rate
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is not None and not dataframe.empty:
            return float(dataframe['close'].iloc[-1])
        return proposed_rate

    # --------------------------------------------------------------------------
    # Harde Veiligheidsfilters & De Dirigent Besluitvorming
    # --------------------------------------------------------------------------
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, entry_tag: Optional[str],
                            side: str, **kwargs) -> bool:
        """
        De Dirigent hakt de knoop door over elke trade:
        - Master Kill Switches (dag-drawdown, stale data, liquidatie buffer, nachtslot, spread)
        - Thesis & Correlation Cluster Veto (max 1 actieve thesis in gecorreleerde alts)
        - Margin occupancy cap (max 20-30%)
        - Confluence score
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last_candle = dataframe.iloc[-1] if (dataframe is not None and not dataframe.empty) else None
        open_trades = Trade.get_open_trades()
        ticker = self.dp.ticker(pair) if hasattr(self, "dp") and self.dp else {}
        equity = self.wallets.get_total_stake_amount() if self.wallets else 168.0
        starting_eq = self.wallets.get_starting_balance() if self.wallets else 168.0

        approved, score, reason = self.dirigent.evaluate_entry(
            pair=pair,
            side=side,
            dataframe_row=last_candle,
            open_trades=open_trades,
            ticker=ticker,
            current_equity=equity,
            starting_equity=starting_eq,
            current_dt=datetime.now(timezone.utc)
        )
        logger.info(f"{reason}")
        return approved

    # --------------------------------------------------------------------------
    # True Account-% Risk Sizing (Track 1: 0.5-1.0%, Track 2: 0.3-0.6%)
    # --------------------------------------------------------------------------
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str], side: str,
                            **kwargs) -> float:
        """
        True Account-% Risk Sizing via Risk Sentinel:
        - Track 1 (Trend Core): 0.5% - 1.0% van account equity (default 0.75%).
        - Track 2 (Sweep Hunter): 0.3% - 0.6% van account equity (default 0.45%).
        - Margebezetting gecapped op 20% - 30% van account (default 25%).
        """
        equity = self.wallets.get_total_stake_amount() if self.wallets else 168.0
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last_row = dataframe.iloc[-1] if (dataframe is not None and not dataframe.empty) else None

        is_track2 = "sweep" in (entry_tag or "").lower()
        structural_sl = self.risk_sentinel.calculate_structural_stop_distance(
            dataframe_row=last_row,
            current_rate=current_rate,
            is_short=(side == "short")
        )

        open_trades = Trade.get_open_trades()
        stake, notional, reason = self.risk_sentinel.calculate_position_sizing(
            equity=equity,
            current_rate=current_rate,
            structural_sl_dist=structural_sl,
            is_track2=is_track2,
            open_trades=open_trades
        )
        logger.info(f"{reason} | Pair: {pair}")
        if min_stake:
            stake = max(stake, min_stake)
        if max_stake:
            stake = min(stake, max_stake)
        return float(stake)

    # --------------------------------------------------------------------------
    # Levenscyclus: Track 1 (40/35/25 Scale-Out) & Track 2 (60/40 Fast Exits)
    # --------------------------------------------------------------------------
    def _calculate_r_distance(self, trade: Trade) -> float:
        """
        Bepaalt de werkelijke 1R koersafstand:
        Structurele stop (meestal 1.5% - 3.5%), NOOIT 0.2% ruis.
        """
        if trade.stop_loss_pct and trade.stop_loss_pct < 0:
            return max(0.012, abs(trade.stop_loss_pct))
        return 0.020

    def adjust_trade_position(self, trade: Trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs) -> Optional[float]:
        """
        Scale-out beheer via Risk Sentinel:
        Track 1 (Trend Core 40/35/25):
        - 40% @ +1.0R -> Winst banken, pas HIERNA stoploss naar Break-Even (+0.15% fee buffer).
        - 35% @ +2.0R -> Winst banken, stoploss naar +1.0R (winst vergrendeld).
        - 25% Runner -> Geen harde TP, trail achter structuur / ATR.

        Track 2 (Sweep Hunter):
        - 60% @ +1.5R (snelle winstname)
        - 40% @ +2.5R (volledige sluiting)
        """
        if trade.open_orders:
            return None

        R = self._calculate_r_distance(trade)
        adjustment = self.risk_sentinel.evaluate_scale_out(
            trade=trade,
            current_profit=current_profit,
            r_distance=R,
            min_stake=min_stake
        )
        if adjustment:
            amount, tag = adjustment
            msg = f"🎯 [{tag}] {trade.pair}: winst={current_profit:.2%}, schaalt positie uit met {amount:.2f} USDT!"
            logger.info(msg)
            if self.dp:
                self.dp.send_msg(msg, always_send=True)
            return amount

        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> Optional[float]:
        """
        Stoploss beheer via Risk Sentinel:
        - Pas NÁ TP1 (+1.0R): Stoploss naar Break-Even (+0.15% fee buffer).
          (Fast-BE op +0.75R is bewust verwijderd om normale trendpullbacks niet af te kappen).
        - NÁ TP2 (+2.0R): Stoploss vergrendeld op +1.0R winst.
        - Runner fase: Trailing stop achter 1.5x ATR.
        - Vóór TP1: Volledige structurele stoploss (-1.5% tot -3.5%).
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        atr_offset = None
        if dataframe is not None and not dataframe.empty and "atr" in dataframe:
            atr = dataframe["atr"].iloc[-1]
            if atr and current_rate > 0:
                atr_offset = (1.5 * atr) / current_rate

        R = self._calculate_r_distance(trade)
        desired_stop, stage = self.risk_sentinel.calculate_custom_stoploss(
            trade=trade,
            current_profit=current_profit,
            r_distance=R,
            default_sl=self.stoploss,
            atr_offset_pct=atr_offset
        )

        return stoploss_from_open(
            desired_stop,
            current_profit,
            is_short=trade.is_short,
            leverage=trade.leverage or 1.0
        )

    # --------------------------------------------------------------------------
    # Bot Loop & Equity Tracking
    # --------------------------------------------------------------------------
    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._update_peak_equity()
        self._update_consecutive_losses()
        self._load_meta_weights()

    def _update_peak_equity(self) -> None:
        if self.wallets:
            equity = self.wallets.get_total_stake_amount()
            if equity > self._peak_equity:
                self._peak_equity = equity

    def _get_current_drawdown(self) -> float:
        if self.wallets and self._peak_equity > 0:
            equity = self.wallets.get_total_stake_amount()
            return max(0.0, (self._peak_equity - equity) / self._peak_equity)
        return 0.0

    def _update_consecutive_losses(self) -> None:
        try:
            all_closed = Trade.get_trades([Trade.is_open.is_(False)]).all()
            recent_trades = sorted(all_closed, key=lambda t: t.id, reverse=True)[:10]
            losses = 0
            for t in recent_trades:
                if t.close_profit and t.close_profit < 0:
                    losses += 1
                else:
                    break
            self._consecutive_losses = losses
        except Exception:
            pass

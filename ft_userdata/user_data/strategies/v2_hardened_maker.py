# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import numpy as np
import pandas as pd
from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    stoploss_from_open,
)
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)


class v2_hardened_maker(IStrategy):
    """
    v2_hardened_maker:
    Pilaar 1: Post-Only Maker order executie + 4s timeout escape valve
    Pilaar 2: MFE Winstbehoud (TP1 @ +1.1R sluit 50%, Dynamische Trailing vanaf +0.80R)
    Pilaar 3: Geheugen met Wiskundige Formules & Time-Decay (10-dag half-life, 40-trade recency)
    Pilaar 4: Hermetisch Afgesloten Risk Engine (Max 1 positie, max 5x leverage, 1.8% risico, floating DD floor)
    Pilaar 5: Losing-Streak Schokdemper (3 verliezen -> 0.9% risk, 5 verliezen -> 60 min cooldown)
    Pilaar 6: Non-blocking SQLite WAL & Telegram Reporting
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False

    # Risk settings
    stoploss = -0.06  # Ruime absolute veiligheidsstop, custom_stoploss beheert het
    use_custom_stoploss = True
    trailing_stop = False  # Custom trailing via custom_stoploss

    # Position Adjustment (voor TP1 partial exit)
    position_adjustment_enable = True

    # Pilaar 1: Post-Only Maker Execution
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {
        "entry": "PO",   # Post-Only: gegarandeerd Maker fee korting
        "exit": "GTC",
    }

    # SMC & Indicator parameters
    swing_len = IntParameter(5, 15, default=8, space="buy", optimize=True)
    rsi_long_max = IntParameter(30, 50, default=45, space="buy", optimize=True)
    startup_candle_count = 60

    # Minimum ROI fallback
    minimal_roi = {
        "0": 0.08,
        "60": 0.05,
        "180": 0.03,
        "360": 0.015,
    }

    # Protections (Pilaar 4 & 5)
    @property
    def protections(self):
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 288,  # 3 dagen op 15m
                "trade_limit": 5,
                "stop_duration_candles": 48,     # 12 uur pauze
                "max_allowed_drawdown": 0.08,    # 8% hard stop
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,   # 6 uur op 15m
                "trade_limit": 5,
                "stop_duration_candles": 8,      # 2 uur pauze
                "only_per_pair": False,
            }
        ]

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._peak_equity: float = 0.0
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None
        self._regime_scores: Dict[str, float] = {}
        self._tp1_executed_trades: set = set()
        logger.info("v2_hardened_maker strategy initialized with 6 Pillars.")

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs) -> float:
        """Pilaar 4: Max 5x leverage cap."""
        return min(5.0, max_leverage)

    # --------------------------------------------------------------------------
    # Pilaar 1: Post-Only Maker 4s Timeout Escape
    # --------------------------------------------------------------------------
    def check_entry_timeout(self, pair: str, trade: Trade, order: dict,
                            order_tag: Optional[str], current_time: datetime,
                            **kwargs) -> bool:
        """
        Annuleert de Post-Only limit order na 4 seconden als deze niet direct
        als maker gevuld is.
        """
        order_date = order.get("transact_time_utc") or trade.open_date_utc or trade.open_date
        if order_date:
            if order_date.tzinfo is None:
                order_date = order_date.replace(tzinfo=timezone.utc)
            now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            age_secs = (now - order_date).total_seconds()
            if age_secs >= 4:
                logger.info(f"[Post-Only 4s Escape] Cancelling unfilled maker order for {pair} after {age_secs:.1f}s")
                return True
        return False

    def adjust_entry_price(self, trade: Trade, order: dict, pair: str,
                           current_time: datetime, proposed_rate: float,
                           current_order_rate: float, entry_tag: Optional[str],
                           side: str, **kwargs) -> float:
        """Herplaatst direct op actuele koers na timeout."""
        return proposed_rate

    # --------------------------------------------------------------------------
    # Indicatoren & Entry
    # --------------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        swing = int(self.swing_len.value)
        window = swing * 2 + 1
        dataframe["swing_high"] = dataframe["high"].where(
            dataframe["high"] == dataframe["high"].rolling(window, center=True).max()
        )
        dataframe["swing_low"] = dataframe["low"].where(
            dataframe["low"] == dataframe["low"].rolling(window, center=True).min()
        )
        dataframe["last_swing_high"] = dataframe["swing_high"].ffill()
        dataframe["last_swing_low"] = dataframe["swing_low"].ffill()

        # Fib 50% discount zone
        dataframe["fib_mid"] = (dataframe["last_swing_high"] + dataframe["last_swing_low"]) / 2
        dataframe["in_discount"] = dataframe["close"] < dataframe["fib_mid"]

        # Bullish Order Block
        dataframe["bullish_ob"] = (
            (dataframe["low"].shift(1) < dataframe["low"].shift(2)) &
            (dataframe["close"].shift(1) < dataframe["open"].shift(1)) &
            (dataframe["close"] > dataframe["high"].shift(1)) &
            (dataframe["close"] > dataframe["open"])
        )
        dataframe["ob_high"] = np.where(dataframe["bullish_ob"], dataframe["high"].shift(1), np.nan)
        dataframe["ob_low"] = np.where(dataframe["bullish_ob"], dataframe["low"].shift(1), np.nan)

        dataframe["ob_high"] = dataframe["ob_high"].ffill(limit=25)
        dataframe["ob_low"] = dataframe["ob_low"].ffill(limit=25)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ob_high"].notna()) &
                (dataframe["low"] <= dataframe["ob_high"]) &
                (dataframe["high"] >= dataframe["ob_low"]) &
                (dataframe["in_discount"]) &
                (dataframe["rsi"] < self.rsi_long_max.value) &
                (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # --------------------------------------------------------------------------
    # Pilaar 2: MFE Winstbehoud (TP1 @ +1.1R + Dynamische Trailing vanaf +0.80R)
    # --------------------------------------------------------------------------
    def _calculate_trade_risk_ratio(self, trade: Trade) -> float:
        """Berekent de werkelijke risico-eenheid R (stoploss ratio) van de positie."""
        if trade.stop_loss_pct and trade.stop_loss_pct < 0:
            return abs(trade.stop_loss_pct)
        return abs(self.stoploss)

    def adjust_trade_position(self, trade: Trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs) -> Optional[float]:
        """
        TP1 Winstbehoud:
        Bij +1.1R winst sluiten we direct 50% van de positie om winst veilig te stellen.
        """
        # Als er al openstaande orders zijn voor deze trade, wacht
        if trade.open_orders:
            return None

        # Slechts 1 maal TP1 per trade uitvoeren
        if trade.id in self._tp1_executed_trades or trade.nr_of_successful_exits >= 1:
            return None

        R = self._calculate_trade_risk_ratio(trade)
        tp1_trigger = 1.1 * R

        if current_profit >= tp1_trigger:
            self._tp1_executed_trades.add(trade.id)
            half_stake = -(trade.stake_amount / 2)
            msg = (f"🎯 [TP1 BEREIKT] {trade.pair}: profit={current_profit:.2%}, "
                   f"vergrendelt 50% positie ({abs(half_stake):.2f} USDT)")
            logger.info(msg)
            if self.dp:
                self.dp.send_msg(msg, always_send=True)
            return half_stake

        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> Optional[float]:
        """
        Dynamisch Winstbehoud:
        1. Vanaf +0.80R winst -> Activeer trailing stop en lock break-even (+0.50% winst buffer)
        2. Vóór +0.80R -> Structurele OB-low stop (~0.5% onder OB low)
        """
        R = self._calculate_trade_risk_ratio(trade)
        trail_trigger = 0.80 * R

        # Stap 1: Dynamisch trailen vanaf +0.80R
        if current_profit >= trail_trigger:
            # Vergrendel minimaal +0.50% winst vanaf entry
            locked_profit = max(0.005, current_profit * 0.50)
            return stoploss_from_open(locked_profit, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        # Stap 2: Structurele Order Block Stop
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return self.stoploss

        last_ob_low = dataframe["ob_low"].iloc[-1]
        if last_ob_low is None or np.isnan(last_ob_low):
            return self.stoploss

        stop_price = last_ob_low * 0.995
        if trade.open_rate > 0:
            relative_sl = (stop_price / trade.open_rate) - 1
            if -0.08 < relative_sl < -0.005:
                return stoploss_from_open(relative_sl, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        return self.stoploss

    # --------------------------------------------------------------------------
    # Pilaar 3, 4 & 5: Memory Engine, Risk Engine & Streak Schokdemper
    # --------------------------------------------------------------------------
    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """Wordt elke bot-iteratie uitgevoerd voor memory & risk berekeningen."""
        self._update_peak_equity()
        self._update_consecutive_losses()
        self._update_regime_memory(current_time)

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
        """Telt opeenvolgende verliezen uit de SQLite trades tabel."""
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
        except Exception as e:
            logger.warning(f"Kon consecutive losses niet ophalen: {e}")

    def _update_regime_memory(self, now: datetime) -> None:
        """
        Pilaar 3: Memory Engine met wiskundige time-decay:
        weight = exp(-age_days * ln(2) / half_life_days)
        Recency window: 40 trades
        Ruisfilter: |profit| < 0.1%
        """
        HALF_LIFE_DAYS = 10.0
        RECENCY_WINDOW = 40
        NOISE_THRESHOLD = 0.001

        whitelist = self.config.get("exchange", {}).get("pair_whitelist", [])
        for pair in whitelist:
            try:
                pair_closed = Trade.get_trades([Trade.is_open.is_(False), Trade.pair == pair]).all()
                trades = sorted(pair_closed, key=lambda t: t.id, reverse=True)[:RECENCY_WINDOW]

                if not trades:
                    self._regime_scores[pair] = 0.0
                    continue

                weighted_score = 0.0
                total_weight = 0.0

                for t in trades:
                    if t.close_profit is None or abs(t.close_profit) < NOISE_THRESHOLD:
                        continue

                    close_dt = t.close_date_utc or t.close_date
                    if close_dt:
                        if close_dt.tzinfo is None:
                            close_dt = close_dt.replace(tzinfo=timezone.utc)
                        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
                        age_days = max(0.0, (now_utc - close_dt).total_seconds() / 86400.0)
                    else:
                        age_days = 0.0

                    w = math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)
                    outcome = 1.0 if t.close_profit > 0 else -1.0
                    weighted_score += w * outcome
                    total_weight += w

                self._regime_scores[pair] = (
                    weighted_score / total_weight if total_weight > 0 else 0.0
                )
            except Exception as e:
                logger.warning(f"Error in regime calculation for {pair}: {e}")

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, entry_tag: Optional[str],
                            side: str, **kwargs) -> bool:
        """
        Pilaar 4: Hermetisch afgesloten Risk Engine:
        1. Max 1 positie gelijktijdig
        2. Drawdown floor (8% hard stop)
        3. Losing streak 60-minuten cooldown na 5 verliezers
        """
        # 1. Max 1 positie
        open_trades = Trade.get_open_trades()
        if len(open_trades) >= 1:
            logger.info(f"[Risk Engine] Geweigerd voor {pair}: reeds {len(open_trades)} positie open.")
            return False

        # 2. Drawdown floor: 8% hard stop
        dd = self._get_current_drawdown()
        if dd >= 0.08:
            msg = f"🛑 [Risk Engine HARD STOP] Drawdown is {dd:.2%}, overschrijdt 8% floor!"
            logger.warning(msg)
            if self.dp:
                self.dp.send_msg(msg, always_send=True)
            return False

        # 3. Losing streak cooldown: 5 verliezen -> 60 min rust
        now = datetime.now(timezone.utc)
        if self._consecutive_losses >= 5:
            if self._cooldown_until is None:
                self._cooldown_until = now + timedelta(minutes=60)
                msg = f"⏳ [Schokdemper] 5 verliezers op rij! 60 minuten cooldown actief tot {self._cooldown_until.strftime('%H:%M:%S UTC')}."
                logger.warning(msg)
                if self.dp:
                    self.dp.send_msg(msg, always_send=True)
                return False
            elif now < self._cooldown_until:
                remain = int((self._cooldown_until - now).total_seconds() / 60)
                logger.info(f"[Schokdemper] Cooldown actief nog {remain} min.")
                return False
            else:
                self._cooldown_until = None
                logger.info("[Schokdemper] Cooldown periode verstreken. Hervatting mogelijk.")

        return True

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str], side: str,
                            **kwargs) -> float:
        """
        Pilaar 4 & 5: Dynamische Stake Sizing
        - Basis risico: 1.8% van de totale equity
        - Streak schokdemper: bij 3 verliezers op rij -> halvering naar 0.9%
        - Regime score: bij bearish score (< -0.3) -> halvering
        - Drawdown floor: bij >= 5% floating drawdown -> halvering
        """
        base_risk_ratio = 0.018  # 1.8%

        # Streak schokdemper: halvering bij >= 3 verliezen
        if self._consecutive_losses >= 3:
            base_risk_ratio *= 0.5
            logger.info(f"[Stake Sizing] 3+ verliezers ({self._consecutive_losses}): risico gehalveerd naar {base_risk_ratio:.3%}")

        # Regime memory schokdemper
        score = self._regime_scores.get(pair, 0.0)
        if score < -0.3:
            base_risk_ratio *= 0.5
            logger.info(f"[Stake Sizing] Bearish memory score {score:.2f} voor {pair}: risico gehalveerd")

        # Drawdown floor schokdemper (5% halvering)
        dd = self._get_current_drawdown()
        if dd >= 0.05:
            base_risk_ratio *= 0.5
            logger.info(f"[Stake Sizing] Floating drawdown {dd:.2%} >= 5%: risico gehalveerd")

        if self.wallets:
            equity = self.wallets.get_total_stake_amount()
            sl_pct = abs(self.stoploss)
            calculated_stake = (equity * base_risk_ratio) / sl_pct
            # Cap op maximaal 20% van de equity per trade
            stake = min(calculated_stake, equity * 0.20)
            stake = min(stake, max_stake)
            if min_stake:
                stake = max(stake, min_stake)
            return stake

        return proposed_stake

import logging
from datetime import datetime, timezone
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

class StrategyORB:
    """
    Multi-Asset Opening Range Breakout (ORB) Strategy.
    Designed specifically for Prop Firm Challenges (FundedNext) with strict risk caps.
    
    Rules:
    - Captures the High and Low of the first 15 minutes of the opening session.
    - Waits for the 15-minute range to solidify.
    - Triggers on confirmed breakout with volume expansion and directional RSI.
    - Limits to strictly MAXIMUM 1 TRADE PER ASSET PER DAY (Anti-Overtrading).
    - Session cut-off prevents choppy mid-day entries.
    """

    def __init__(self):
        self.trades_taken_today = {}  # { 'YYYY-MM-DD': { 'MNQZ6': True, 'MGCZ6': True } }
        self.range_cache = {}          # { 'YYYY-MM-DD': { 'MNQZ6': {'high': X, 'low': Y} } }

    def reset_daily(self, date_str: str):
        if date_str not in self.trades_taken_today:
            self.trades_taken_today[date_str] = {}
        if date_str not in self.range_cache:
            self.range_cache[date_str] = {}

    def analyze_bars(self, bars: list[dict], spec: dict = None, symbol: str = "MNQZ6") -> tuple[str, str, float, float, float, float]:
        """
        Analyzes 5m candles and returns:
        (signal: 'BUY'|'SELL'|'HOLD', tag: str, current_price: float, rsi: float, orb_high: float, orb_low: float)
        """
        if len(bars) < 15:
            return "HOLD", "", 0.0, 50.0, 0.0, 0.0

        spec = spec or {}
        range_start = spec.get("orb_start_utc", "13:30")
        range_end = spec.get("orb_end_utc", "13:45")
        session_end = spec.get("orb_cutoff_utc", "16:00")
        vol_mult = float(spec.get("vol_mult", 1.05))

        df = pd.DataFrame(bars)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["volume"] = df.get("upVolume", 0).astype(float) + df.get("downVolume", 0).astype(float)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # Relative Strength Index (RSI 14)
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        current_price = float(curr["close"])
        rsi_val = float(curr["rsi"]) if not np.isnan(curr["rsi"]) else 50.0

        ts_str = curr.get("timestamp", "")
        # Parse timestamp to UTC datetime
        try:
            if "T" in ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            else:
                dt = datetime.now(timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)

        date_str = dt.strftime("%Y-%m-%d")
        cur_hm = dt.strftime("%H:%M")
        self.reset_daily(date_str)

        # Check if already traded today on this symbol
        if self.trades_taken_today[date_str].get(symbol, False):
            return "HOLD", "daily_trade_completed", current_price, rsi_val, 0.0, 0.0

        # Calculate or retrieve Opening Range High/Low
        cached = self.range_cache[date_str].get(symbol)
        if cached:
            orb_high = cached["high"]
            orb_low = cached["low"]
        else:
            # Find bars that fall inside the opening range [range_start, range_end]
            range_bars = []
            for b in bars:
                b_ts = b.get("timestamp", "")
                if date_str in b_ts:
                    try:
                        b_dt = datetime.fromisoformat(b_ts.replace("Z", "+00:00"))
                        b_hm = b_dt.strftime("%H:%M")
                        if range_start <= b_hm < range_end:
                            range_bars.append(b)
                    except Exception:
                        pass

            if not range_bars:
                return "HOLD", "awaiting_opening_range", current_price, rsi_val, 0.0, 0.0

            orb_high = max(float(b["high"]) for b in range_bars)
            orb_low = min(float(b["low"]) for b in range_bars)

            # If range session has fully elapsed, lock the range in cache
            now_utc = datetime.now(timezone.utc)
            now_hm = now_utc.strftime("%H:%M")
            if now_hm >= range_end or cur_hm >= range_end:
                self.range_cache[date_str][symbol] = {"high": orb_high, "low": orb_low}
                logger.info(f"[{symbol}] {date_str} ORB Locked! High: {orb_high:.2f} | Low: {orb_low:.2f} (Range: {orb_high-orb_low:.2f} pts)")

        # Only evaluate breakout AFTER opening range has completed and BEFORE cutoff
        now_utc = datetime.now(timezone.utc)
        now_hm = now_utc.strftime("%H:%M")
        if not (range_end <= now_hm < session_end):
            return "HOLD", "outside_orb_window", current_price, rsi_val, orb_high, orb_low

        # Breakout Conditions
        vol_surge = curr["volume"] >= curr["vol_ma"] * float(spec.get("vol_mult", 0.90))

        # 1. Bullish Breakout (Long)
        if current_price > orb_high and vol_surge and rsi_val >= 48.0:
            logger.info(f"🚀 [{symbol}] ORB LONG BREAKOUT DETECTED! Price: {current_price:.2f} > ORB High {orb_high:.2f} | Vol: {curr['volume']:.0f} | RSI: {rsi_val:.1f}")
            return "BUY", f"orb_breakout_long_{symbol}", current_price, rsi_val, orb_high, orb_low

        # 2. Bearish Breakdown (Short)
        if current_price < orb_low and vol_surge and rsi_val <= 52.0:
            logger.info(f"🔻 [{symbol}] ORB SHORT BREAKDOWN DETECTED! Price: {current_price:.2f} < ORB Low {orb_low:.2f} | Vol: {curr['volume']:.0f} | RSI: {rsi_val:.1f}")
            return "SELL", f"orb_breakdown_short_{symbol}", current_price, rsi_val, orb_high, orb_low

        return "HOLD", "watching_orb", current_price, rsi_val, orb_high, orb_low

    def mark_trade_executed(self, symbol: str, date_str: str = None):
        """Marks that the 1 daily allowed trade for this symbol has been triggered."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.reset_daily(date_str)
        self.trades_taken_today[date_str][symbol] = True
        logger.info(f"[{symbol}] Daily trade recorded for {date_str}. Trading locked for this asset until tomorrow.")

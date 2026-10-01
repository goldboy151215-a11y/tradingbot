import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

class StrategyShield:
    def __init__(self):
        self.min_bars = 30

    def analyze_bars(self, bars: list[dict], spec: dict = None, symbol: str = "MNQ") -> tuple[str, str, float, float, float, float]:
        """
        Analyzes 5m candles and returns (signal: 'BUY'|'SELL'|'HOLD', tag: str, current_price: float, rsi: float, ema20: float, ema50: float).
        Uses pure pandas/numpy vector calculations for maximum speed and zero external C-dependencies.
        """
        if len(bars) < self.min_bars:
            logger.debug(f"Not enough bars ({len(bars)}/{self.min_bars})")
            return "HOLD", "", 0.0, 50.0, 0.0, 0.0

        spec = spec or {}
        vol_mult = float(spec.get("vol_mult", 1.05))
        rsi_min = float(spec.get("rsi_min", 50))
        rsi_max = float(spec.get("rsi_max", 74))

        df = pd.DataFrame(bars)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df.get("upVolume", 0).astype(float) + df.get("downVolume", 0).astype(float)

        # 1. Exponential Moving Averages (EMA 20 & 50)
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

        # 2. Relative Strength Index (RSI 14)
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        # 3. Bollinger Bands (20, 1.8 stdev)
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + (1.8 * df["bb_std"])
        df["bb_lower"] = df["bb_mid"] - (1.8 * df["bb_std"])

        # 4. Volume Moving Average
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # Current (forming/latest) and previous candles
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3] if len(df) >= 3 else prev
        current_price = float(curr["close"])
        rsi_val = float(curr["rsi"]) if not np.isnan(curr["rsi"]) else 50.0
        ema20_val = float(curr["ema20"]) if not np.isnan(curr["ema20"]) else current_price
        ema50_val = float(curr["ema50"]) if not np.isnan(curr["ema50"]) else current_price
        prev_ema20 = float(prev["ema20"]) if not np.isnan(prev["ema20"]) else ema20_val
        prev_ema50 = float(prev["ema50"]) if not np.isnan(prev["ema50"]) else ema50_val

        # 0. New York Open ORB Setup (13:45 - 16:00 UTC)
        ts_str = curr.get("timestamp", "")
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if "T" in ts_str else datetime.now(timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            cur_hm = dt.strftime("%H:%M")
        except Exception:
            cur_hm = ""
            date_str = ""

        if "13:45" <= cur_hm < "16:00":
            range_bars = [b for b in bars if b.get("timestamp", "").startswith(date_str) and "13:30" <= b.get("timestamp", "")[11:16] < "13:45"]
            if range_bars:
                orb_h = max(float(b["high"]) for b in range_bars)
                orb_l = min(float(b["low"]) for b in range_bars)
                vol_ok = curr["volume"] >= curr["vol_ma"] * 0.95

                if curr["close"] > orb_h and (prev["close"] <= orb_h or curr["open"] <= orb_h) and vol_ok and rsi_val >= 48.0:
                    logger.info(f"NY OPEN ORB LONG on {symbol}! Price: {current_price} > High {orb_h:.2f}")
                    return "BUY", f"ny_open_orb_long_{symbol}", current_price, rsi_val, ema20_val, ema50_val

                if curr["close"] < orb_l and (prev["close"] >= orb_l or curr["open"] >= orb_l) and vol_ok and rsi_val <= 52.0:
                    logger.info(f"NY OPEN ORB SHORT on {symbol}! Price: {current_price} < Low {orb_l:.2f}")
                    return "SELL", f"ny_open_orb_short_{symbol}", current_price, rsi_val, ema20_val, ema50_val

        # Long Setup: Bollinger Upper Breakout (Crossover or Strong Green Continuation) + Volume Surge + Bullish Trend
        crossover = (curr["close"] > curr["bb_upper"]) and (prev["close"] <= prev["bb_upper"])
        continuation = (
            (curr["close"] > curr["bb_upper"])
            and (curr["close"] > curr["open"])
            and (prev["close"] > prev["bb_upper"])
            and (prev2["close"] <= prev2["bb_upper"])
        )
        long_bb_cross = crossover or continuation
        long_vol = curr["volume"] > curr["vol_ma"] * vol_mult
        long_rsi = rsi_min <= rsi_val <= rsi_max
        long_trend = (ema20_val > ema50_val) and (ema20_val >= prev_ema20)

        if long_bb_cross and long_vol and long_rsi and long_trend:
            logger.info(f"LONG signal on {symbol}! Price: {current_price}, RSI: {rsi_val:.1f}")
            return "BUY", f"bb_breakout_long_{symbol}", current_price, rsi_val, ema20_val, ema50_val

        # Short Setup: EMA20 Pullback Rejection + Volume + Bearish Trend (Confirmed EMA Slope & RSI Cap <= 50)
        short_pullback = (curr["high"] >= ema20_val) and (curr["close"] < ema20_val)
        short_red = curr["close"] < curr["open"]
        short_vol = curr["volume"] > curr["vol_ma"] * vol_mult
        # RSI must be in true bearish momentum territory (max 50.0)
        short_rsi = (rsi_min - 4) <= rsi_val <= 50.0
        # EMA20 must be below EMA50 AND the EMA20 slope must be pointing downward (no V-bounce trap)
        short_trend = (ema20_val < ema50_val) and (ema20_val <= prev_ema20)

        if short_pullback and short_red and short_vol and short_rsi and short_trend:
            logger.info(f"SHORT signal on {symbol}! Price: {current_price}, RSI: {rsi_val:.1f}")
            return "SELL", f"bear_pullback_short_{symbol}", current_price, rsi_val, ema20_val, ema50_val

        return "HOLD", "", current_price, rsi_val, ema20_val, ema50_val

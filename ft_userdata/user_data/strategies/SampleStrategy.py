from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
from datetime import datetime
from freqtrade.persistence import Trade
from typing import Optional
import talib.abstract as ta
import numpy as np


class SampleStrategy(IStrategy):
    """
    SMC Fib POI - dichter bij Pine Script
    - Order Block zones
    - Discount filter
    - custom_stoploss onder OB low (echte RR)
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False
    use_custom_stoploss = True

    # Ruime "hard" stoploss als fallback
    stoploss = -0.06
    trailing_stop = False

    # ROI als TP-proxy (~1:2 bij ~2.5-3% risk)
    minimal_roi = {
        "0": 0.06,
        "30": 0.04,
        "90": 0.025,
        "180": 0.012,
    }

    swing_len = IntParameter(5, 15, default=8, space="buy", optimize=True)
    rsi_long_max = IntParameter(30, 50, default=45, space="buy", optimize=True)

    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # --- Swing High / Low (zoals ta.pivothigh / pivotlow) ---
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

        # Fib 50% → Premium / Discount
        dataframe["fib_mid"] = (dataframe["last_swing_high"] + dataframe["last_swing_low"]) / 2
        dataframe["in_discount"] = dataframe["close"] < dataframe["fib_mid"]

        # --- Bullish Order Block (exacte Pine-logica) ---
        # low[1] < low[2] and close[1] < open[1] and close > high[1] and close > open
        dataframe["bullish_ob"] = (
            (dataframe["low"].shift(1) < dataframe["low"].shift(2)) &
            (dataframe["close"].shift(1) < dataframe["open"].shift(1)) &
            (dataframe["close"] > dataframe["high"].shift(1)) &
            (dataframe["close"] > dataframe["open"])
        )

        # OB high/low van de candle die de OB vormt
        dataframe["ob_high"] = np.where(dataframe["bullish_ob"], dataframe["high"].shift(1), np.nan)
        dataframe["ob_low"]  = np.where(dataframe["bullish_ob"], dataframe["low"].shift(1), np.nan)

        # Houd OB een tijd geldig (zoals de boxes in Pine)
        dataframe["ob_high"] = dataframe["ob_high"].ffill(limit=25)
        dataframe["ob_low"]  = dataframe["ob_low"].ffill(limit=25)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Prijs raakt bestaande long POI + in discount + RSI ok
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

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> Optional[float]:
        """
        Zet stop net onder de Order Block low (zoals in Pine: low * 0.995)
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return self.stoploss

        # Laatste bekende OB low
        last_ob_low = dataframe["ob_low"].iloc[-1]
        if last_ob_low is None or np.isnan(last_ob_low):
            return self.stoploss

        # Stop ~0.5% onder de OB low
        stop_price = last_ob_low * 0.995

        # Omrekenen naar relatieve stoploss t.o.v. open rate
        if trade.open_rate > 0:
            relative_sl = (stop_price / trade.open_rate) - 1
            # Niet te ruim en niet positief
            if relative_sl < -0.005 and relative_sl > -0.08:
                return relative_sl

        return self.stoploss

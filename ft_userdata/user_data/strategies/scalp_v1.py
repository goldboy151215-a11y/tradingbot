# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
Candidate 1: Trend Pullback Scalper with 15m Trend Filter and 10x Leverage.
"""
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class scalp_v1(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True

    stoploss = -0.022
    use_custom_stoploss = False

    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True

    minimal_roi = {
        "0": 0.040,
        "15": 0.025,
        "30": 0.018,
        "60": 0.012,
    }

    startup_candle_count = 60

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
        return min(10.0, max_leverage)

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema_21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend
        bullish_htf = dataframe["close_15m"] > dataframe["ema_50_15m"]
        bearish_htf = dataframe["close_15m"] < dataframe["ema_50_15m"]

        # 5m Trend & Pullback Long
        long_pullback = (
            (dataframe["ema_9"] > dataframe["ema_21"]) &
            (dataframe["low"] <= dataframe["ema_21"]) &
            (dataframe["close"] > dataframe["ema_9"]) &
            (dataframe["close"] > dataframe["open"]) &
            (dataframe["rsi"] >= 45.0) & (dataframe["rsi"] <= 62.0) &
            (dataframe["volume"] > 0)
        )

        # 5m Trend & Pullback Short
        short_pullback = (
            (dataframe["ema_9"] < dataframe["ema_21"]) &
            (dataframe["high"] >= dataframe["ema_21"]) &
            (dataframe["close"] < dataframe["ema_9"]) &
            (dataframe["close"] < dataframe["open"]) &
            (dataframe["rsi"] >= 38.0) & (dataframe["rsi"] <= 55.0) &
            (dataframe["volume"] > 0)
        )

        long_cond = bullish_htf & long_pullback
        short_cond = bearish_htf & short_pullback

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "scalp_long"

        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "scalp_short"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

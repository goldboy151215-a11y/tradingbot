# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional, Dict
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class opt_long_short_1h_10x(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True

    stoploss = -0.02
    use_custom_stoploss = False

    trailing_stop = True
    trailing_stop_positive = 0.005
    trailing_stop_positive_offset = 0.01
    trailing_only_offset_is_reached = True

    minimal_roi = {"0": 0.03, "15": 0.018, "30": 0.012, "60": 0.006}

    startup_candle_count = 120

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
        return min(10.0, max_leverage) if max_leverage > 1.0 else 10.0

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_upper"] = bollinger["upperband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF trend filters
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]
        htf_1h_bull = dataframe["close_1h"] > dataframe["ema20_1h"]
        htf_1h_bear = dataframe["close_1h"] < dataframe["ema20_1h"]

        
        # Long: 1H EMA20 > EMA50, 5m dip to EMA20 and bounce
        long_cond = (dataframe["ema20_1h"] > dataframe["ema50_1h"]) &                     (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) &                     (dataframe["close"] > dataframe["open"]) & (dataframe["rsi"] >= 40) & (dataframe["rsi"] <= 65)
        # Short: 1H EMA20 < EMA50, 5m rally to EMA20 and reject
        short_cond = (dataframe["ema20_1h"] < dataframe["ema50_1h"]) &                      (dataframe["high"] >= dataframe["ema20"]) & (dataframe["close"] < dataframe["ema20"]) &                      (dataframe["close"] < dataframe["open"]) & (dataframe["rsi"] >= 35) & (dataframe["rsi"] <= 60)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "scalp_long"
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "scalp_short"
        

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

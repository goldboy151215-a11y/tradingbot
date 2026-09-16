# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class macd_scalp_5x(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False

    stoploss = -0.09
    trailing_stop = False
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True
    use_custom_stoploss = False

    minimal_roi = {"0": 0.2, "20": 0.12, "40": 0.07}
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min(5.0, max_leverage) if max_leverage > 1.0 else 5.0

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 4,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.12,
            },
        ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]
        htf_1h_bull = dataframe["close_1h"] > dataframe["ema20_1h"]

        
        macd_cross_up = (dataframe["macd"] > dataframe["macdsignal"]) & (dataframe["macd"].shift(1) <= dataframe["macdsignal"].shift(1))
        macd_positive = dataframe["macdhist"] > 0
        trend_ok = dataframe["close"] > dataframe["ema50"]
        rsi_ok = (dataframe["rsi"] > 45) & (dataframe["rsi"] < 68)
        vol_ok = dataframe["volume"] > dataframe["vol_ma"] * 0.8
        long_cond = macd_cross_up & macd_positive & trend_ok & rsi_ok & vol_ok
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "macd_cross_long"
        

        if self.can_short:
            
        macd_cross_down = (dataframe["macd"] < dataframe["macdsignal"]) & (dataframe["macd"].shift(1) >= dataframe["macdsignal"].shift(1))
        macd_neg = dataframe["macdhist"] < 0
        trend_bear = dataframe["close"] < dataframe["ema50"]
        rsi_ok = (dataframe["rsi"] < 55) & (dataframe["rsi"] > 32)
        vol_ok = dataframe["volume"] > dataframe["vol_ma"] * 0.8
        short_cond = macd_cross_down & macd_neg & trend_bear & rsi_ok & vol_ok
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "macd_cross_short"
        

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < 1

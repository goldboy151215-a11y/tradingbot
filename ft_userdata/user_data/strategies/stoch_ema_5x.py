# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class stoch_ema_5x(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False

    stoploss = -0.09
    trailing_stop = False
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True
    use_custom_stoploss = False

    minimal_roi = {"0": 0.18, "15": 0.1, "35": 0.06}
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
        
        dataframe["ema8"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["ema14"] = ta.EMA(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        
        # StochRSI
        fastk, fastd = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
        dataframe["stoch_k"] = fastk
        dataframe["stoch_d"] = fastd
        
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]
        htf_1h_bull = dataframe["close_1h"] > dataframe["ema20_1h"]

        
        # Trend: 8 > 14 > 50
        bullish_ribbon = (dataframe["ema8"] > dataframe["ema14"]) & (dataframe["ema14"] > dataframe["ema50"])
        # Dip into EMA14/EMA50 + StochRSI crossing up from oversold (<35)
        stoch_cross = (dataframe["stoch_k"] > dataframe["stoch_d"]) & (dataframe["stoch_k"].shift(1) <= dataframe["stoch_d"].shift(1)) & (dataframe["stoch_k"] < 40)
        pullback = (dataframe["low"] <= dataframe["ema14"]) & (dataframe["close"] > dataframe["ema14"])
        vol_ok = dataframe["volume"] > dataframe["vol_ma"] * 0.7
        long_cond = bullish_ribbon & stoch_cross & pullback & vol_ok
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "stoch_ema_pullback"
        

        if self.can_short:
            
        bearish_ribbon = (dataframe["ema8"] < dataframe["ema14"]) & (dataframe["ema14"] < dataframe["ema50"])
        stoch_cross_down = (dataframe["stoch_k"] < dataframe["stoch_d"]) & (dataframe["stoch_k"].shift(1) >= dataframe["stoch_d"].shift(1)) & (dataframe["stoch_k"] > 60)
        rally = (dataframe["high"] >= dataframe["ema14"]) & (dataframe["close"] < dataframe["ema14"])
        vol_ok = dataframe["volume"] > dataframe["vol_ma"] * 0.7
        short_cond = bearish_ribbon & stoch_cross_down & rally & vol_ok
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "stoch_ema_short"
        

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < 1

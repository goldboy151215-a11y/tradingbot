#!/usr/bin/env python3
import os
import subprocess
import json
import glob
import zipfile
import time

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"

TEMPLATE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional, Dict
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class {class_name}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "{timeframe}"
    can_short = {can_short}

    stoploss = {stoploss}
    use_custom_stoploss = False

    trailing_stop = {trailing_stop}
    trailing_stop_positive = {trail_pos}
    trailing_stop_positive_offset = {trail_offset}
    trailing_only_offset_is_reached = True

    minimal_roi = {minimal_roi}

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
        return min({leverage}, max_leverage) if max_leverage > 1.0 else {leverage}

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

        {entry_logic}

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe
"""

def generate_and_test(config):
    name = config["name"]
    file_path = os.path.join(STRATEGIES_DIR, f"{name}.py")
    
    code = TEMPLATE.format(
        class_name=name,
        timeframe=config.get("timeframe", "5m"),
        can_short=str(config.get("can_short", False)),
        stoploss=config.get("stoploss", -0.02),
        trailing_stop=str(config.get("trailing_stop", True)),
        trail_pos=config.get("trail_pos", 0.005),
        trail_offset=config.get("trail_offset", 0.010),
        minimal_roi=json.dumps(config.get("minimal_roi", {"0": 0.025, "15": 0.015, "30": 0.008})),
        leverage=config.get("leverage", 10.0),
        entry_logic=config.get("entry_logic", "")
    )
    
    with open(file_path, "w") as f:
        f.write(code)
    
    # Run backtest
    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", name,
        "--timeframe", config.get("timeframe", "5m"),
        "--timerange", "20260817-20260914",
        "--enable-protections",
        "--export", "trades",
    ]
    
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {"name": name, "error": proc.stderr[:300]}
    
    zips = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.zip")), key=os.path.getmtime)
    if not zips:
        return {"name": name, "error": "No result zip"}
    
    with zipfile.ZipFile(zips[-1]) as z:
        for f in z.namelist():
            if f.endswith(".json") and not f.endswith("_config.json"):
                data = json.loads(z.read(f).decode())
                s = data.get("strategy", {}).get(name, {})
                return {
                    "name": name,
                    "trades": s.get("total_trades", 0),
                    "wins": s.get("wins", 0),
                    "losses": s.get("losses", 0),
                    "winrate": round(s.get("winrate", 0) * 100, 1),
                    "profit_abs": round(s.get("profit_total_abs", 0), 2),
                    "profit_pct": round(s.get("profit_total", 0) * 100, 2),
                    "profit_factor": round(s.get("profit_factor", 0), 2) if s.get("profit_factor") else 0,
                    "drawdown": round(s.get("max_drawdown_account", 0) * 100, 2),
                    "holding_avg": s.get("holding_avg", "N/A"),
                }
    return {"name": name, "error": "No strat data"}

# Define candidates
CANDIDATES = [
    # 1. EMA Cross with 4H Trend (similar to ema_trend_long, but 5m execution, 10x leverage, tight scalp ROI)
    {
        "name": "opt_ema_4h_scalp_10x",
        "timeframe": "5m",
        "can_short": False,
        "leverage": 10.0,
        "stoploss": -0.025,
        "trailing_stop": True,
        "trail_pos": 0.006,
        "trail_offset": 0.012,
        "minimal_roi": {"0": 0.04, "10": 0.025, "25": 0.015, "45": 0.008},
        "entry_logic": """
        cross_above = (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        ema_trend = dataframe["ema20"] > dataframe["ema50"]
        long_cond = cross_above & ema_trend & htf_4h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_cross_4h"
        """
    },
    # 2. EMA Pullback & Bounce with 4H Trend (Buying the dip to EMA20 on 5m)
    {
        "name": "opt_ema_pullback_4h_10x",
        "timeframe": "5m",
        "can_short": False,
        "leverage": 10.0,
        "stoploss": -0.020,
        "trailing_stop": True,
        "trail_pos": 0.005,
        "trail_offset": 0.010,
        "minimal_roi": {"0": 0.035, "15": 0.020, "30": 0.012, "60": 0.007},
        "entry_logic": """
        dip = (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"] > dataframe["open"])
        ema_trend = dataframe["ema20"] > dataframe["ema50"]
        rsi_ok = (dataframe["rsi"] >= 42.0) & (dataframe["rsi"] <= 65.0)
        long_cond = dip & ema_trend & htf_4h_bull & rsi_ok & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_dip_4h"
        """
    },
    # 3. BB Mean Reversion with 1H Trend filter
    {
        "name": "opt_bb_mean_rev_1h_10x",
        "timeframe": "5m",
        "can_short": False,
        "leverage": 10.0,
        "stoploss": -0.020,
        "trailing_stop": True,
        "trail_pos": 0.005,
        "trail_offset": 0.010,
        "minimal_roi": {"0": 0.030, "10": 0.018, "20": 0.012, "40": 0.006},
        "entry_logic": """
        bb_bounce = (dataframe["low"] <= dataframe["bb_lower"]) & (dataframe["close"] > dataframe["bb_lower"]) & (dataframe["close"] > dataframe["open"])
        rsi_oversold = (dataframe["rsi"] < 40.0) & (dataframe["rsi"] > 25.0)
        long_cond = bb_bounce & rsi_oversold & htf_1h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_dip_1h"
        """
    },
    # 4. Long + Short Scalper with 1H EMA20/50 regime
    {
        "name": "opt_long_short_1h_10x",
        "timeframe": "5m",
        "can_short": True,
        "leverage": 10.0,
        "stoploss": -0.020,
        "trailing_stop": True,
        "trail_pos": 0.005,
        "trail_offset": 0.010,
        "minimal_roi": {"0": 0.030, "15": 0.018, "30": 0.012, "60": 0.006},
        "entry_logic": """
        # Long: 1H EMA20 > EMA50, 5m dip to EMA20 and bounce
        long_cond = (dataframe["ema20_1h"] > dataframe["ema50_1h"]) & \
                    (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) & \
                    (dataframe["close"] > dataframe["open"]) & (dataframe["rsi"] >= 40) & (dataframe["rsi"] <= 65)
        # Short: 1H EMA20 < EMA50, 5m rally to EMA20 and reject
        short_cond = (dataframe["ema20_1h"] < dataframe["ema50_1h"]) & \
                     (dataframe["high"] >= dataframe["ema20"]) & (dataframe["close"] < dataframe["ema20"]) & \
                     (dataframe["close"] < dataframe["open"]) & (dataframe["rsi"] >= 35) & (dataframe["rsi"] <= 60)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "scalp_long"
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "scalp_short"
        """
    }
]

if __name__ == "__main__":
    for cand in CANDIDATES:
        print(f"Testing {cand['name']}...")
        res = generate_and_test(cand)
        print(json.dumps(res, indent=2))

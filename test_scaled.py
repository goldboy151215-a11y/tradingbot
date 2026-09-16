#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"

TEMPLATE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class {name}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "{tf}"
    can_short = {can_short}

    stoploss = {sl}
    trailing_stop = {trailing_stop}
    trailing_stop_positive = {trail_pos}
    trailing_stop_positive_offset = {trail_offset}
    trailing_only_offset_is_reached = True
    use_custom_stoploss = False

    minimal_roi = {roi}

    startup_candle_count = 120

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min({leverage}, max_leverage) if max_leverage > 1.0 else {leverage}

    @property
    def protections(self):
        return [
            {{
                "method": "CooldownPeriod",
                "stop_duration_candles": 4,
            }},
            {{
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 5,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.12,
            }},
        ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_upper"] = bollinger["upperband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]
        htf_1h_bull = dataframe["ema20_1h"] > dataframe["ema50_1h"]
        htf_1h_bear = dataframe["ema20_1h"] < dataframe["ema50_1h"]

        {entry_logic}

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < {max_trades}
"""

def test_variant(params):
    name = params["name"]
    code = TEMPLATE.format(
        name=name,
        tf=params.get("tf", "5m"),
        can_short=str(params.get("can_short", False)),
        sl=params.get("sl", -0.05),
        trailing_stop=str(params.get("trailing_stop", False)),
        trail_pos=params.get("trail_pos", 0.01),
        trail_offset=params.get("trail_offset", 0.02),
        roi=json.dumps(params.get("roi", {"0": 0.10, "60": 0.05})),
        leverage=params.get("leverage", 5.0),
        max_trades=params.get("max_trades", 2),
        entry_logic=params.get("entry_logic", "")
    )
    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", name,
        "--timeframe", params.get("tf", "5m"),
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"{name} ERROR: {proc.stderr[-300:]}")
        return None

    zips = sorted(glob.glob(f"{RESULTS_DIR}/*.zip"), key=os.path.getmtime)
    with zipfile.ZipFile(zips[-1]) as z:
        for fname in z.namelist():
            if fname.endswith(".json") and not fname.endswith("_config.json"):
                data = json.loads(z.read(fname).decode())
                s = data.get("strategy", {}).get(name, {})
                t = s.get("total_trades", 0)
                w = s.get("wins", 0)
                l = s.get("losses", 0)
                wr = round(s.get("winrate", 0) * 100, 1)
                p_abs = round(s.get("profit_total_abs", 0), 2)
                p_pct = round(s.get("profit_total", 0) * 100, 2)
                pf = round(s.get("profit_factor", 0), 2) if s.get("profit_factor") else 0
                dd = round(s.get("max_drawdown_account", 0) * 100, 2)
                hold = s.get("holding_avg", "N/A")
                print(f"{name} (lev {params.get('leverage')}x, {params.get('tf')}): Trades={t}, W={w}, L={l}, Win%={wr}%, Profit=${p_abs} ({p_pct}%), PF={pf}, DD={dd}%, AvgHold={hold}")
                return s

EXPERIMENTS = [
    # Baseline EMA 4H trend, 1x leverage, 5m TF
    {
        "name": "exp_ema4h_1x",
        "tf": "5m",
        "leverage": 1.0,
        "can_short": False,
        "sl": -0.0631,
        "trailing_stop": False,
        "roi": {"0": 0.12, "60": 0.08, "120": 0.04},
        "max_trades": 1,
        "entry_logic": """
        cross = (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        long_cond = cross & ema_ok & htf_4h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_cross"
        """
    },
    # Scaled EMA 4H trend, 3x leverage, 5m TF
    {
        "name": "exp_ema4h_3x",
        "tf": "5m",
        "leverage": 3.0,
        "can_short": False,
        "sl": -0.10,  # -3.3% price drop at 3x
        "trailing_stop": False,
        "roi": {"0": 0.18, "30": 0.10, "60": 0.06}, # +6% to +2% price move
        "max_trades": 1,
        "entry_logic": """
        cross = (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        long_cond = cross & ema_ok & htf_4h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_cross"
        """
    },
    # Scaled EMA 4H trend, 5x leverage, 5m TF
    {
        "name": "exp_ema4h_5x",
        "tf": "5m",
        "leverage": 5.0,
        "can_short": False,
        "sl": -0.12,  # -2.4% price drop at 5x
        "trailing_stop": False,
        "roi": {"0": 0.25, "30": 0.15, "60": 0.08}, # +5% to +1.6% price move
        "max_trades": 1,
        "entry_logic": """
        cross = (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        long_cond = cross & ema_ok & htf_4h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_cross"
        """
    },
    # Scaled EMA 4H trend, 10x leverage, 5m TF
    {
        "name": "exp_ema4h_10x",
        "tf": "5m",
        "leverage": 10.0,
        "can_short": False,
        "sl": -0.15,  # -1.5% price drop at 10x
        "trailing_stop": False,
        "roi": {"0": 0.40, "30": 0.25, "60": 0.15}, # +4% to +1.5% price move
        "max_trades": 1,
        "entry_logic": """
        cross = (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        long_cond = cross & ema_ok & htf_4h_bull & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "ema_cross"
        """
    },
    # Pullback to EMA20 with 4H Trend, 5x leverage
    {
        "name": "exp_pullback4h_5x",
        "tf": "5m",
        "leverage": 5.0,
        "can_short": False,
        "sl": -0.10,
        "trailing_stop": False,
        "roi": {"0": 0.20, "20": 0.12, "45": 0.07},
        "max_trades": 1,
        "entry_logic": """
        dip = (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"] > dataframe["open"])
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        rsi_ok = (dataframe["rsi"] >= 40) & (dataframe["rsi"] <= 60)
        long_cond = dip & ema_ok & htf_4h_bull & rsi_ok & (dataframe["volume"] > 0)
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "pullback_dip"
        """
    }
]

if __name__ == "__main__":
    for exp in EXPERIMENTS:
        test_variant(exp)

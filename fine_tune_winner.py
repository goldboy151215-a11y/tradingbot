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
    timeframe = "5m"
    can_short = False

    stoploss = {sl}
    trailing_stop = False
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
                "stop_duration_candles": {cooldown},
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

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]

        dip = (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"] > dataframe["open"])
        ema_ok = dataframe["ema20"] > dataframe["ema50"]
        rsi_ok = (dataframe["rsi"] >= {rsi_min}) & (dataframe["rsi"] <= {rsi_max})
        vol_ok = dataframe["volume"] >= dataframe["vol_ma"] * {vol_factor}

        long_cond = dip & ema_ok & htf_4h_bull & rsi_ok & vol_ok
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "pullback_dip"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < 1
"""

def test_tune(p):
    name = p["name"]
    code = TEMPLATE.format(
        name=name,
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        leverage=p["leverage"],
        cooldown=p.get("cooldown", 4),
        rsi_min=p.get("rsi_min", 40),
        rsi_max=p.get("rsi_max", 60),
        vol_factor=p.get("vol_factor", 0.8),
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
        "--timeframe", "5m",
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"{name} ERROR")
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
                print(f"{name}: Lev={p['leverage']}x, SL={p['sl']}, Trades={t}, W={w}, L={l}, Win%={wr}%, Profit=${p_abs} ({p_pct}%), PF={pf}, DD={dd}%, AvgHold={hold}")
                return s

CANDIDATES = [
    # Baseline winner: 5x, SL -10% (-2% price), ROI {0: 0.20, 20: 0.12, 45: 0.07}
    {
        "name": "tune_winner_base",
        "leverage": 5.0,
        "sl": -0.10,
        "roi": {"0": 0.20, "20": 0.12, "45": 0.07},
        "rsi_min": 40, "rsi_max": 60, "vol_factor": 0.8
    },
    # Variation A: 4x leverage (even lower drawdown and risk)
    {
        "name": "tune_4x_safe",
        "leverage": 4.0,
        "sl": -0.09, # -2.25% price
        "roi": {"0": 0.18, "20": 0.10, "45": 0.06},
        "rsi_min": 40, "rsi_max": 60, "vol_factor": 0.8
    },
    # Variation B: 5x with slightly wider stop -12% (-2.4% price) to prevent wick stopouts
    {
        "name": "tune_5x_widersl",
        "leverage": 5.0,
        "sl": -0.12,
        "roi": {"0": 0.22, "20": 0.13, "45": 0.07},
        "rsi_min": 40, "rsi_max": 60, "vol_factor": 0.8
    },
    # Variation C: 5x with tighter RSI window (42 to 58) for highest quality entries
    {
        "name": "tune_5x_rsi_tight",
        "leverage": 5.0,
        "sl": -0.10,
        "roi": {"0": 0.20, "20": 0.12, "45": 0.07},
        "rsi_min": 42, "rsi_max": 58, "vol_factor": 0.8
    },
    # Variation D: 6x leverage, SL -11%, ROI {0: 0.24, 20: 0.14, 45: 0.08}
    {
        "name": "tune_6x_balanced",
        "leverage": 6.0,
        "sl": -0.11,
        "roi": {"0": 0.24, "20": 0.14, "45": 0.08},
        "rsi_min": 40, "rsi_max": 60, "vol_factor": 0.8
    },
    # Variation E: 5x with 8 candle cooldown after SL
    {
        "name": "tune_5x_cooldown8",
        "leverage": 5.0,
        "sl": -0.10,
        "roi": {"0": 0.20, "20": 0.12, "45": 0.07},
        "cooldown": 8,
        "rsi_min": 40, "rsi_max": 60, "vol_factor": 0.8
    },
]

if __name__ == "__main__":
    for c in CANDIDATES:
        test_tune(c)

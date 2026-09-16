#!/usr/bin/env python3
"""
Comprehensive Pairlist Comparison Engine: 17 Coins vs 9 Coins vs Core 4 Coins.
Tests across:
- Fixed Stake ($100)
- Auto-Compounding (58%) on $168 capital
- Auto-Compounding (58%) on €2000 capital
"""

import subprocess
import glob
import os
import zipfile
import json
import time

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"
CONFIG_PATH = "/root/ft_userdata/user_data/config-backtest.json"

PAIRS_17 = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "AVAX/USDT:USDT",
    "BNB/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "SUI/USDT:USDT",
    "NEAR/USDT:USDT", "LINK/USDT:USDT", "ARB/USDT:USDT", "ADA/USDT:USDT",
    "DOT/USDT:USDT", "LTC/USDT:USDT", "BCH/USDT:USDT"
]

PAIRS_9 = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "AVAX/USDT:USDT",
    "BNB/USDT:USDT", "DOGE/USDT:USDT", "SUI/USDT:USDT", "NEAR/USDT:USDT",
    "LINK/USDT:USDT"
]

PAIRS_4 = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "AVAX/USDT:USDT"
]

TEMPLATE_STRATEGY = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
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

    stoploss = -0.20
    trailing_stop = False
    use_custom_stoploss = False

    minimal_roi = {{
        "0": 0.44,
        "15": 0.24,
        "30": 0.12,
    }}
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min(12.0, max_leverage) if max_leverage > 1.0 else 12.0

    @property
    def protections(self):
        return [
            {{
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.35,
            }}
        ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=1.8, nbdevdn=1.8)
        dataframe["bb_upper"] = boll["upperband"]
        dataframe["bb_middle"] = boll["middleband"]
        dataframe["bb_lower"] = boll["lowerband"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]

        breakout = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * 1.2
        rsi_ok = (dataframe["rsi"] > 52) & (dataframe["rsi"] < 70)

        long_cond = breakout & vol_surge & rsi_ok & htf_4h_bull

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_breakout_scalp"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe
"""

EXPERIMENTS = [
    # 1. 17 Munten - Vaste $100 Stake
    {
        "name": "comp_17pairs_fixed100",
        "desc": "A1. 17 Volatiele Munten | Vaste $100 Stake",
        "pairs": PAIRS_17, "wallet": 168.0, "stake": 100.0, "ratio": 0.99
    },
    # 2. 9 Munten - Vaste $100 Stake
    {
        "name": "comp_9pairs_fixed100",
        "desc": "B1. 9 Top-Tier Munten | Vaste $100 Stake",
        "pairs": PAIRS_9, "wallet": 168.0, "stake": 100.0, "ratio": 0.99
    },
    # 3. Core 4 Munten - Vaste $100 Stake
    {
        "name": "comp_4pairs_fixed100",
        "desc": "C1. Core 4 Munten (BTC, ETH, SOL, AVAX) | Vaste $100 Stake",
        "pairs": PAIRS_4, "wallet": 168.0, "stake": 100.0, "ratio": 0.99
    },
    # 4. 17 Munten - 58% Compounding ($168 Start)
    {
        "name": "comp_17pairs_comp58_168",
        "desc": "A2. 17 Volatiele Munten | 58% Auto-Compounding ($168 Start)",
        "pairs": PAIRS_17, "wallet": 168.0, "stake": "unlimited", "ratio": 0.58
    },
    # 5. 9 Munten - 58% Compounding ($168 Start)
    {
        "name": "comp_9pairs_comp58_168",
        "desc": "B2. 9 Top-Tier Munten | 58% Auto-Compounding ($168 Start)",
        "pairs": PAIRS_9, "wallet": 168.0, "stake": "unlimited", "ratio": 0.58
    },
    # 6. Core 4 Munten - 58% Compounding ($168 Start)
    {
        "name": "comp_4pairs_comp58_168",
        "desc": "C2. Core 4 Munten | 58% Auto-Compounding ($168 Start)",
        "pairs": PAIRS_4, "wallet": 168.0, "stake": "unlimited", "ratio": 0.58
    },
    # 7. 17 Munten - 58% Compounding (€2000 Start)
    {
        "name": "comp_17pairs_comp58_2000",
        "desc": "A3. 17 Volatiele Munten | 58% Compounding (€2000 Start)",
        "pairs": PAIRS_17, "wallet": 2000.0, "stake": "unlimited", "ratio": 0.58
    },
    # 8. 9 Munten - 58% Compounding (€2000 Start)
    {
        "name": "comp_9pairs_comp58_2000",
        "desc": "B3. 9 Top-Tier Munten | 58% Compounding (€2000 Start)",
        "pairs": PAIRS_9, "wallet": 2000.0, "stake": "unlimited", "ratio": 0.58
    },
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE_STRATEGY.format(name=name)

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = p["wallet"]
    cfg["stake_amount"] = p["stake"]
    cfg["tradable_balance_ratio"] = p["ratio"]
    cfg["amend_last_stake_amount"] = True
    cfg["exchange"]["pair_whitelist"] = p["pairs"]

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)

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
        return {"name": name, "error": proc.stderr[-250:]}

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
                trades_per_day = round(s.get("trades_per_day", 0), 2)
                return {
                    "name": name,
                    "desc": p["desc"],
                    "pairs_count": len(p["pairs"]),
                    "wallet": p["wallet"],
                    "trades": t,
                    "trades_per_day": trades_per_day,
                    "wins": w,
                    "losses": l,
                    "winrate": wr,
                    "final_balance": round(p["wallet"] + p_abs, 2),
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown": dd,
                    "holding_avg": hold
                }

print("=== STARTING 17 PAIRS vs 9 PAIRS vs 4 PAIRS BACKTEST ===")
results = []
for p in EXPERIMENTS:
    print(f"Testing: {p['desc']}...", end="", flush=True)
    t0 = time.time()
    res = run_test(p)
    dt = time.time() - t0
    if "error" in res:
        print(f" ERROR: {res['error']}")
    else:
        print(f" DONE ({dt:.1f}s) -> Trades={res['trades']} ({res['trades_per_day']}/dag) | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Winst=${res['profit_abs']} ({res['profit_pct']}%) | DD={res['drawdown']}%")
        results.append(res)

with open("/root/pairs_comparison_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== COMPLETE! Results written to /root/pairs_comparison_results.json ===")

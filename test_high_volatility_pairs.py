#!/usr/bin/env python3
"""
High Volatility Pairs + Dynamic Compounding Backtest Engine.
Tests:
1. Baseline pairs (BTC, ETH, SOL)
2. High-Beta Volatile Selection (SOL, DOGE, AVAX, XRP, BNB, ETH, BTC)
3. Dynamic Compounding Ratios (40%, 50%, 60%, 70% of current equity per trade)
4. Order pricing optimization (Limit Maker)
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
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min({leverage}, max_leverage) if max_leverage > 1.0 else {leverage}

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

        boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup={bb_std}, nbdevdn={bb_std})
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
    # 1. 3 Major Pairs (ETH, SOL, BTC) + 50% Compounding
    {
        "name": "vol_3pairs_comp50",
        "desc": "1. 3 Grote Paren (BTC, ETH, SOL) + 50% Compounding",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "tradable_ratio": 0.50
    },
    # 2. 6 Volatiele Paren (BTC, ETH, SOL, DOGE, AVAX, XRP) + 50% Compounding
    {
        "name": "vol_6pairs_comp50",
        "desc": "2. 6 Volatiele Paren (SOL, DOGE, AVAX, XRP, ETH, BTC) + 50% Compounding",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"],
        "tradable_ratio": 0.50
    },
    # 3. 6 Volatiele Paren + 60% Compounding
    {
        "name": "vol_6pairs_comp60",
        "desc": "3. 6 Volatiele Paren + 60% Compounding (Snelle Winstgroei)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"],
        "tradable_ratio": 0.60
    },
    # 4. 6 Volatiele Paren + 70% Compounding (Maximale Snelheid)
    {
        "name": "vol_6pairs_comp70",
        "desc": "4. 6 Volatiele Paren + 70% Compounding (Hardcore Groei)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"],
        "tradable_ratio": 0.70
    },
    # 5. Top 3 Ultra-Volatiel (SOL, DOGE, XRP) + 60% Compounding
    {
        "name": "vol_ultra3_comp60",
        "desc": "5. Top 3 Ultra-Volatiel (SOL, DOGE, XRP) + 60% Compounding",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "pairs": ["SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"],
        "tradable_ratio": 0.60
    }
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE.format(
        name=name,
        leverage=p["leverage"],
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        bb_std=p["bb_std"]
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = 168.0
    cfg["stake_amount"] = "unlimited"
    cfg["tradable_balance_ratio"] = p["tradable_ratio"]
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
                    "pairs": p["pairs"],
                    "ratio": p["tradable_ratio"],
                    "trades": t,
                    "trades_per_day": trades_per_day,
                    "wins": w,
                    "losses": l,
                    "winrate": wr,
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown": dd,
                    "holding_avg": hold
                }

print("=== STARTING VOLATILE PAIRS + COMPOUNDING EXPERIMENT ===")
results = []
for p in EXPERIMENTS:
    print(f"Testing: {p['desc']}...", end="", flush=True)
    t0 = time.time()
    res = run_test(p)
    dt = time.time() - t0
    if "error" in res:
        print(f" ERROR: {res['error']}")
    else:
        print(f" DONE ({dt:.1f}s) -> Trades={res['trades']} | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Winst=${res['profit_abs']} ({res['profit_pct']}%) | PF={res['profit_factor']} | DD={res['drawdown']}%")
        results.append(res)

with open("/root/volatile_pairs_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== EXPERIMENT COMPLETE! Results saved to /root/volatile_pairs_results.json ===")

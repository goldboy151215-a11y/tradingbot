#!/usr/bin/env python3
"""
Higher Leverage Scaling Grid Search.
Tests scaling the champion BB squeeze breakout strategy from 6x up to 50x leverage.
Compares:
1. Fixed Price Stop (-1.67% price drop, allowing higher ROE swing at higher leverage to avoid noise stopouts)
2. Fixed ROE Stop (-10% ROE, tighter price stop)
3. Trailing Stops vs Fixed ROI ladders
4. Stake sizing ($100 fixed vs dynamic compounding)
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

    stoploss = {sl}
    trailing_stop = {trailing}
    {trailing_params}
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
    # 1. Benchmark (6x Leverage, $100 stake)
    {
        "name": "scale_6x_benchmark",
        "desc": "6x Hefboom (Benchmark) | SL -10% ROE (-1.67% prijs)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    # 2. 7x Leverage (Fixed price stop vs fixed ROE stop)
    {
        "name": "scale_7x_fixed_price_sl",
        "desc": "7x Hefboom | Vaste Prijs Stop (-1.67% = -11.7% ROE)",
        "leverage": 7.0, "sl": -0.117, "roi": {"0": 0.25, "15": 0.14, "30": 0.07},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    # 3. 8x Leverage (Fixed price stop vs fixed ROE stop)
    {
        "name": "scale_8x_fixed_price_sl",
        "desc": "8x Hefboom | Vaste Prijs Stop (-1.67% = -13.3% ROE)",
        "leverage": 8.0, "sl": -0.133, "roi": {"0": 0.28, "15": 0.16, "30": 0.08},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    {
        "name": "scale_8x_tight_roe_sl",
        "desc": "8x Hefboom | Strakke ROE Stop (-10% ROE = -1.25% prijs)",
        "leverage": 8.0, "sl": -0.10, "roi": {"0": 0.28, "15": 0.16, "30": 0.08},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    # 4. 10x Leverage (Fixed price stop vs fixed ROE stop)
    {
        "name": "scale_10x_fixed_price_sl",
        "desc": "10x Hefboom | Vaste Prijs Stop (-1.67% = -16.7% ROE)",
        "leverage": 10.0, "sl": -0.167, "roi": {"0": 0.35, "15": 0.20, "30": 0.10},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    {
        "name": "scale_10x_trailing",
        "desc": "10x Hefboom | Trailing Stop (1.5% offset, 0.8% trail)",
        "leverage": 10.0, "sl": -0.15, "roi": {"0": 0.40, "30": 0.20, "60": 0.10},
        "trailing": True, "trailing_params": "trailing_stop_positive = 0.08\ntrailing_stop_positive_offset = 0.15\ntrailing_only_offset_is_reached = True", "stake": 100.0
    },
    # 5. 12x Leverage
    {
        "name": "scale_12x_fixed_price_sl",
        "desc": "12x Hefboom | Vaste Prijs Stop (-1.67% = -20% ROE)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.42, "15": 0.24, "30": 0.12},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    # 6. 15x Leverage
    {
        "name": "scale_15x_fixed_price_sl",
        "desc": "15x Hefboom | Vaste Prijs Stop (-1.67% = -25% ROE)",
        "leverage": 15.0, "sl": -0.25, "roi": {"0": 0.50, "15": 0.30, "30": 0.15},
        "trailing": False, "trailing_params": "", "stake": 100.0
    },
    # 7. 20x Leverage (Fixed Price Stop vs Tight Stop)
    {
        "name": "scale_20x_fixed_price_sl",
        "desc": "20x Hefboom | Vaste Prijs Stop (-1.67% = -33.4% ROE)",
        "leverage": 20.0, "sl": -0.334, "roi": {"0": 0.65, "15": 0.40, "30": 0.20},
        "trailing": False, "trailing_params": "", "stake": 75.0
    },
    # 8. 25x Leverage (Fixed Price Stop)
    {
        "name": "scale_25x_fixed_price_sl",
        "desc": "25x Hefboom | Vaste Prijs Stop (-1.67% = -41.7% ROE)",
        "leverage": 25.0, "sl": -0.417, "roi": {"0": 0.80, "15": 0.50, "30": 0.25},
        "trailing": False, "trailing_params": "", "stake": 60.0
    },
    # 9. 50x Leverage (WEEX Max Leverage with wide price stop)
    {
        "name": "scale_50x_weex_hardcore",
        "desc": "50x Hefboom | WEEX Hardcore Scalper ($40 stake)",
        "leverage": 50.0, "sl": -0.50, "roi": {"0": 1.10, "15": 0.60, "30": 0.30},
        "trailing": False, "trailing_params": "", "stake": 40.0
    }
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE_STRATEGY.format(
        name=name,
        leverage=p["leverage"],
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        trailing=p["trailing"],
        trailing_params=p["trailing_params"]
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = 168.0
    cfg["stake_amount"] = p["stake"]
    cfg["tradable_balance_ratio"] = 0.99

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
                    "leverage": p["leverage"],
                    "stake": p["stake"],
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

print("=== STARTING HIGHER LEVERAGE SCALING EXPERIMENT (6x to 50x) ===")
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

with open("/root/higher_leverage_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== HIGHER LEVERAGE SCALING EXPERIMENT COMPLETE ===")

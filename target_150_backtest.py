#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json
import time

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"
CONFIG_PATH = "/root/ft_userdata/user_data/config-backtest.json"

TEMPLATE_CODE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
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
                "max_allowed_drawdown": 0.25,
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
    # Baseline: $25 stake, 6x leverage, $168 capital
    {
        "name": "target_6x_stake25",
        "desc": "6x Hefboom | $25 Stake (Huidig)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "bb_std": 1.8, "stake_amount": 25.0, "wallet": 168.0, "max_open": 1
    },
    # Higher Stake on same 6x (Active capital utilization: $60 stake)
    {
        "name": "target_6x_stake60",
        "desc": "6x Hefboom | $60 Stake (35% saldo inzet)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "bb_std": 1.8, "stake_amount": 60.0, "wallet": 168.0, "max_open": 1
    },
    # Higher Stake on same 6x (Active capital utilization: $100 stake)
    {
        "name": "target_6x_stake100",
        "desc": "6x Hefboom | $100 Stake (60% saldo inzet)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "bb_std": 1.8, "stake_amount": 100.0, "wallet": 168.0, "max_open": 1
    },
    # Compounding / Unlimited stake (75% of account per trade)
    {
        "name": "target_6x_stake_compound75",
        "desc": "6x Hefboom | Compounding Stake (75% saldo)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "bb_std": 1.8, "stake_amount": "unlimited", "tradable_ratio": 0.75, "wallet": 168.0, "max_open": 1
    },
    # 10x Leverage + $60 Stake
    {
        "name": "target_10x_stake60",
        "desc": "10x Hefboom | $60 Stake | SL -10% ROE (-1.0% prijs)",
        "leverage": 10.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.14, "30": 0.07},
        "bb_std": 1.8, "stake_amount": 60.0, "wallet": 168.0, "max_open": 1
    },
    # 10x Leverage + $100 Stake
    {
        "name": "target_10x_stake100",
        "desc": "10x Hefboom | $100 Stake | SL -10% ROE",
        "leverage": 10.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.14, "30": 0.07},
        "bb_std": 1.8, "stake_amount": 100.0, "wallet": 168.0, "max_open": 1
    },
    # 10x Leverage + Compounding (75% saldo)
    {
        "name": "target_10x_compound75",
        "desc": "10x Hefboom | Compounding Stake (75% saldo)",
        "leverage": 10.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.14, "30": 0.07},
        "bb_std": 1.8, "stake_amount": "unlimited", "tradable_ratio": 0.75, "wallet": 168.0, "max_open": 1
    },
    # 15x Leverage + $50 Stake
    {
        "name": "target_15x_stake50",
        "desc": "15x Hefboom | $50 Stake | SL -10% ROE (-0.67% prijs)",
        "leverage": 15.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.15, "30": 0.08},
        "bb_std": 1.8, "stake_amount": 50.0, "wallet": 168.0, "max_open": 1
    },
    # 20x Leverage + $40 Stake
    {
        "name": "target_20x_stake40",
        "desc": "20x Hefboom | $40 Stake | SL -10% ROE (-0.50% prijs)",
        "leverage": 20.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.15, "30": 0.08},
        "bb_std": 1.8, "stake_amount": 40.0, "wallet": 168.0, "max_open": 1
    },
    # 20x Leverage + $75 Stake
    {
        "name": "target_20x_stake75",
        "desc": "20x Hefboom | $75 Stake | SL -10% ROE",
        "leverage": 20.0, "sl": -0.10, "roi": {"0": 0.25, "15": 0.15, "30": 0.08},
        "bb_std": 1.8, "stake_amount": 75.0, "wallet": 168.0, "max_open": 1
    },
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE_CODE.format(
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
    cfg["max_open_trades"] = p["max_open"]
    cfg["dry_run_wallet"] = p["wallet"]
    cfg["stake_amount"] = p["stake_amount"]
    if "tradable_ratio" in p:
        cfg["tradable_balance_ratio"] = p["tradable_ratio"]
    else:
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

print("=== RUNNING TARGET €100 - €150/MONTH PROFIT BACKTESTS ===")
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

with open("/root/target_150_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== COMPLETE! Results written to /root/target_150_results.json ===")

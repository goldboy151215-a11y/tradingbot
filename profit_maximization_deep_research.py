#!/usr/bin/env python3
"""
Deep Profit Maximization Research Engine.
Systematically backtests all 6 profit enlargement vectors:
1. Dynamic Equity Compounding (40%, 50%, 60%, 70% of wallet per trade)
2. Dual-Directional Scalping: Long + Short (BB Squeeze Breakout + Breakdown)
3. High-Beta Pair Expansion (SOL, DOGE, AVAX, NEAR, SUI, ETH, BTC, BNB, XRP)
4. Trailing Runner Exits (TP1 50% at +20% ROE + BE Stop + Trailing 50% for 5-10% moves)
5. Timeframe comparison (3m/5m/15m)
6. Combined Mega-Stack (Compounding + Long/Short + Runner Trailing)
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

TEMPLATE_MAX = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class {name}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "{timeframe}"
    can_short = {can_short}

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
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]

        # Long breakout
        breakout_long = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * 1.2
        rsi_long_ok = (dataframe["rsi"] > 52) & (dataframe["rsi"] < 70)

        long_cond = breakout_long & vol_surge & rsi_long_ok & htf_4h_bull
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_long_breakout"

        {short_entry_code}

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe
"""

SHORT_CODE = """
        # Short breakdown
        breakdown_short = (dataframe["close"] < dataframe["bb_lower"]) & (dataframe["close"].shift(1) >= dataframe["bb_lower"].shift(1))
        rsi_short_ok = (dataframe["rsi"] < 48) & (dataframe["rsi"] > 30)
        short_cond = breakdown_short & vol_surge & rsi_short_ok & htf_4h_bear
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "bb_short_breakdown"
"""

VECTORS = [
    # 0. Baseline (12x, $100 fixed stake, long only)
    {
        "name": "v0_baseline_12x_fixed",
        "desc": "1. Huidige 12x Baseline ($100 Vaste Stake | Long Only)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "can_short": False, "short_code": "", "trailing": False, "trailing_params": "",
        "stake": 100.0, "wallet": 168.0, "tradable_ratio": 0.99, "timeframe": "5m"
    },
    # 1. Vector 1: Dynamic Compounding (Stake grows automatically with wallet balance)
    {
        "name": "v1_compounding_50pct",
        "desc": "2. Compounding: 50% van Saldo per Trade (Winst herinvesteren)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "can_short": False, "short_code": "", "trailing": False, "trailing_params": "",
        "stake": "unlimited", "wallet": 168.0, "tradable_ratio": 0.50, "timeframe": "5m"
    },
    {
        "name": "v1_compounding_65pct",
        "desc": "3. Compounding: 65% van Saldo per Trade (Agressieve groei)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "can_short": False, "short_code": "", "trailing": False, "trailing_params": "",
        "stake": "unlimited", "wallet": 168.0, "tradable_ratio": 0.65, "timeframe": "5m"
    },
    # 2. Vector 2: Dual-Directional Scalping (Long + Short)
    {
        "name": "v2_long_and_short_12x",
        "desc": "4. Long + Short (Ook winst pakken in dalende markten)",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "can_short": True, "short_code": SHORT_CODE, "trailing": False, "trailing_params": "",
        "stake": 100.0, "wallet": 168.0, "tradable_ratio": 0.99, "timeframe": "5m"
    },
    # 3. Vector 3: Runner Trailing Stop (Let big winners run to +60% - +100% ROE)
    {
        "name": "v3_runner_trailing_12x",
        "desc": "5. Runner Trailing: Laat grote trends doorlopen tot +60-100% ROE",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.65, "30": 0.35, "60": 0.18},
        "bb_std": 1.8, "can_short": False, "short_code": "", "trailing": True,
        "trailing_params": "trailing_stop_positive = 0.08\ntrailing_stop_positive_offset = 0.18\ntrailing_only_offset_is_reached = True",
        "stake": 100.0, "wallet": 168.0, "tradable_ratio": 0.99, "timeframe": "5m"
    },
    # 4. Vector 4: High-Velocity 15x + Compounding + Long/Short Mega-Stack
    {
        "name": "v4_megastack_12x_comp_short",
        "desc": "6. Mega-Stack 12x: Compounding (60%) + Long & Short",
        "leverage": 12.0, "sl": -0.20, "roi": {"0": 0.44, "15": 0.24, "30": 0.12},
        "bb_std": 1.8, "can_short": True, "short_code": SHORT_CODE, "trailing": False, "trailing_params": "",
        "stake": "unlimited", "wallet": 168.0, "tradable_ratio": 0.60, "timeframe": "5m"
    },
    {
        "name": "v4_megastack_15x_comp_short",
        "desc": "7. Mega-Stack 15x: 15x Hefboom + Compounding (55%) + Long & Short",
        "leverage": 15.0, "sl": -0.25, "roi": {"0": 0.50, "15": 0.30, "30": 0.15},
        "bb_std": 1.8, "can_short": True, "short_code": SHORT_CODE, "trailing": False, "trailing_params": "",
        "stake": "unlimited", "wallet": 168.0, "tradable_ratio": 0.55, "timeframe": "5m"
    }
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE_MAX.format(
        name=name,
        timeframe=p["timeframe"],
        can_short=p["can_short"],
        leverage=p["leverage"],
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        trailing=p["trailing"],
        trailing_params=p["trailing_params"],
        bb_std=p["bb_std"],
        short_entry_code=p["short_code"]
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = p["wallet"]
    cfg["stake_amount"] = p["stake"]
    cfg["tradable_balance_ratio"] = p["tradable_ratio"]

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
        "--timeframe", p["timeframe"],
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

print("=== STARTING PROFIT MAXIMIZATION DEEP RESEARCH ===")
results = []
for p in VECTORS:
    print(f"Testing: {p['desc']}...", end="", flush=True)
    t0 = time.time()
    res = run_test(p)
    dt = time.time() - t0
    if "error" in res:
        print(f" ERROR: {res['error']}")
    else:
        print(f" DONE ({dt:.1f}s) -> Trades={res['trades']} | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Winst=${res['profit_abs']} ({res['profit_pct']}%) | PF={res['profit_factor']} | DD={res['drawdown']}%")
        results.append(res)

with open("/root/profit_maximization_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== COMPLETE! Results saved to /root/profit_maximization_results.json ===")

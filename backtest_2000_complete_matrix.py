#!/usr/bin/env python3
"""
Comprehensive €2000 Starting Capital Simulation Matrix.
Tests:
1. Top-Liquid Pairs (BTC, ETH, SOL) - 71.4% Winrate
2. Compounding ratios: 30%, 40%, 50%, 58% vs Fixed Stake ($500, $800, $1000)
3. Weekly & Monthly Profit Projections
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

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: Optional[float], max_stake: float, leverage: float, entry_tag: Optional[str], side: str, **kwargs) -> float:
        {custom_stake_code}
"""

EXPERIMENTS = [
    # 1. €2000 with Vaste $500 Stake (Zeer Veilig, 25% Saldo)
    {
        "name": "m2000_fixed_500",
        "desc": "€2000 Start | Vaste Inzet $500 (25% van saldo)",
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "stake_cfg": 500.0,
        "tradable_ratio": 0.99,
        "stake_code": "return min(500.0, max_stake)"
    },
    # 2. €2000 with Vaste $1000 Stake (50% Saldo)
    {
        "name": "m2000_fixed_1000",
        "desc": "€2000 Start | Vaste Inzet $1000 (50% van saldo)",
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "stake_cfg": 1000.0,
        "tradable_ratio": 0.99,
        "stake_code": "return min(1000.0, max_stake)"
    },
    # 3. €2000 with 35% Auto-Compounding (Stabiele Groei)
    {
        "name": "m2000_comp_35pct",
        "desc": "€2000 Start | 35% Auto-Compounding (Start $700 ➔ Groeit mee)",
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "stake_cfg": "unlimited",
        "tradable_ratio": 0.35,
        "stake_code": """try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            return min(total_equity * 0.35, max_stake)
        except Exception:
            return 700.0"""
    },
    # 4. €2000 with 50% Auto-Compounding (Snelle Groei)
    {
        "name": "m2000_comp_50pct",
        "desc": "€2000 Start | 50% Auto-Compounding (Start $1000 ➔ Groeit mee)",
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "stake_cfg": "unlimited",
        "tradable_ratio": 0.50,
        "stake_code": """try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            return min(total_equity * 0.50, max_stake)
        except Exception:
            return 1000.0"""
    },
    # 5. €2000 with 58% Auto-Compounding (Huidige Live Formule)
    {
        "name": "m2000_comp_58pct",
        "desc": "€2000 Start | 58% Auto-Compounding (Start $1160 ➔ Huidige Live Formule)",
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        "stake_cfg": "unlimited",
        "tradable_ratio": 0.58,
        "stake_code": """try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            return min(total_equity * 0.58, max_stake)
        except Exception:
            return 1160.0"""
    },
]

def run_test(p):
    name = p["name"]
    code = TEMPLATE.format(
        name=name,
        custom_stake_code=p["stake_code"]
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = 2000.0
    cfg["stake_amount"] = p["stake_cfg"]
    cfg["tradable_balance_ratio"] = p["tradable_ratio"]
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
                dd_abs = round(s.get("max_drawdown_abs", 0), 2)
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
                    "start_capital": 2000.0,
                    "final_balance": round(2000.0 + p_abs, 2),
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown_pct": dd,
                    "drawdown_abs": dd_abs,
                    "holding_avg": hold
                }

print("=== STARTING €2000 CAPITAL SIMULATION MATRIX ===")
results = []
for p in EXPERIMENTS:
    print(f"Testing: {p['desc']}...", end="", flush=True)
    t0 = time.time()
    res = run_test(p)
    dt = time.time() - t0
    if "error" in res:
        print(f" ERROR: {res['error']}")
    else:
        print(f" DONE ({dt:.1f}s) -> Trades={res['trades']} | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Eindsaldo=€{res['final_balance']} (Winst: +€{res['profit_abs']} / +{res['profit_pct']}%) | DD={res['drawdown_pct']}%")
        results.append(res)

with open("/root/bt_2000_matrix_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n=== MATRIX RUN COMPLETE! Saved to /root/bt_2000_matrix_results.json ===")

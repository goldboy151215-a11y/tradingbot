#!/usr/bin/env python3
"""
Backtest Engine: €2000 Starting Capital with Dynamic Compounding (12x BB Squeeze).
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

STRATEGY_NAME = "bt_2000_compounding_12x"

STRATEGY_CODE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from datetime import datetime
from typing import Optional
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
from pandas import DataFrame

class bt_2000_compounding_12x(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False

    stoploss = -0.20
    trailing_stop = False
    use_custom_stoploss = False

    minimal_roi = {
        "0": 0.44,
        "15": 0.24,
        "30": 0.12,
    }
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min(12.0, max_leverage) if max_leverage > 1.0 else 12.0

    @property
    def protections(self):
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.35,
            }
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
        try:
            total_equity = self.wallets.get_total(self.config["stake_currency"])
            stake_ratio = float(self.config.get("tradable_balance_ratio", 0.58))
            compounded = max(min_stake or 5.0, total_equity * stake_ratio)
            return min(compounded, max_stake)
        except Exception:
            return 1000.0
"""

def run_backtest_2000():
    with open(f"{STRATEGIES_DIR}/{STRATEGY_NAME}.py", "w") as f:
        f.write(STRATEGY_CODE)

    # 1. Test Compounding: Start $2000, 58% dynamic compounding
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = 1
    cfg["dry_run_wallet"] = 2000.0
    cfg["stake_amount"] = "unlimited"
    cfg["tradable_balance_ratio"] = 0.58
    cfg["amend_last_stake_amount"] = True

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)

    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", STRATEGY_NAME,
        "--timeframe", "5m",
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]

    print("Running Backtest for €2000 Startbedrag met Compounding...")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0

    if proc.returncode != 0:
        print(f"Error in backtesting: {proc.stderr}")
        return None

    zips = sorted(glob.glob(f"{RESULTS_DIR}/*.zip"), key=os.path.getmtime)
    with zipfile.ZipFile(zips[-1]) as z:
        for fname in z.namelist():
            if fname.endswith(".json") and not fname.endswith("_config.json"):
                data = json.loads(z.read(fname).decode())
                strat_data = data.get("strategy", {}).get(STRATEGY_NAME, {})
                trades_list = data.get("strategy_comparison", [])
                
                # Also extract trade-by-trade list from the results object
                raw_trades = strat_data.get("trades", [])
                
                return {
                    "raw_summary": strat_data,
                    "duration_sec": dt,
                    "data": data
                }

res = run_backtest_2000()
if res:
    s = res["raw_summary"]
    print("=== BACKTEST RESULTATEN €2000 STARTBEDRAG ===")
    print(f"Totale Trades: {s.get('total_trades')}")
    print(f"Winst / Verlies: {s.get('wins')} Wins / {s.get('losses')} Losses")
    print(f"Winrate: {round(s.get('winrate', 0) * 100, 1)}%")
    print(f"Startkapitaal: €2.000,00")
    print(f"Eindkapitaal: €{round(2000.0 + s.get('profit_total_abs', 0), 2)}")
    print(f"Totale Netto Winst: +€{round(s.get('profit_total_abs', 0), 2)} (+{round(s.get('profit_total', 0) * 100, 2)}%)")
    print(f"Profit Factor: {round(s.get('profit_factor', 0), 2)}")
    print(f"Max Account Drawdown: {round(s.get('max_drawdown_account', 0) * 100, 2)}% (€{round(s.get('max_drawdown_abs', 0), 2)})")
    print(f"Gemiddelde Duur per Trade: {s.get('holding_avg')}")
    
    with open("/root/bt_2000_summary.json", "w") as f:
        json.dump(s, f, indent=2)


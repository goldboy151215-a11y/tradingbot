#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"
CONFIG_PATH = "/root/ft_userdata/user_data/config-backtest.json"

PAIRLIST_SETS = {
    "ETH_SOL": ["ETH/USDT:USDT", "SOL/USDT:USDT"],
    "ETH_SOL_DOGE_XRP": ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"],
    "ALL_6": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "BNB/USDT:USDT"],
}

with open(CONFIG_PATH) as f:
    base_cfg = json.load(f)

for set_name, pairs in PAIRLIST_SETS.items():
    base_cfg["exchange"]["pair_whitelist"] = pairs
    with open(CONFIG_PATH, "w") as f:
        json.dump(base_cfg, f, indent=4)

    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", "exp_pullback4h_5x",
        "--timeframe", "5m",
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"{set_name} ERROR: {proc.stderr[-200:]}")
        continue

    zips = sorted(glob.glob(f"{RESULTS_DIR}/*.zip"), key=os.path.getmtime)
    with zipfile.ZipFile(zips[-1]) as z:
        for fname in z.namelist():
            if fname.endswith(".json") and not fname.endswith("_config.json"):
                data = json.loads(z.read(fname).decode())
                s = data.get("strategy", {}).get("exp_pullback4h_5x", {})
                t = s.get("total_trades", 0)
                w = s.get("wins", 0)
                l = s.get("losses", 0)
                wr = round(s.get("winrate", 0) * 100, 1)
                p_abs = round(s.get("profit_total_abs", 0), 2)
                p_pct = round(s.get("profit_total", 0) * 100, 2)
                pf = round(s.get("profit_factor", 0), 2) if s.get("profit_factor") else 0
                dd = round(s.get("max_drawdown_account", 0) * 100, 2)
                print(f"Set: {set_name} -> Trades={t}, W={w}, L={l}, Win%={wr}%, Profit=${p_abs} ({p_pct}%), PF={pf}, DD={dd}%")
                for pb in s.get('results_per_pair', []):
                    k = pb.get('key')
                    tr = pb.get('trades')
                    pwr = round(pb.get('winrate', 0)*100, 1)
                    pabs = round(pb.get('profit_total_abs', 0), 2)
                    ppf = round(pb.get('profit_factor', 0), 2) if pb.get('profit_factor') else 0
                    print(f"    {k}: tr={tr}, win%={pwr}%, profit=${pabs}, pf={ppf}")

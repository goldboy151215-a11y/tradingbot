#!/usr/bin/env python3
import subprocess
import json
import glob
import os
import zipfile

def run_test(name, strategy_name="weex_futures_quant", timerange="20260817-20260914"):
    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", strategy_name,
        "--timeframe", "5m",
        "--timerange", timerange,
        "--enable-protections",
        "--export", "trades",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"FAILED: {name}")
        print(proc.stderr[:1000])
        return None
    
    # Parse latest zip
    zips = sorted(glob.glob("/root/ft_userdata/user_data/backtest_results/*.zip"), key=os.path.getmtime)
    if not zips:
        return None
    latest_zip = zips[-1]
    with zipfile.ZipFile(latest_zip) as z:
        for f in z.namelist():
            if f.endswith(".json") and not f.endswith("_config.json"):
                data = json.loads(z.read(f).decode())
                strat_data = data.get("strategy", {}).get(strategy_name, {})
                return strat_data
    return None

if __name__ == "__main__":
    res = run_test("baseline")
    if res:
        print(f"Trades: {res.get('total_trades')}, Wins: {res.get('wins')}, Losses: {res.get('losses')}, Win%: {res.get('winrate', 0)*100:.1f}%, Profit: ${res.get('profit_total_abs', 0):.2f} ({res.get('profit_total_pct', 0):.2f}%), DD: {res.get('max_drawdown_account', 0)*100:.2f}%")

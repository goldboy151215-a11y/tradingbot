#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json
import time

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"

def run_backtest(strategy_name, timeframe="5m", timerange="20260817-20260914"):
    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", strategy_name,
        "--timeframe", timeframe,
        "--timerange", timerange,
        "--enable-protections",
        "--export", "trades",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    duration = time.time() - t0
    if proc.returncode != 0:
        return {"error": proc.stderr[:300]}

    zips = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.zip")), key=os.path.getmtime)
    if not zips:
        return {"error": "no zip"}
    
    with zipfile.ZipFile(zips[-1]) as z:
        for f in z.namelist():
            if f.endswith(".json") and not f.endswith("_config.json"):
                data = json.loads(z.read(f).decode())
                s = data.get("strategy", {}).get(strategy_name, {})
                return {
                    "strategy": strategy_name,
                    "timeframe": timeframe,
                    "trades": s.get("total_trades", 0),
                    "wins": s.get("wins", 0),
                    "losses": s.get("losses", 0),
                    "winrate": round(s.get("winrate", 0) * 100, 1),
                    "profit_abs": round(s.get("profit_total_abs", 0), 2),
                    "profit_pct": round(s.get("profit_total", 0) * 100, 2),
                    "profit_factor": round(s.get("profit_factor", 0), 2) if s.get("profit_factor") else 0,
                    "drawdown": round(s.get("max_drawdown_account", 0) * 100, 2),
                    "holding_avg": s.get("holding_avg", "N/A"),
                }
    return {"error": "no data in zip"}

if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "scalp_v1"
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"
    res = run_backtest(name, tf)
    print(json.dumps(res, indent=2))

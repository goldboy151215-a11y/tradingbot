#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json

with open("/root/ft_userdata/user_data/strategies/ema_trend_long.py") as f:
    content = f.read()

for lev in [1.0, 2.0, 3.0, 5.0, 10.0, 20.0]:
    strat_name = f"ema_trend_lev_{int(lev)}x"
    lev_method = f"""
    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min({lev}, max_leverage) if max_leverage > 1.0 else {lev}
"""
    new_content = content.replace("class ema_trend_long(IStrategy):", f"class {strat_name}(IStrategy):\n{lev_method}")
    with open(f"/root/ft_userdata/user_data/strategies/{strat_name}.py", "w") as f:
        f.write(new_content)
    
    cmd = [
        "docker", "run", "--rm",
        "--cpus=2.0", "--memory=3g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", strat_name,
        "--timeframe", "5m",
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"{strat_name} error: {proc.stderr[:200]}")
        continue
    zips = sorted(glob.glob("/root/ft_userdata/user_data/backtest_results/*.zip"), key=os.path.getmtime)
    with zipfile.ZipFile(zips[-1]) as z:
        for fname in z.namelist():
            if fname.endswith(".json") and not fname.endswith("_config.json"):
                data = json.loads(z.read(fname).decode())
                s = data.get("strategy", {}).get(strat_name, {})
                t = s.get("total_trades")
                w = s.get("wins")
                l = s.get("losses")
                wr = round(s.get("winrate", 0) * 100, 1)
                p_abs = round(s.get("profit_total_abs", 0), 2)
                p_pct = round(s.get("profit_total", 0) * 100, 2)
                pf = round(s.get("profit_factor", 0), 2) if s.get("profit_factor") else 0
                dd = round(s.get("max_drawdown_account", 0) * 100, 2)
                print(f"{strat_name}: Trades: {t}, Wins: {w}, Losses: {l}, Win%: {wr}%, Profit: ${p_abs} ({p_pct}%), PF: {pf}, DD: {dd}%")

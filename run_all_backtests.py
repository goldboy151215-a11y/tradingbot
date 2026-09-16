#!/usr/bin/env python3
"""
Orchestrates isolated backtesting for 3 Fib POI strategy variants across 2 timeranges.
"""

import json
import os
import subprocess
import sys
import zipfile

RUNS = [
    {"strategy": "fib_poi_base", "timerange": "20260715-20260913", "name": "base_short"},
    {"strategy": "fib_poi_base", "timerange": "20260101-20260913", "name": "base_long"},
    {"strategy": "fib_poi_long_only", "timerange": "20260715-20260913", "name": "long_only_short"},
    {"strategy": "fib_poi_long_only", "timerange": "20260101-20260913", "name": "long_only_long"},
    {"strategy": "fib_poi_no_trail", "timerange": "20260715-20260913", "name": "no_trail_short"},
    {"strategy": "fib_poi_no_trail", "timerange": "20260101-20260913", "name": "no_trail_long"},
]

RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"

def get_latest_result_zip():
    last_result_path = os.path.join(RESULTS_DIR, ".last_result.json")
    if not os.path.exists(last_result_path):
        return None
    with open(last_result_path, "r") as f:
        data = json.load(f)
        zip_name = data.get("latest_backtest")
        if zip_name:
            return os.path.join(RESULTS_DIR, zip_name)
    return None

def parse_result_zip(zip_path, expected_strategy):
    with zipfile.ZipFile(zip_path, "r") as z:
        for fname in z.namelist():
            if fname.endswith(".json") and not fname.endswith("_config.json"):
                with z.open(fname) as f:
                    data = json.load(f)
                    strat_data = data.get("strategy", {}).get(expected_strategy)
                    if strat_data:
                        return strat_data
    raise RuntimeError(f"Strategy {expected_strategy} not found in {zip_path}")

def run_backtest(strategy: str, timerange: str, export_filename: str):
    cmd = [
        "docker", "run", "--rm",
        "--cpus=1.5", "--memory=2g",
        "-v", "/root/ft_userdata/user_data:/freqtrade/user_data",
        "ft_userdata-freqtrade:latest",
        "backtesting",
        "--config", "/freqtrade/user_data/config-backtest.json",
        "--strategy", strategy,
        "--timeframe", "15m",
        "--timerange", timerange,
        "--enable-protections",
        "--export", "trades",
    ]
    print(f"\n==========================================")
    print(f"STARTING: {strategy} [{timerange}]")
    print(f"COMMAND: {' '.join(cmd)}")
    print(f"==========================================")
    
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"ERROR: Backtest failed for {strategy} [{timerange}] with exit code {proc.returncode}")
        print("--- STDERR ---")
        print(proc.stderr)
        print("--- STDOUT (tail 50 lines) ---")
        print("\n".join(proc.stdout.splitlines()[-50:]))
        sys.exit(1)
    
    zip_path = get_latest_result_zip()
    if not zip_path or not os.path.exists(zip_path):
        raise RuntimeError(f"Could not find latest backtest zip for {strategy} [{timerange}]")
    
    strat_metrics = parse_result_zip(zip_path, strategy)
    return strat_metrics

def main():
    results = []
    r_cash_unit = 168.40 * 0.0075  # 1R = $1.263

    for run in RUNS:
        strat = run["strategy"]
        tr = run["timerange"]
        name = run["name"]
        
        metrics = run_backtest(strat, tr, name)
        
        total_trades = metrics.get("total_trades", 0)
        wins = metrics.get("wins", 0)
        losses = metrics.get("losses", 0)
        draws = metrics.get("draws", 0)
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        profit_factor = metrics.get("profit_factor", 0.0) or 0.0
        tot_profit_abs = metrics.get("profit_total_abs", 0.0)
        tot_profit_pct = metrics.get("profit_total_pct", 0.0)
        max_dd_account = (metrics.get("max_drawdown_account", 0.0) or 0.0) * 100
        max_dd_abs = metrics.get("max_drawdown_abs", 0.0) or 0.0
        
        # Long vs Short & R-multiples
        long_trades = 0
        long_profit = 0.0
        long_wins = 0
        short_trades = 0
        short_profit = 0.0
        short_wins = 0
        
        win_rs = []
        loss_rs = []
        
        trades = metrics.get("trades", [])
        for t in trades:
            is_short = t.get("is_short", False)
            pnl = t.get("close_profit_abs", 0.0)
            r_val = pnl / r_cash_unit
            if pnl > 0:
                win_rs.append(r_val)
            elif pnl < 0:
                loss_rs.append(abs(r_val))

            if is_short:
                short_trades += 1
                short_profit += pnl
                if pnl > 0:
                    short_wins += 1
            else:
                long_trades += 1
                long_profit += pnl
                if pnl > 0:
                    long_wins += 1
                    
        avg_win_r = (sum(win_rs) / len(win_rs)) if win_rs else 0.0
        avg_loss_r = (sum(loss_rs) / len(loss_rs)) if loss_rs else 0.0

        # Exit reason summary
        exit_reasons = {}
        ers_raw = metrics.get("exit_reason_summary", [])
        if isinstance(ers_raw, list):
            for item in ers_raw:
                k = item.get("key", "unknown")
                if k != "TOTAL":
                    exit_reasons[k] = {
                        "trades": item.get("trades", 0),
                        "profit_abs": item.get("profit_total_abs", 0.0),
                        "winrate": (item.get("winrate", 0.0) or 0.0) * 100
                    }

        res = {
            "name": name,
            "strategy": strat,
            "timerange": tr,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "winrate": winrate,
            "profit_factor": profit_factor,
            "tot_profit_abs": tot_profit_abs,
            "tot_profit_pct": tot_profit_pct,
            "max_dd_account_pct": max_dd_account,
            "max_dd_abs": max_dd_abs,
            "long_trades": long_trades,
            "long_profit": long_profit,
            "long_winrate": (long_wins / long_trades * 100) if long_trades > 0 else 0.0,
            "short_trades": short_trades,
            "short_profit": short_profit,
            "short_winrate": (short_wins / short_trades * 100) if short_trades > 0 else 0.0,
            "avg_win_r": avg_win_r,
            "avg_loss_r": avg_loss_r,
            "exit_reasons": exit_reasons,
        }
        results.append(res)
        print(f"COMPLETED: {name} ({strat} [{tr}])")
        print(f"  Trades: {total_trades} (W:{wins}/L:{losses}), Win%: {winrate:.1f}%, PF: {profit_factor:.2f}")
        print(f"  PnL: {tot_profit_abs:+.3f} USDT ({tot_profit_pct:+.2f}%), Max DD: {max_dd_abs:.2f} USDT ({max_dd_account:.2f}%)")
        print(f"  Longs: {long_trades} ({long_profit:+.2f} USDT), Shorts: {short_trades} ({short_profit:+.2f} USDT)")
        print(f"  Avg Win R: {avg_win_r:.2f}R, Avg Loss R: {avg_loss_r:.2f}R")
        print(f"  Exit reasons: {exit_reasons}")

    out_file = os.path.join(RESULTS_DIR, "comparison_matrix.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nAll backtests finished successfully. Results saved to {out_file}")

if __name__ == "__main__":
    main()

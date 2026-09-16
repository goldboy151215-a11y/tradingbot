#!/usr/bin/env python3
"""
Orchestrates isolated backtesting for 3 new LONG+SHORT variants with 'goeie SL' across 2 timeranges:
A) both_fixed_sl
B) both_fixed_sl_htf
C) both_fixed_sl_rsi_tight
"""

import json
import os
import subprocess
import sys
import zipfile

RUNS = [
    {"strategy": "both_fixed_sl", "timerange": "20260715-20260913", "code": "A_P1"},
    {"strategy": "both_fixed_sl", "timerange": "20260101-20260913", "code": "A_P2"},
    {"strategy": "both_fixed_sl_htf", "timerange": "20260715-20260913", "code": "B_P1"},
    {"strategy": "both_fixed_sl_htf", "timerange": "20260101-20260913", "code": "B_P2"},
    {"strategy": "both_fixed_sl_rsi_tight", "timerange": "20260715-20260913", "code": "C_P1"},
    {"strategy": "both_fixed_sl_rsi_tight", "timerange": "20260101-20260913", "code": "C_P2"},
]

RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"
R_UNIT = 168.40 * 0.0075  # $1.263 = 1R

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

def run_backtest(strategy: str, timerange: str, code: str):
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
    print(f"RUNNING {code}: {strategy} [{timerange}]")
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
    return strat_metrics, zip_path

def main():
    results = []
    
    for run in RUNS:
        strat = run["strategy"]
        tr = run["timerange"]
        code = run["code"]
        
        metrics, zip_path = run_backtest(strat, tr, code)
        
        total_trades = metrics.get("total_trades", 0)
        wins = metrics.get("wins", 0)
        losses = metrics.get("losses", 0)
        draws = metrics.get("draws", 0)
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        profit_factor = metrics.get("profit_factor", 0.0) or 0.0
        tot_profit_abs = metrics.get("profit_total_abs", 0.0)
        tot_profit_pct = (tot_profit_abs / 168.40) * 100
        max_dd_account = (metrics.get("max_drawdown_account", 0.0) or 0.0) * 100
        max_dd_abs = metrics.get("max_drawdown_abs", 0.0) or 0.0
        
        trades = metrics.get("trades", [])
        long_trades = [t for t in trades if not t.get("is_short", False)]
        short_trades = [t for t in trades if t.get("is_short", False)]
        
        long_pnl = sum(t.get("profit_abs", 0.0) for t in long_trades)
        long_wins = sum(1 for t in long_trades if t.get("profit_abs", 0.0) > 0)
        long_losses = sum(1 for t in long_trades if t.get("profit_abs", 0.0) < 0)
        long_wr = (long_wins / len(long_trades) * 100) if long_trades else 0.0
        
        short_pnl = sum(t.get("profit_abs", 0.0) for t in short_trades)
        short_wins = sum(1 for t in short_trades if t.get("profit_abs", 0.0) > 0)
        short_losses = sum(1 for t in short_trades if t.get("profit_abs", 0.0) < 0)
        short_wr = (short_wins / len(short_trades) * 100) if short_trades else 0.0
        
        wins_r = [t.get("profit_abs", 0.0) / R_UNIT for t in trades if t.get("profit_abs", 0.0) > 0]
        losses_r = [abs(t.get("profit_abs", 0.0)) / R_UNIT for t in trades if t.get("profit_abs", 0.0) < 0]
        avg_win_r = (sum(wins_r) / len(wins_r)) if wins_r else 0.0
        avg_loss_r = (sum(losses_r) / len(losses_r)) if losses_r else 0.0
        
        # Categorize exits: trail na 1R / sl / roi / overig
        trail_exits = 0
        sl_exits = 0
        roi_exits = 0
        other_exits = 0
        
        exit_summary_raw = metrics.get("exit_reason_summary", [])
        exit_breakdown = {}
        for item in exit_summary_raw:
            k = item.get("key")
            cnt = item.get("trades", 0)
            pnl_k = item.get("profit_total_abs", 0.0)
            if k == "TOTAL":
                continue
            exit_breakdown[k] = {"count": cnt, "pnl": pnl_k}
            if k == "trailing_stop_loss":
                trail_exits += cnt
            elif k in ("stop_loss", "stoploss"):
                sl_exits += cnt
            elif k == "roi":
                roi_exits += cnt
            else:
                other_exits += cnt
                
        res = {
            "code": code,
            "strategy": strat,
            "timerange": tr,
            "zip_file": os.path.basename(zip_path),
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "winrate": round(winrate, 1),
            "profit_factor": round(profit_factor, 2),
            "tot_profit_abs": round(tot_profit_abs, 3),
            "tot_profit_pct": round(tot_profit_pct, 2),
            "max_dd_abs": round(max_dd_abs, 3),
            "max_dd_pct": round(max_dd_account, 2),
            "long_trades": len(long_trades),
            "long_wins": long_wins,
            "long_losses": long_losses,
            "long_wr": round(long_wr, 1),
            "long_pnl": round(long_pnl, 3),
            "short_trades": len(short_trades),
            "short_wins": short_wins,
            "short_losses": short_losses,
            "short_wr": round(short_wr, 1),
            "short_pnl": round(short_pnl, 3),
            "avg_win_r": round(avg_win_r, 2),
            "avg_loss_r": round(avg_loss_r, 2),
            "exit_counts": {
                "trail_1r": trail_exits,
                "sl": sl_exits,
                "roi": roi_exits,
                "overig": other_exits,
            },
            "exit_breakdown": exit_breakdown,
        }
        results.append(res)
        print(f"DONE {code}: Trades={total_trades}, WR={winrate:.1f}%, PF={profit_factor:.2f}, PnL={tot_profit_abs:+.3f} USDT, MaxDD={max_dd_account:.2f}%")
        print(f"  Longs: {len(long_trades)} ({long_pnl:+.3f} USDT, WR: {long_wr:.1f}%) | Shorts: {len(short_trades)} ({short_pnl:+.3f} USDT, WR: {short_wr:.1f}%)")
        print(f"  Exits: Trail_1R={trail_exits}, SL={sl_exits}, ROI={roi_exits}, Overig={other_exits}")
        print(f"  Avg Win R: {avg_win_r:.2f}R | Avg Loss R: {avg_loss_r:.2f}R")

    out_file = os.path.join(RESULTS_DIR, "both_variants_comparison.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nAll 6 runs finished successfully. Output in {out_file}")

if __name__ == "__main__":
    main()

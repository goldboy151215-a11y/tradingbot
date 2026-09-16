#!/usr/bin/env python3
"""
Orchestrates isolated backtesting for Long-Only Fib POI variants:
- long_only_ref (Baseline)
- long_htf (Variant 1)
- long_hold_tp (Variant 2)
- long_htf_hold (Variant 3, conditionally run only if 1 & 2 both improve PF on P1)
"""

import json
import os
import subprocess
import sys
import zipfile

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

def evaluate_run(metrics, zip_path, strategy, timerange, code):
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
    wins_r = [t.get("profit_abs", 0.0) / R_UNIT for t in trades if t.get("profit_abs", 0.0) > 0]
    losses_r = [abs(t.get("profit_abs", 0.0)) / R_UNIT for t in trades if t.get("profit_abs", 0.0) < 0]
    avg_win_r = (sum(wins_r) / len(wins_r)) if wins_r else 0.0
    avg_loss_r = (sum(losses_r) / len(losses_r)) if losses_r else 0.0
    
    # Exit counts: trail (na 1R) / sl / roi / invalidation / overig
    trail_cnt = 0
    sl_cnt = 0
    roi_cnt = 0
    invalidation_cnt = 0
    other_cnt = 0
    
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
            trail_cnt += cnt
        elif k in ("stop_loss", "stoploss"):
            sl_cnt += cnt
        elif k == "roi":
            roi_cnt += cnt
        elif "invalidated" in k:
            invalidation_cnt += cnt
        else:
            other_cnt += cnt

    res = {
        "code": code,
        "strategy": strategy,
        "timerange": timerange,
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
        "avg_win_r": round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "exit_counts": {
            "trail": trail_cnt,
            "sl": sl_cnt,
            "roi": roi_cnt,
            "invalidation": invalidation_cnt,
            "overig": other_cnt
        },
        "exit_breakdown": exit_breakdown
    }
    print(f"DONE {code}: Trades={total_trades}, WR={winrate:.1f}%, PF={profit_factor:.2f}, PnL={tot_profit_abs:+.3f} USDT, MaxDD={max_dd_account:.2f}%")
    print(f"  Exits: Trail={trail_cnt}, SL={sl_cnt}, ROI={roi_cnt}, Invalidation={invalidation_cnt}, Overig={other_cnt}")
    print(f"  Avg Win R: {avg_win_r:.2f}R | Avg Loss R: {avg_loss_r:.2f}R")
    return res

def main():
    results = []
    
    # 1. Baseline: long_only_ref
    m_ref_p1, z_ref_p1 = run_backtest("long_only_ref", "20260715-20260913", "ref_P1")
    res_ref_p1 = evaluate_run(m_ref_p1, z_ref_p1, "long_only_ref", "20260715-20260913", "ref_P1")
    results.append(res_ref_p1)
    
    m_ref_p2, z_ref_p2 = run_backtest("long_only_ref", "20260101-20260913", "ref_P2")
    res_ref_p2 = evaluate_run(m_ref_p2, z_ref_p2, "long_only_ref", "20260101-20260913", "ref_P2")
    results.append(res_ref_p2)
    
    ref_pf_p1 = res_ref_p1["profit_factor"]
    print(f"\nBASELINE ref_P1 Profit Factor: {ref_pf_p1:.2f}")

    # 2. Variant 1: long_htf
    m_htf_p1, z_htf_p1 = run_backtest("long_htf", "20260715-20260913", "htf_P1")
    res_htf_p1 = evaluate_run(m_htf_p1, z_htf_p1, "long_htf", "20260715-20260913", "htf_P1")
    results.append(res_htf_p1)
    
    m_htf_p2, z_htf_p2 = run_backtest("long_htf", "20260101-20260913", "htf_P2")
    res_htf_p2 = evaluate_run(m_htf_p2, z_htf_p2, "long_htf", "20260101-20260913", "htf_P2")
    results.append(res_htf_p2)
    
    htf_pf_p1 = res_htf_p1["profit_factor"]
    htf_improves = htf_pf_p1 > ref_pf_p1
    print(f"Variant 1 (long_htf) P1 PF: {htf_pf_p1:.2f} (Improves over {ref_pf_p1:.2f}: {htf_improves})")

    # 3. Variant 2: long_hold_tp
    m_hold_p1, z_hold_p1 = run_backtest("long_hold_tp", "20260715-20260913", "hold_P1")
    res_hold_p1 = evaluate_run(m_hold_p1, z_hold_p1, "long_hold_tp", "20260715-20260913", "hold_P1")
    results.append(res_hold_p1)
    
    m_hold_p2, z_hold_p2 = run_backtest("long_hold_tp", "20260101-20260913", "hold_P2")
    res_hold_p2 = evaluate_run(m_hold_p2, z_hold_p2, "long_hold_tp", "20260101-20260913", "hold_P2")
    results.append(res_hold_p2)
    
    hold_pf_p1 = res_hold_p1["profit_factor"]
    hold_improves = hold_pf_p1 > ref_pf_p1
    print(f"Variant 2 (long_hold_tp) P1 PF: {hold_pf_p1:.2f} (Improves over {ref_pf_p1:.2f}: {hold_improves})")

    # 4. Variant 3: long_htf_hold (conditional: ONLY if 1 AND 2 both improve PF on P1)
    if htf_improves and hold_improves:
        print("\nBOTH 1 (htf) and 2 (hold) improved PF on P1! Running Variant 3: long_htf_hold...")
        m_htf_hold_p1, z_htf_hold_p1 = run_backtest("long_htf_hold", "20260715-20260913", "htf_hold_P1")
        res_htf_hold_p1 = evaluate_run(m_htf_hold_p1, z_htf_hold_p1, "long_htf_hold", "20260715-20260913", "htf_hold_P1")
        results.append(res_htf_hold_p1)
        
        m_htf_hold_p2, z_htf_hold_p2 = run_backtest("long_htf_hold", "20260101-20260913", "htf_hold_P2")
        res_htf_hold_p2 = evaluate_run(m_htf_hold_p2, z_htf_hold_p2, "long_htf_hold", "20260101-20260913", "htf_hold_P2")
        results.append(res_htf_hold_p2)
    else:
        print(f"\nCondition for Variant 3 NOT met (htf_improves={htf_improves}, hold_improves={hold_improves}). Skipping long_htf_hold as requested.")

    out_file = os.path.join(RESULTS_DIR, "long_only_comparison.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nBacktesting completed. Summary saved to {out_file}")

if __name__ == "__main__":
    main()

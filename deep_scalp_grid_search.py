#!/usr/bin/env python3
"""
Deep Scalp Grid Search Engine.
Explores the full parameter space of the BB Squeeze Breakout Scalper:
- Stoploss: Tighter (-6%, -8%) to Looser (-10%, -12%, -15%, -18%)
- Take-Profit (ROI ladders): Micro, Medium, Extended, and Trailing Stops
- Leverage: 1x (no leverage), 3x, 4x, 5x, 6x, 7x, 8x, 10x, 12x
- Entry filters: Volume multipliers (1.0x, 1.1x, 1.2x, 1.4x), RSI bounds, Bollinger STD
- Higher timeframe filters: 4H EMA20 vs 1H EMA20 vs None
- Slot concurrency: 1 slot vs 2 slots
"""

import subprocess
import glob
import os
import zipfile
import json
import time
from itertools import product

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"
CONFIG_PATH = "/root/ft_userdata/user_data/config-backtest.json"
OUTPUT_REPORT = "/root/grid_search_summary.json"

TEMPLATE_STRATEGY = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
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

    stoploss = {sl}
    trailing_stop = {trailing_stop}
    trailing_stop_positive = {trail_pos}
    trailing_stop_positive_offset = {trail_offset}
    trailing_only_offset_is_reached = True
    use_custom_stoploss = False

    minimal_roi = {roi}
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min({leverage}, max_leverage) if max_leverage > 1.0 else {leverage}

    @property
    def protections(self):
        return [
            {{
                "method": "CooldownPeriod",
                "stop_duration_candles": {cooldown},
            }},
            {{
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.18,
            }},
        ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
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

        # HTF filter
        {htf_filter_code}

        breakout = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * {vol_mult}
        rsi_ok = (dataframe["rsi"] > {rsi_min}) & (dataframe["rsi"] < {rsi_max})

        long_cond = breakout & vol_surge & rsi_ok & htf_ok

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_breakout_scalp"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < {max_open_trades}
"""

def test_config(p):
    name = p["name"]

    if p.get("htf_mode") == "4h":
        htf_code = "htf_ok = dataframe['close_4h'] > dataframe['ema20_4h']"
    elif p.get("htf_mode") == "1h":
        htf_code = "htf_ok = dataframe['close_1h'] > dataframe['ema20_1h']"
    else:
        htf_code = "htf_ok = (dataframe['close'] > dataframe['ema50'])"

    code = TEMPLATE_STRATEGY.format(
        name=name,
        leverage=p["leverage"],
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        trailing_stop=str(p.get("trailing_stop", False)),
        trail_pos=p.get("trail_pos", 0.015),
        trail_offset=p.get("trail_offset", 0.030),
        cooldown=p.get("cooldown", 4),
        vol_mult=p.get("vol_mult", 1.2),
        rsi_min=p.get("rsi_min", 52),
        rsi_max=p.get("rsi_max", 70),
        bb_std=p.get("bb_std", 2.0),
        htf_filter_code=htf_code,
        max_open_trades=p.get("max_open_trades", 1)
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

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
    if not zips:
        return {"name": name, "error": "No result zip"}

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
                sharpe = round(s.get("sharpe", 0), 2) if s.get("sharpe") else 0
                sortino = round(s.get("sortino", 0), 2) if s.get("sortino") else 0
                return {
                    "name": name,
                    "params": p,
                    "trades": t,
                    "wins": w,
                    "losses": l,
                    "winrate": wr,
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown": dd,
                    "sharpe": sharpe,
                    "sortino": sortino,
                    "holding_avg": hold
                }
    return {"name": name, "error": "No strat in zip"}

def generate_grid_configurations():
    grid = []
    
    # Dimensions to sweep:
    # 1. Leverage: 1x, 3x, 5x, 6x, 7x, 8x, 10x
    # 2. Stoploss: Tighter (-0.07, -0.09) vs Balanced (-0.10, -0.12) vs Wider (-0.15, -0.18)
    # 3. ROI Ladder:
    #    - Fast Micro: {0: 0.16, 10: 0.08, 20: 0.04}
    #    - Balanced Scalp: {0: 0.22, 15: 0.12, 30: 0.06}
    #    - Extended Scalp: {0: 0.28, 20: 0.16, 40: 0.08}
    # 4. Trailing stop enabled vs disabled
    # 5. Volume surge: 1.0x, 1.2x, 1.4x
    # 6. RSI bounds: (50, 70), (52, 70), (55, 75)
    # 7. HTF filter: 4H vs 1H vs 5m only
    # 8. Max open trades: 1 vs 2

    # A. Baseline No Leverage (1x)
    grid.append({
        "name": "g_1x_nolev_base",
        "leverage": 1.0, "sl": -0.035,
        "roi": {"0": 0.05, "15": 0.03, "30": 0.015},
        "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
    })
    grid.append({
        "name": "g_1x_nolev_wide",
        "leverage": 1.0, "sl": -0.050,
        "roi": {"0": 0.08, "30": 0.04, "60": 0.020},
        "vol_mult": 1.1, "rsi_min": 50, "rsi_max": 72, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
    })

    # B. Systematic Leverage & Stoploss Combinations (3x, 5x, 6x, 7x, 8x, 10x)
    leverage_sl_roi_combos = [
        # 3x
        (3.0, -0.06, {"0": 0.12, "15": 0.07, "30": 0.035}, "g_3x_tight"),
        (3.0, -0.09, {"0": 0.15, "15": 0.09, "30": 0.045}, "g_3x_balanced"),
        # 5x
        (5.0, -0.08, {"0": 0.18, "15": 0.10, "30": 0.05}, "g_5x_tight"),
        (5.0, -0.10, {"0": 0.20, "15": 0.12, "30": 0.06}, "g_5x_balanced"),
        (5.0, -0.14, {"0": 0.25, "20": 0.15, "40": 0.08}, "g_5x_wide"),
        # 6x (Our current sweet spot)
        (6.0, -0.08, {"0": 0.18, "10": 0.10, "20": 0.05}, "g_6x_fast_tp"),
        (6.0, -0.10, {"0": 0.22, "15": 0.12, "30": 0.06}, "g_6x_balanced"),
        (6.0, -0.12, {"0": 0.25, "15": 0.14, "30": 0.07}, "g_6x_wide_sl"),
        (6.0, -0.15, {"0": 0.30, "20": 0.18, "40": 0.09}, "g_6x_deep_runner"),
        # 7x
        (7.0, -0.09, {"0": 0.20, "15": 0.12, "30": 0.06}, "g_7x_tight"),
        (7.0, -0.11, {"0": 0.24, "15": 0.14, "30": 0.07}, "g_7x_balanced"),
        (7.0, -0.14, {"0": 0.28, "20": 0.16, "40": 0.08}, "g_7x_wide"),
        # 8x
        (8.0, -0.10, {"0": 0.22, "15": 0.13, "30": 0.065}, "g_8x_tight"),
        (8.0, -0.12, {"0": 0.28, "15": 0.16, "30": 0.08}, "g_8x_balanced"),
        (8.0, -0.16, {"0": 0.35, "20": 0.20, "40": 0.10}, "g_8x_wide"),
        # 10x
        (10.0, -0.12, {"0": 0.28, "15": 0.16, "30": 0.08}, "g_10x_tight"),
        (10.0, -0.15, {"0": 0.35, "15": 0.20, "30": 0.10}, "g_10x_balanced"),
        (10.0, -0.18, {"0": 0.42, "20": 0.25, "40": 0.12}, "g_10x_wide"),
    ]

    for lev, sl, roi, name in leverage_sl_roi_combos:
        grid.append({
            "name": name,
            "leverage": lev, "sl": sl, "roi": roi,
            "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
        })

    # C. Trailing Stop Variations across 5x, 6x, 8x
    for lev, sl, trail_offset, trail_pos in [
        (5.0, -0.10, 0.040, 0.015),
        (6.0, -0.10, 0.045, 0.018),
        (6.0, -0.12, 0.060, 0.022),
        (8.0, -0.12, 0.060, 0.024),
    ]:
        grid.append({
            "name": f"g_trail_{int(lev)}x_off{int(trail_offset*1000)}",
            "leverage": lev, "sl": sl,
            "roi": {"0": 0.30, "20": 0.18, "45": 0.09},
            "trailing_stop": True, "trail_pos": trail_pos, "trail_offset": trail_offset,
            "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "cooldown": 4
        })

    # D. Volume Multiplier Variations (Sensitivity to Volume)
    for vm in [1.0, 1.1, 1.3, 1.5]:
        grid.append({
            "name": f"g_6x_vol_{int(vm*10)}",
            "leverage": 6.0, "sl": -0.10,
            "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
            "vol_mult": vm, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
        })

    # E. RSI Filter Sensitivity
    for rmin, rmax, tag in [(48, 68, "rsi_loose"), (50, 70, "rsi_med"), (55, 75, "rsi_aggressive")]:
        grid.append({
            "name": f"g_6x_{tag}",
            "leverage": 6.0, "sl": -0.10,
            "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
            "vol_mult": 1.2, "rsi_min": rmin, "rsi_max": rmax, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
        })

    # F. Bollinger Band Deviation Sensitivity (1.8 STD vs 2.2 STD)
    for bstd, tag in [(1.8, "bb18"), (2.2, "bb22")]:
        grid.append({
            "name": f"g_6x_{tag}",
            "leverage": 6.0, "sl": -0.10,
            "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
            "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": bstd, "htf_mode": "4h", "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
        })

    # G. Higher Timeframe Filter (4H vs 1H vs None)
    for htf, tag in [("1h", "htf_1h"), ("none", "htf_none")]:
        grid.append({
            "name": f"g_6x_{tag}",
            "leverage": 6.0, "sl": -0.10,
            "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
            "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": htf, "max_open_trades": 1, "trailing_stop": False, "cooldown": 4
        })

    # H. 2 Concurrent Slots at 5x and 6x
    for lev, name in [(5.0, "g_2slots_5x"), (6.0, "g_2slots_6x")]:
        grid.append({
            "name": name,
            "leverage": lev, "sl": -0.10,
            "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
            "vol_mult": 1.2, "rsi_min": 52, "rsi_max": 70, "bb_std": 2.0, "htf_mode": "4h", "max_open_trades": 2, "trailing_stop": False, "cooldown": 4
        })

    return grid

if __name__ == "__main__":
    configs = generate_grid_configurations()
    total = len(configs)
    print(f"=== LAUNCHING DEEP SCALP GRID SEARCH: {total} CONFIGURATIONS ===")
    results = []

    for i, c in enumerate(configs, 1):
        t0 = time.time()
        print(f"[{i}/{total}] Testing '{c['name']}' (Lev={c['leverage']}x, SL={c['sl']})...", end=" ", flush=True)
        res = test_config(c)
        results.append(res)
        elapsed = round(time.time() - t0, 1)

        if "error" in res:
            print(f"ERROR ({elapsed}s): {res['error']}")
        else:
            print(f"DONE ({elapsed}s) -> Trades={res['trades']} | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Profit=${res['profit_abs']} ({res['profit_pct']}%) | PF={res['profit_factor']} | DD={res['drawdown']}%")

    # Save raw results
    with open(OUTPUT_REPORT, "w") as f:
        json.dump(results, f, indent=2)

    # Sort and rank top performers
    valid_results = [r for r in results if "profit_abs" in r]
    sorted_by_profit = sorted(valid_results, key=lambda x: x["profit_abs"], reverse=True)
    sorted_by_pf = sorted(valid_results, key=lambda x: x["profit_factor"], reverse=True)
    sorted_by_sharpe = sorted(valid_results, key=lambda x: x["sharpe"], reverse=True)

    print("\n" + "="*80)
    print("🏆 TOP 5 CONFIGURATIONS BY NET PROFIT:")
    for r in sorted_by_profit[:5]:
        p = r["params"]
        print(f"  • {r['name']} ({p['leverage']}x, SL={p['sl']}, TP={p['roi']}): Net Profit=${r['profit_abs']} ({r['profit_pct']}%), Win%={r['winrate']}%, PF={r['profit_factor']}, DD={r['drawdown']}%, Trades={r['trades']}")

    print("\n🛡️ TOP 5 CONFIGURATIONS BY PROFIT FACTOR:")
    for r in sorted_by_pf[:5]:
        p = r["params"]
        print(f"  • {r['name']} ({p['leverage']}x, SL={p['sl']}): PF={r['profit_factor']}, Net Profit=${r['profit_abs']}, Win%={r['winrate']}%, DD={r['drawdown']}%, Trades={r['trades']}")

    print("\n📈 TOP 5 CONFIGURATIONS BY SHARPE RATIO:")
    for r in sorted_by_sharpe[:5]:
        p = r["params"]
        print(f"  • {r['name']} ({p['leverage']}x, SL={p['sl']}): Sharpe={r['sharpe']}, Net Profit=${r['profit_abs']}, Win%={r['winrate']}%, DD={r['drawdown']}%, Trades={r['trades']}")
    print("="*80)

#!/usr/bin/env python3
"""
Trade Limit & Concurrency Experiment Engine.
Specifically tests:
1. Concurrency: max_open_trades = 1 vs 2 vs 3 vs 4 vs 5 (trading multiple pairs simultaneously)
2. Cooldown: cooldown = 0 (no cooldown, immediate re-entry) vs 1 vs 2 vs 4 vs 8
3. Protections: With MaxDrawdown/StopLoss protections vs Raw Unrestricted
4. Across top leverage settings (6x, 8x, 10x) and base (1x)
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

TEMPLATE_CODE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
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
    trailing_stop = False
    use_custom_stoploss = False

    minimal_roi = {roi}
    startup_candle_count = 150

    def leverage(self, pair: str, current_time, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag, side: str, **kwargs) -> float:
        return min({leverage}, max_leverage) if max_leverage > 1.0 else {leverage}

    @property
    def protections(self):
        {protections_code}

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

        boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
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

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        {confirm_entry_code}
"""

def test_limit_variant(p):
    name = p["name"]

    if p.get("enable_protections", True) and p.get("cooldown", 0) > 0:
        prot_code = f"""return [
            {{
                "method": "CooldownPeriod",
                "stop_duration_candles": {p['cooldown']},
            }},
            {{
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 6,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.20,
            }},
        ]"""
    else:
        prot_code = "return []"

    if p.get("slot_limit") is not None:
        confirm_code = f"""open_trades = Trade.get_open_trades()
        return len(open_trades) < {p['slot_limit']}"""
    else:
        confirm_code = "return True"

    code = TEMPLATE_CODE.format(
        name=name,
        leverage=p["leverage"],
        sl=p["sl"],
        roi=json.dumps(p["roi"]),
        protections_code=prot_code,
        confirm_entry_code=confirm_code
    )

    with open(f"{STRATEGIES_DIR}/{name}.py", "w") as f:
        f.write(code)

    # Set max_open_trades in config
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["max_open_trades"] = p.get("max_open_trades_cfg", 3)
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
                hold = s.get("holding_avg", "N/A")
                trades_per_day = round(s.get("trades_per_day", 0), 2)
                return {
                    "name": name,
                    "desc": p["desc"],
                    "slots": p.get("slot_limit", "unlimited"),
                    "cooldown": p.get("cooldown", 0),
                    "trades": t,
                    "trades_per_day": trades_per_day,
                    "wins": w,
                    "losses": l,
                    "winrate": wr,
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown": dd,
                    "holding_avg": hold
                }

EXPERIMENTS = [
    # Baseline winner with 1 slot limit + 4 candle cooldown
    {
        "name": "lim_1slot_cd4_6x",
        "desc": "1 Slot Limit + Cooldown 4 (Beveiligd)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 1, "max_open_trades_cfg": 1, "cooldown": 4, "enable_protections": True
    },
    # 1 slot limit but NO cooldown (cooldown = 0)
    {
        "name": "lim_1slot_nocd_6x",
        "desc": "1 Slot Limit + GEEN Cooldown (Directe herinstap)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 1, "max_open_trades_cfg": 1, "cooldown": 0, "enable_protections": False
    },
    # 2 concurrent slots + cooldown 4
    {
        "name": "lim_2slots_cd4_6x",
        "desc": "2 Gelijktijdige Trades + Cooldown 4",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 2, "max_open_trades_cfg": 2, "cooldown": 4, "enable_protections": True
    },
    # 2 concurrent slots + NO cooldown (cooldown = 0)
    {
        "name": "lim_2slots_nocd_6x",
        "desc": "2 Gelijktijdige Trades + GEEN Cooldown",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 2, "max_open_trades_cfg": 2, "cooldown": 0, "enable_protections": False
    },
    # 3 concurrent slots (All whitelisted pairs simultaneously) + Cooldown 4
    {
        "name": "lim_3slots_cd4_6x",
        "desc": "3 Gelijktijdige Trades (Alle paren vol) + Cooldown 4",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 3, "max_open_trades_cfg": 3, "cooldown": 4, "enable_protections": True
    },
    # 3 concurrent slots + NO cooldown (TOTAAL ONBEPERKT)
    {
        "name": "lim_3slots_unlimited_6x",
        "desc": "3 Gelijktijdige Trades + TOTAAL ONBEPERKT (No CD, No MaxDD)",
        "leverage": 6.0, "sl": -0.10, "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "slot_limit": 3, "max_open_trades_cfg": 3, "cooldown": 0, "enable_protections": False
    },
    # 8x leverage with 2 slots + cooldown 2
    {
        "name": "lim_2slots_cd2_8x",
        "desc": "8x Hefboom + 2 Slots + Korte Cooldown 2",
        "leverage": 8.0, "sl": -0.12, "roi": {"0": 0.28, "15": 0.16, "30": 0.08},
        "slot_limit": 2, "max_open_trades_cfg": 2, "cooldown": 2, "enable_protections": True
    },
    # 8x leverage 3 slots UNRESTRICTED
    {
        "name": "lim_3slots_unlimited_8x",
        "desc": "8x Hefboom + 3 Slots TOTAAL ONBEPERKT",
        "leverage": 8.0, "sl": -0.12, "roi": {"0": 0.28, "15": 0.16, "30": 0.08},
        "slot_limit": 3, "max_open_trades_cfg": 3, "cooldown": 0, "enable_protections": False
    }
]

if __name__ == "__main__":
    print(f"=== TESTING TRADE LIMITS & CONCURRENCY ({len(EXPERIMENTS)} CONFIGURATIONS) ===")
    results = []
    for exp in EXPERIMENTS:
        t0 = time.time()
        print(f"Testing: {exp['desc']}...", end=" ", flush=True)
        res = test_limit_variant(exp)
        results.append(res)
        elapsed = round(time.time() - t0, 1)
        if "error" in res:
            print(f"FAILED ({elapsed}s): {res['error']}")
        else:
            print(f"DONE ({elapsed}s) -> Trades={res['trades']} ({res['trades_per_day']}/dag) | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Winst=${res['profit_abs']} ({res['profit_pct']}%) | PF={res['profit_factor']} | DD={res['drawdown']}%")

    with open("/root/trade_limits_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\n=== TRADE LIMITS EXPERIMENT COMPLETE ===")

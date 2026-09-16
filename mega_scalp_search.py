#!/usr/bin/env python3
"""
Comprehensive Multi-Archetype Scalping Matrix Engine.
Tests multiple distinct scalping concepts:
1. StochRSI + EMA Ribbon Pullbacks
2. Bollinger Band Squeeze Breakouts
3. MACD Momentum Histogram Scalps
4. RSI Extreme Mean Reversion
5. EMA 9/21 Micro Pullback Scalps (different SL / TP / Leverage sweeps)
"""

import subprocess
import glob
import os
import zipfile
import json
import textwrap

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
    timeframe = "{timeframe}"
    can_short = {can_short}

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
                "max_allowed_drawdown": 0.15,
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
{indicators}
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        dataframe.loc[:, "enter_tag"] = ""

        # HTF Trend
        htf_4h_bull = dataframe["close_4h"] > dataframe["ema20_4h"]
        htf_4h_bear = dataframe["close_4h"] < dataframe["ema20_4h"]

{entry_code}

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < {max_open_trades}
"""

def indent_code(code_str, spaces=8):
    lines = code_str.strip().split("\n")
    return "\n".join((" " * spaces) + line.strip() for line in lines if line.strip())

def run_single_test(strat_def):
    name = strat_def["name"]
    ind_code = indent_code(strat_def["indicators"], 8)
    ent_code = indent_code(strat_def["entry_code"], 8)

    code = TEMPLATE_CODE.format(
        name=name,
        timeframe=strat_def.get("timeframe", "5m"),
        can_short=str(strat_def.get("can_short", False)),
        sl=strat_def.get("sl", -0.018),
        trailing_stop=str(strat_def.get("trailing_stop", False)),
        trail_pos=strat_def.get("trail_pos", 0.008),
        trail_offset=strat_def.get("trail_offset", 0.015),
        roi=json.dumps(strat_def.get("roi")),
        leverage=strat_def.get("leverage", 5.0),
        cooldown=strat_def.get("cooldown", 4),
        max_open_trades=strat_def.get("max_open_trades", 1),
        indicators=ind_code,
        entry_code=ent_code
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
        "--timeframe", strat_def.get("timeframe", "5m"),
        "--timerange", "20260817-20260914",
        "--enable-protections",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {"name": name, "error": proc.stderr[-300:]}

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
                return {
                    "name": name,
                    "archetype": strat_def["archetype"],
                    "leverage": strat_def["leverage"],
                    "sl": strat_def["sl"],
                    "trades": t,
                    "wins": w,
                    "losses": l,
                    "winrate": wr,
                    "profit_abs": p_abs,
                    "profit_pct": p_pct,
                    "profit_factor": pf,
                    "drawdown": dd,
                    "holding_avg": hold
                }
    return {"name": name, "error": "No strat in zip"}

def build_all_configurations():
    configs = []

    # 1. ARCHETYPE: Fast EMA 9/21 Pullback Dip Scalper (Multiple Leverage & SL configurations)
    ema_pullback_ind = """
dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
    """
    ema_pullback_ent = """
dip = (dataframe["low"] <= dataframe["ema20"]) & (dataframe["close"] > dataframe["ema20"]) & (dataframe["close"] > dataframe["open"])
ema_ok = dataframe["ema20"] > dataframe["ema50"]
rsi_ok = (dataframe["rsi"] >= 40) & (dataframe["rsi"] <= 60)
long_cond = dip & ema_ok & htf_4h_bull & rsi_ok & (dataframe["volume"] > 0)
dataframe.loc[long_cond, "enter_long"] = 1
dataframe.loc[long_cond, "enter_tag"] = "ema_pullback"
    """

    for lev, sl, roi in [
        (3.0, -0.06, {"0": 0.12, "20": 0.08, "45": 0.04}),
        (5.0, -0.10, {"0": 0.20, "20": 0.12, "45": 0.07}),
        (6.0, -0.11, {"0": 0.24, "20": 0.14, "45": 0.08}),
        (8.0, -0.13, {"0": 0.28, "20": 0.16, "45": 0.09}),
        (10.0, -0.15, {"0": 0.35, "20": 0.20, "45": 0.12}),
    ]:
        configs.append({
            "name": f"ema_pullback_{int(lev)}x",
            "archetype": "EMA 9/21 Dip-Buyer",
            "indicators": ema_pullback_ind,
            "entry_code": ema_pullback_ent,
            "can_short": False,
            "leverage": lev,
            "sl": sl,
            "roi": roi,
            "trailing_stop": False,
            "max_open_trades": 1,
        })

    # 2. ARCHETYPE: StochRSI + EMA Momentum Cross Scalper
    stoch_ind = """
dataframe["ema8"] = ta.EMA(dataframe, timeperiod=8)
dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
stoch = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0)
dataframe["slowk"] = stoch["slowk"]
dataframe["slowd"] = stoch["slowd"]
    """
    stoch_ent = """
stoch_bull = (dataframe["slowk"] > dataframe["slowd"]) & (dataframe["slowk"].shift(1) <= dataframe["slowd"].shift(1)) & (dataframe["slowk"] < 45)
ema_align = (dataframe["ema8"] > dataframe["ema21"]) & (dataframe["close"] > dataframe["ema50"])
long_cond = stoch_bull & ema_align & htf_4h_bull & (dataframe["volume"] > 0)
dataframe.loc[long_cond, "enter_long"] = 1
dataframe.loc[long_cond, "enter_tag"] = "stoch_ema_cross"
    """
    for lev, sl, roi in [
        (4.0, -0.08, {"0": 0.16, "15": 0.10, "30": 0.05}),
        (6.0, -0.11, {"0": 0.22, "15": 0.13, "30": 0.07}),
        (8.0, -0.14, {"0": 0.28, "15": 0.16, "30": 0.09}),
    ]:
        configs.append({
            "name": f"stoch_cross_{int(lev)}x",
            "archetype": "StochRSI + EMA Momentum",
            "indicators": stoch_ind,
            "entry_code": stoch_ent,
            "can_short": False,
            "leverage": lev,
            "sl": sl,
            "roi": roi,
            "trailing_stop": False,
            "max_open_trades": 1,
        })

    # 3. ARCHETYPE: Bollinger Band Squeeze Breakout Scalper
    bb_ind = """
dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
dataframe["bb_upper"] = boll["upperband"]
dataframe["bb_middle"] = boll["middleband"]
dataframe["bb_lower"] = boll["lowerband"]
    """
    bb_ent = """
breakout = (dataframe["close"] > dataframe["bb_upper"]) & (dataframe["close"].shift(1) <= dataframe["bb_upper"].shift(1))
vol_surge = dataframe["volume"] > dataframe["vol_ma"] * 1.2
rsi_ok = (dataframe["rsi"] > 52) & (dataframe["rsi"] < 70)
long_cond = breakout & vol_surge & rsi_ok & htf_4h_bull
dataframe.loc[long_cond, "enter_long"] = 1
dataframe.loc[long_cond, "enter_tag"] = "bb_squeeze_breakout"
    """
    for lev, sl, roi in [
        (4.0, -0.08, {"0": 0.18, "15": 0.10, "30": 0.05}),
        (6.0, -0.10, {"0": 0.22, "15": 0.12, "30": 0.06}),
    ]:
        configs.append({
            "name": f"bb_breakout_{int(lev)}x",
            "archetype": "BB Squeeze Breakout",
            "indicators": bb_ind,
            "entry_code": bb_ent,
            "can_short": False,
            "leverage": lev,
            "sl": sl,
            "roi": roi,
            "trailing_stop": False,
            "max_open_trades": 1,
        })

    # 4. ARCHETYPE: MACD Fast Trend Scalper
    macd_ind = """
dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
dataframe["macd"] = macd["macd"]
dataframe["macdsignal"] = macd["macdsignal"]
dataframe["macdhist"] = macd["macdhist"]
    """
    macd_ent = """
macd_cross = (dataframe["macd"] > dataframe["macdsignal"]) & (dataframe["macd"].shift(1) <= dataframe["macdsignal"].shift(1))
hist_up = dataframe["macdhist"] > 0
rsi_bull = (dataframe["rsi"] > 45) & (dataframe["rsi"] < 65)
long_cond = macd_cross & hist_up & rsi_bull & htf_4h_bull
dataframe.loc[long_cond, "enter_long"] = 1
dataframe.loc[long_cond, "enter_tag"] = "macd_scalp"
    """
    for lev, sl, roi in [
        (4.0, -0.08, {"0": 0.16, "15": 0.10, "30": 0.05}),
        (6.0, -0.11, {"0": 0.22, "15": 0.12, "30": 0.07}),
    ]:
        configs.append({
            "name": f"macd_scalp_{int(lev)}x",
            "archetype": "MACD Histogram Scalper",
            "indicators": macd_ind,
            "entry_code": macd_ent,
            "can_short": False,
            "leverage": lev,
            "sl": sl,
            "roi": roi,
            "trailing_stop": False,
            "max_open_trades": 1,
        })

    # 5. ARCHETYPE: RSI Oversold Reversal Dip Scalper
    rsi_ind = """
dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
boll = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
dataframe["bb_lower"] = boll["lowerband"]
    """
    rsi_ent = """
oversold = (dataframe["rsi"] < 35) | (dataframe["rsi"].shift(1) < 32)
bounce = (dataframe["close"] > dataframe["open"]) & (dataframe["low"] <= dataframe["bb_lower"])
long_cond = oversold & bounce & htf_4h_bull & (dataframe["volume"] > 0)
dataframe.loc[long_cond, "enter_long"] = 1
dataframe.loc[long_cond, "enter_tag"] = "rsi_extreme_dip"
    """
    for lev, sl, roi in [
        (4.0, -0.08, {"0": 0.18, "15": 0.10, "30": 0.05}),
        (6.0, -0.10, {"0": 0.22, "15": 0.12, "30": 0.07}),
    ]:
        configs.append({
            "name": f"rsi_rev_{int(lev)}x",
            "archetype": "RSI Oversold Reversal",
            "indicators": rsi_ind,
            "entry_code": rsi_ent,
            "can_short": False,
            "leverage": lev,
            "sl": sl,
            "roi": roi,
            "trailing_stop": False,
            "max_open_trades": 1,
        })

    return configs

if __name__ == "__main__":
    configs = build_all_configurations()
    print(f"=== STARTING MATRIX SEARCH ACROSS {len(configs)} CONFIGURATIONS ===")
    results = []
    for idx, c in enumerate(configs, 1):
        print(f"[{idx}/{len(configs)}] Testing {c['name']} ({c['archetype']} {c['leverage']}x)...")
        res = run_single_test(c)
        results.append(res)
        if "error" in res:
            print(f"   -> ERROR: {res['error']}")
        else:
            print(f"   -> Trades={res['trades']} | Win%={res['winrate']}% ({res['wins']}W/{res['losses']}L) | Profit=${res['profit_abs']} ({res['profit_pct']}%) | PF={res['profit_factor']} | DD={res['drawdown']}%")

    with open("/root/mega_scalp_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\n=== MATRIX SEARCH COMPLETE. RESULTS SAVED TO /root/mega_scalp_results.json ===")

#!/usr/bin/env python3
import subprocess
import glob
import os
import zipfile
import json

STRATEGIES_DIR = "/root/ft_userdata/user_data/strategies"
RESULTS_DIR = "/root/ft_userdata/user_data/backtest_results"

TEMPLATE = """# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
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
                "max_allowed_drawdown": 0.15,
            }},
        ]

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
        vol_surge = dataframe["volume"] > dataframe["vol_ma"] * {vol_mult}
        rsi_ok = (dataframe["rsi"] > {rsi_min}) & (dataframe["rsi"] < {rsi_max})

        long_cond = breakout & vol_surge & rsi_ok & htf_4h_bull
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "bb_breakout_scalp"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        open_trades = Trade.get_open_trades()
        return len(open_trades) < 1
"""

def test_bb_variant(p):
    name = p["name"]
    code = TEMPLATE.format(
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
        print(f"FAILED {name}: {proc.stderr[-200:]}")
        return None

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
                print(f"{name} ({p['leverage']}x, SL={p['sl']}): Trades={t}, W={w}, L={l}, Win%={wr}%, Profit=${p_abs} ({p_pct}%), PF={pf}, DD={dd}%, AvgHold={hold}")
                return s

CANDIDATES = [
    # 1. Base winner 6x
    {
        "name": "bb_tune_6x_base",
        "leverage": 6.0,
        "sl": -0.10,
        "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
    },
    # 2. Faster take-profit ladder
    {
        "name": "bb_tune_6x_fast_tp",
        "leverage": 6.0,
        "sl": -0.10,
        "roi": {"0": 0.18, "10": 0.10, "20": 0.05},
    },
    # 3. 7x leverage with SL -11%
    {
        "name": "bb_tune_7x_balanced",
        "leverage": 7.0,
        "sl": -0.11,
        "roi": {"0": 0.24, "15": 0.14, "30": 0.07},
    },
    # 4. 8x leverage with SL -12%
    {
        "name": "bb_tune_8x_aggressive",
        "leverage": 8.0,
        "sl": -0.12,
        "roi": {"0": 0.28, "15": 0.16, "30": 0.08},
    },
    # 5. Volume multiplier 1.1x (more trades)
    {
        "name": "bb_tune_6x_more_trades",
        "leverage": 6.0,
        "sl": -0.10,
        "roi": {"0": 0.22, "15": 0.12, "30": 0.06},
        "vol_mult": 1.1,
        "rsi_min": 50,
        "rsi_max": 72,
    },
    # 6. Trailing stop lock-in (+4% ROE trail)
    {
        "name": "bb_tune_6x_trailing",
        "leverage": 6.0,
        "sl": -0.10,
        "roi": {"0": 0.25, "20": 0.15, "45": 0.08},
        "trailing_stop": True,
        "trail_pos": 0.018,
        "trail_offset": 0.045,
    }
]

if __name__ == "__main__":
    for c in CANDIDATES:
        test_bb_variant(c)

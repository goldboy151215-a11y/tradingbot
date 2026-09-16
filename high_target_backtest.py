import json
import numpy as np
import pandas as pd
from datetime import datetime
import talib.abstract as ta
import os

# Load 5m & 4h historical data
DATA_DIR = "/root/ft_userdata/user_data/data/bybit/futures"
PAIRS = ["BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT", "BNB_USDT_USDT", "DOGE_USDT_USDT", "XRP_USDT_USDT"]

pair_data = {}
for p in PAIRS:
    f_5m = f"{DATA_DIR}/{p}-5m-futures.feather"
    f_4h = f"{DATA_DIR}/{p}-4h-futures.feather"
    if os.path.exists(f_5m) and os.path.exists(f_4h):
        df_5m = pd.read_feather(f_5m)
        df_4h = pd.read_feather(f_4h)
        df_5m['date'] = pd.to_datetime(df_5m['date'], utc=True)
        df_4h['date'] = pd.to_datetime(df_4h['date'], utc=True)
        pair_data[p] = (df_5m, df_4h)

def run_simulation(leverage, stake_mode, stake_val, bb_std=1.8, sl_roe=-0.10, roi_ladder=None):
    if roi_ladder is None:
        roi_ladder = {"0": 0.22, "15": 0.12, "30": 0.06}
        
    start_equity = 168.0
    equity = start_equity
    peak_equity = start_equity
    max_drawdown = 0.0
    
    trades = []
    
    # Pre-calculate indicators for each pair
    prepared = {}
    for p, (df_5m, df_4h) in pair_data.items():
        d5 = df_5m.copy().sort_values('date').reset_index(drop=True)
        d4 = df_4h.copy().sort_values('date').reset_index(drop=True)
        
        d4['ema20_4h'] = ta.EMA(d4, timeperiod=20)
        d4['close_4h'] = d4['close']
        
        d5 = pd.merge_asof(d5, d4[['date', 'ema20_4h', 'close_4h']], on='date', direction='backward')
        
        d5['rsi'] = ta.RSI(d5, timeperiod=14)
        d5['vol_ma'] = d5['volume'].rolling(20).mean()
        boll = ta.BBANDS(d5, timeperiod=20, nbdevup=bb_std, nbdevdn=bb_std)
        d5['bb_upper'] = boll['upperband']
        
        # Long condition
        htf_bull = d5['close_4h'] > d5['ema20_4h']
        breakout = (d5['close'] > d5['bb_upper']) & (d5['close'].shift(1) <= d5['bb_upper'].shift(1))
        vol_surge = d5['volume'] > d5['vol_ma'] * 1.2
        rsi_ok = (d5['rsi'] > 52) & (d5['rsi'] < 70)
        
        d5['signal'] = breakout & vol_surge & rsi_ok & htf_bull
        prepared[p] = d5
        
    # Merge all candles in chronological timeline
    timeline = []
    for p, d5 in prepared.items():
        for idx, row in d5.iterrows():
            if idx < 50:
                continue
            timeline.append((row['date'], p, idx))
            
    timeline.sort(key=lambda x: x[0])
    
    # Fast trade simulator (1 slot focused)
    active_trade = None
    
    # Calculate price sl from ROE sl: sl_price = sl_roe / leverage
    sl_price_pct = abs(sl_roe) / leverage
    
    for dt, p, idx in timeline:
        d5 = prepared[p]
        row = d5.iloc[idx]
        
        # Check active trade
        if active_trade is not None:
            if active_trade['pair'] == p:
                entry_rate = active_trade['entry_rate']
                current_high = row['high']
                current_low = row['low']
                current_close = row['close']
                hold_minutes = (dt - active_trade['open_date']).total_seconds() / 60.0
                
                # Check stoploss
                lowest_price_ratio = (current_low - entry_rate) / entry_rate
                if lowest_price_ratio <= -sl_price_pct:
                    exit_price = entry_rate * (1.0 - sl_price_pct)
                    loss_roe = -sl_price_pct * leverage
                    loss_usdt = active_trade['margin'] * loss_roe
                    equity += loss_usdt
                    active_trade['closed'] = True
                    active_trade['profit_usdt'] = loss_usdt
                    active_trade['profit_ratio'] = -sl_price_pct
                    active_trade['exit_reason'] = 'stoploss'
                    trades.append(active_trade)
                    active_trade = None
                else:
                    # Check ROI ladder
                    target_roe = roi_ladder["30"] if hold_minutes >= 30 else (roi_ladder["15"] if hold_minutes >= 15 else roi_ladder["0"])
                    target_price_pct = target_roe / leverage
                    highest_price_ratio = (current_high - entry_rate) / entry_rate
                    
                    if highest_price_ratio >= target_price_pct:
                        exit_price = entry_rate * (1.0 + target_price_pct)
                        gain_roe = target_price_pct * leverage
                        # deduct taker fee 0.05% per side = 0.1% roundtrip on notional
                        fee = active_trade['margin'] * leverage * 0.001
                        gain_usdt = (active_trade['margin'] * gain_roe) - fee
                        equity += gain_usdt
                        active_trade['closed'] = True
                        active_trade['profit_usdt'] = gain_usdt
                        active_trade['profit_ratio'] = target_price_pct
                        active_trade['exit_reason'] = 'roi'
                        trades.append(active_trade)
                        active_trade = None
                        
            # Track drawdown
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_drawdown:
                max_drawdown = dd
                
            if equity <= 10.0:  # account wiped out
                break
            continue
            
        # Entry check
        if active_trade is None and row['signal']:
            entry_price = row['close']
            if stake_mode == 'fixed':
                margin = min(stake_val, equity * 0.95)
            elif stake_mode == 'compound_pct':
                margin = equity * (stake_val / 100.0)
            else:
                margin = min(25.0, equity * 0.5)
                
            if margin >= 5.0 and equity > 10.0:
                active_trade = {
                    'pair': p,
                    'open_date': dt,
                    'entry_rate': entry_price,
                    'margin': margin,
                    'leverage': leverage
                }
                
    wins = [t for t in trades if t['profit_usdt'] > 0]
    losses = [t for t in trades if t['profit_usdt'] <= 0]
    winrate = len(wins) / len(trades) * 100 if trades else 0.0
    total_profit = equity - start_equity
    
    gross_win = sum(t['profit_usdt'] for t in wins)
    gross_loss = abs(sum(t['profit_usdt'] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else 999.0
    
    return {
        'leverage': leverage,
        'stake_mode': stake_mode,
        'stake_val': stake_val,
        'bb_std': bb_std,
        'trades': len(trades),
        'winrate': round(winrate, 1),
        'start_equity': start_equity,
        'final_equity': round(equity, 2),
        'profit_usdt': round(total_profit, 2),
        'return_pct': round((total_profit / start_equity) * 100, 2),
        'profit_factor': round(profit_factor, 2),
        'max_drawdown': round(max_drawdown, 2),
    }

# Test grid of setups aiming for $100 - $150 profit per month
configs = [
    # Baseline
    (6, 'fixed', 25.0, 1.8, -0.10, "6x Hefboom | $25 Vaste Stake (Huidig)"),
    
    # 1. Higher Stake with Safe 6x Leverage (Compounds or larger portion of $168)
    (6, 'fixed', 50.0, 1.8, -0.10, "6x Hefboom | $50 Vaste Stake (~30% equity)"),
    (6, 'fixed', 75.0, 1.8, -0.10, "6x Hefboom | $75 Vaste Stake (~45% equity)"),
    (6, 'fixed', 100.0, 1.8, -0.10, "6x Hefboom | $100 Vaste Stake (~60% equity)"),
    (6, 'compound_pct', 50.0, 1.8, -0.10, "6x Hefboom | 50% Compounding Stake"),
    (6, 'compound_pct', 70.0, 1.8, -0.10, "6x Hefboom | 70% Compounding Stake"),
    
    # 2. 10x - 15x Leverage with Medium Stake
    (10, 'fixed', 50.0, 1.8, -0.12, "10x Hefboom | $50 Vaste Stake"),
    (10, 'fixed', 75.0, 1.8, -0.12, "10x Hefboom | $75 Vaste Stake"),
    (10, 'compound_pct', 50.0, 1.8, -0.12, "10x Hefboom | 50% Compounding Stake"),
    (15, 'fixed', 40.0, 1.8, -0.15, "15x Hefboom | $40 Vaste Stake"),
    (15, 'fixed', 60.0, 1.8, -0.15, "15x Hefboom | $60 Vaste Stake"),
    
    # 3. 20x - 30x Leverage Scalping (Strakke stop -8% ROE)
    (20, 'fixed', 35.0, 1.8, -0.10, "20x Hefboom | $35 Vaste Stake (SL -0.5% prijs)"),
    (20, 'fixed', 50.0, 1.8, -0.10, "20x Hefboom | $50 Vaste Stake (SL -0.5% prijs)"),
    (25, 'fixed', 30.0, 1.8, -0.10, "25x Hefboom | $30 Vaste Stake (SL -0.4% prijs)"),
    (30, 'fixed', 25.0, 1.8, -0.10, "30x Hefboom | $25 Vaste Stake (SL -0.33% prijs)"),
    (50, 'fixed', 20.0, 1.8, -0.10, "50x Hefboom | $20 Vaste Stake (SL -0.2% prijs)"),
    (50, 'fixed', 35.0, 1.8, -0.10, "50x Hefboom | $35 Vaste Stake (SL -0.2% prijs)"),
]

results = []
for lev, mode, val, bb, sl, label in configs:
    res = run_simulation(lev, mode, val, bb_std=bb, sl_roe=sl)
    res['label'] = label
    results.append(res)
    print(f"[{label}] -> Trades={res['trades']} | Win%={res['winrate']}% | Winst=${res['profit_usdt']} ({res['return_pct']}%) | DD={res['max_drawdown']}%")

with open('/root/high_target_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print("DONE! Saved to /root/high_target_results.json")

#!/usr/bin/env python3
import glob
import os
import zipfile
import json

zips = sorted(glob.glob('/root/ft_userdata/user_data/backtest_results/*.zip'), key=os.path.getmtime)
for zf in reversed(zips):
    with zipfile.ZipFile(zf) as z:
        for f in z.namelist():
            if f.endswith('.json') and not f.endswith('_config.json'):
                data = json.loads(z.read(f).decode())
                if 'exp_pullback4h_5x' in data.get('strategy', {}):
                    s = data['strategy']['exp_pullback4h_5x']
                    print('Strategy: exp_pullback4h_5x')
                    print('Trades:', s.get('total_trades'), 'Wins:', s.get('wins'), 'Losses:', s.get('losses'))
                    print('Win%:', s.get('winrate')*100)
                    print('Profit:', s.get('profit_total_abs'))
                    print('Profit Factor:', s.get('profit_factor'))
                    print('Max Drawdown:', s.get('max_drawdown_account')*100)
                    print('\nPair Breakdown:')
                    for pb in s.get('results_per_pair', []):
                        k = pb.get('key')
                        tr = pb.get('trades')
                        wr = round(pb.get('winrate', 0)*100, 1)
                        pabs = round(pb.get('profit_total_abs', 0), 2)
                        pf = round(pb.get('profit_factor', 0), 2) if pb.get('profit_factor') else 0
                        print(f"  {k}: trades={tr}, win%={wr}%, profit=${pabs}, pf={pf}")
                    exit(0)

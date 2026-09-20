# WEEX Futures 12x Mega Runner Dual-Regime Quant Bot (Long + Short)

Geavanceerde algoritmische trading bot voor WEEX Futures (Perpetual USDT Swaps) met **Mega Runner Execution (Long + Short)**, **12x Isolated Leverage**, **3 Actieve Slots**, **58% Dynamic Auto-Compounding**, en **Telegram Command Center & Realtime Notifier**.

---

## 📊 Dual-Regime Strategie Specificaties (Long + Short)

De live strategie ([`weex_futures_quant.py`](file:///root/ft_userdata/user_data/strategies/weex_futures_quant.py)) draait op 5m candles met 4H trendfilter en tradeert zowel **Bullish breakouts (Long)** als **Bearish relief pullbacks (Short)**:

- **Exchange**: WEEX Futures (`weex` / perpetual swaps)
- **Actieve Whitelist (Core 4)**:
  - `BTC/USDT:USDT`
  - `ETH/USDT:USDT`
  - `SOL/USDT:USDT`
  - `AVAX/USDT:USDT`
- **Hefboom (Leverage)**: `12x Isolated`
- **Compounding Model**: Dynamische herinvestering van **58%** van de actuele wallet balance per trade.
- **Max Gelijktijdige Posities**: **`max_open_trades: 3`** (optimale slotbezetting voor continu trade-volume en diversificatie).

### 🟢 1. LONG REGIME (Breakout Scalp & Runner)
- **Execution Timeframe**: 5m
- **Trendfilter**: 4H Close > 4H EMA20 (Bullish HTF Trend)
- **Entry Signaal**:
  - 5m Bollinger Bands Breakout (Close > Upper Band, 20-period, 1.8 STD)
  - Volume Surge (> 1.2x 20-period Volume SMA)
  - Gezonde RSI (52 - 70)
- **Exit Logica**:
  - Mega Runner ROI Ladder (tot +80% ROE)
  - Trailing Stop Winstzekering vanaf +25% ROE

### 🔴 2. SHORT REGIME (Pullback Rejection)
- **Execution Timeframe**: 5m
- **Trendfilter**: 4H Close < 4H EMA20 EN 4H RSI < 52 (Bearish HTF Trend)
- **Entry Signaal**:
  - 5m Relief Pullback naar EMA20 (High >= EMA20 & Close < EMA20)
  - Rode Candle Rejection (Close < Open)
  - RSI Bearish Momentum (50 - 66)
  - Volume Bevestiging (> 1.1x Volume SMA)
- **Flash Breakeven Lock**:
  - Zodra een short positie +6% ROE (+0.5% prijsdaling bij 12x) bereikt, schiet de stoploss automatisch naar Breakeven (+1% fee buffer).

---

## 🎯 Mega Runner ROI & Trailing Winstladder

Bij **12x Leverage**:
| Duur van de Trade | Koersbeweging | ROE Winstdoel |
| :--- | :--- | :--- |
| **0 - 30 min (Directe Megapump)** | +6.67% | **`+80.0% ROE`** |
| **30 - 60 min (Trend Uitschieter)** | +4.17% | **`+50.0% ROE`** |
| **> 60 min (Consolidatie Winst)** | +2.50% | **`+30.0% ROE`** |

* 🛡️ **Trailing Winstzekering:** Zodra een positie **`+25% ROE`** bereikt, trekt de bot automatisch een **5% Trailing Stoploss** mee omhoog om gerealiseerde winst te beveiligen tegen terugval.
* 🚨 **Beschermende Noodstop:** `-50% ROE` hard limit op exchange (-4.17% onderliggende koersdaling).

---

## 📁 Gearchiveerde Versies

* **[`weex_futures_quant_scalp_44_24_12_archive.py`](file:///root/ft_userdata/user_data/strategies/weex_futures_quant_scalp_44_24_12_archive.py)**: De eerdere snelle scalper variant met 44/24/12 trap en 2 slots.

---

## 🚀 Architectuur & Componenten

1. **Freqtrade Core (Docker)**
   - Draait in een geïsoleerde container met exchange connectors en strategy engine.
   - Configuratie: `ft_userdata/user_data/config.json`
   - Strategie: `ft_userdata/user_data/strategies/weex_futures_quant.py`

2. **Telegram Command Center & Realtime Notifier**
   - Bestand: `ft_userdata/user_data/telegram_command_center.py`
   - **Thread 1**: Luistert naar Telegram commando's (`/status`, `/balance`, `/trades`, `/config`, `/daily`, `/profit`, `/start`, `/stop`).
   - **Thread 2**: Realtime SQLite Trade Monitor die direct meldingen stuurt bij elke geopende of gesloten positie.

3. **Web Dashboard Server**
   - Realtime HTML/JS status visualisatie op poort 80 (`web_dashboard_server.py`).

4. **Unit & Integratietests**
   - 37 geautomatiseerde tests (`pytest tests/`).

---

## 🛠️ Installatie & Gebruik

### 1. Repository klonen & Python omgeving
```bash
git clone git@github.com:goldboy151215-a11y/tradingbot.git
cd tradingbot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Docker & Freqtrade opstarten
```bash
cd ft_userdata
docker compose up -d
```

### 3. Telegram Notifier starten
```bash
nohup /root/.venv/bin/python3 /root/ft_userdata/user_data/telegram_command_center.py >> /root/ft_userdata/user_data/logs/telegram_center.log 2>&1 &
```

### 4. Tests uitvoeren
```bash
pytest -v tests/
```

---

## 🔒 Beveiliging
Gevoelige API keys, Telegram tokens, databases (`*.sqlite`) en logs worden via `.gitignore` lokaal beschermd en niet gepusht.

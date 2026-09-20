# WEEX Futures 12x Dual-Regime Quant Trading Bot (Long + Short)

Geavanceerde algoritmische trading bot voor WEEX Futures (Perpetual USDT Swaps) met **Dual-Regime Execution (Long + Short)**, **12x Isolated Leverage**, **58% Dynamic Auto-Compounding**, en **Telegram Command Center & Realtime Notifier**.

---

## 📊 Dual-Regime Strategie Specificaties (Long + Short)

De strategie (`weex_futures_quant.py`) draait op 5m candles met 4H trendfilter en tradeert zowel **Bullish breakouts (Long)** als **Bearish relief pullbacks (Short)**:

- **Exchange**: WEEX Futures (`weex` / perpetual swaps)
- **Actieve Whitelist (Core 4)**:
  - `BTC/USDT:USDT`
  - `ETH/USDT:USDT`
  - `SOL/USDT:USDT`
  - `AVAX/USDT:USDT`
- **Hefboom (Leverage)**: `12x Isolated`
- **Compounding Model**: Dynamische herinvestering van **58%** van de actuele wallet balance per trade (`max_open_trades: 2`).

### 🟢 1. LONG REGIME (Breakout Scalp)
- **Execution Timeframe**: 5m
- **Trendfilter**: 4H Close > 4H EMA20 (Bullish HTF Trend)
- **Entry Signaal**:
  - 5m Bollinger Bands Breakout (Close > Upper Band, 20-period, 1.8 STD)
  - Volume Surge (> 1.2x 20-period Volume SMA)
  - Gezonde RSI (52 - 70)
- **Exit Logica**:
  - Dynamische ROI Ladder (tot +44% ROE)
  - Beschermende Stoploss

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

## 🎯 ROI & Risicobeheer Ladder

Bij **12x Leverage**:
| Duur | Prijsbeweging | ROE Winst |
| :--- | :--- | :--- |
| **0 - 15 min** | +3.67% | **+44.0% ROE** |
| **15 - 30 min** | +2.00% | **+24.0% ROE** |
| **> 30 min** | +1.00% | **+12.0% ROE** |

- **Beschermende Noodstop**: `-50% ROE` hard limit op exchange
- **Scalp Stoploss**: `-20% ROE` (-1.67% onderliggende marktbeweging)

---

## 🚀 Architectuur & Componenten

1. **Freqtrade Core (Docker)**
   - Draait in een geïsoleerde container met exchange connectors en strategy engine.
   - Configuratie: `ft_userdata/user_data/config.json`
   - Strategie: `ft_userdata/user_data/strategies/weex_futures_quant.py`

2. **Telegram Command Center & Realtime Notifier**
   - Bestand: `ft_userdata/user_data/telegram_command_center.py`
   - **Thread 1**: Luistert naar Telegram commando's met interactieve knoppen:
     - `▶️ /start` - Start trading loop (met strikte config checksum verificatie)
     - `⏹️ /stop` - Pauzeert trading loop
     - `📊 /status` - Live actieve posities en ongerealiseerde PnL
     - `💰 /balance` - Actueel USDT saldo en vrije marge
     - `📈 /profit` - Totaal winst- en verliesoverzicht
     - `📜 /trades` - Laatste gesloten trades met resultaat en reden
     - `⚙️ /config` - Whitelist, hefboom & live 58% compounding inzet
     - `📅 /daily` - Dagelijkse PnL statistieken
     - `ℹ️ /help` - Overzicht van alle commando's
   - **Thread 2**: Realtime SQLite Trade Monitor die automatisch notificaties stuurt zodra een positie (Long of Short) wordt geopend of gesloten met winst/verlies.

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

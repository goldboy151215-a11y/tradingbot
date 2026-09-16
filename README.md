# WEEX Futures 12x Auto-Compounding Quant Trading Bot

Geavanceerde algoritmische trading bot voor WEEX Futures (Perpetual USDT Swaps) met **12x Isolated Leverage**, **58% Dynamic Auto-Compounding**, en **Telegram Command Center & Realtime Notifier**.

---

## 📊 Live Strategie Specificaties

- **Exchange**: WEEX Futures (`weex` / perpetual swaps)
- **Actieve Whitelist (Core 4)**:
  - `BTC/USDT:USDT`
  - `ETH/USDT:USDT`
  - `SOL/USDT:USDT`
  - `AVAX/USDT:USDT`
- **Hefboom (Leverage)**: `12x Isolated`
- **Compounding Model**: Dynamische herinvestering van **58%** van de actuele wallet balance per trade (`max_open_trades: 1`).
- **Signaal Logica**:
  - Bollinger Bands Squeeze & Expansion (Period 20, 1.8 STD)
  - RSI Trend Filter (36 - 68)
  - Volume Multiplier (1.1x t.o.v. 20-periode SMA)
- **Risicobeheer**:
  - Stoploss: `-20% ROE` (-1.67% onderliggende marktbeweging)
  - Trailing ROI ladder:
    - `0 min`: `+44% ROE` (+3.67% prijs)
    - `15 min`: `+24% ROE` (+2.00% prijs)
    - `45 min`: `+12% ROE` (+1.00% prijs)

---

## 🚀 Architectuur & Componenten

1. **Freqtrade Core (Docker)**
   - Draait in een geïsoleerde container met exchange connectors en strategy engine.
   - Configuratie: `ft_userdata/user_data/config.json`
   - Strategie: `ft_userdata/user_data/strategies/weex_futures_quant.py`

2. **Telegram Command Center & Realtime Notifier**
   - Systemd Service: `quant-telegram.service`
   - Bestand: `ft_userdata/user_data/telegram_command_center.py`
   - **Thread 1**: Luistert naar Telegram commando's met interactieve knoppen:
     - `▶️ /start` - Start bot / dashboard
     - `⏹️ /stop` - Stop trading
     - `📊 /status` - Live actieve posities en PnL
     - `💰 /balance` - Actueel USDT saldo en wallet details
     - `📈 /profit` - Winst- en verliesoverzicht
     - `📜 /trades` - Laatste 5 afgesloten trades
     - `⚙️ /config` - Whitelist, hefboom & live berekende volgende inzet (58% compounding)
     - `📅 /daily` - Dagelijkse PnL statistieken
     - `ℹ️ /help` - Overzicht van commando's
   - **Thread 2**: Realtime SQLite Trade Monitor die automatisch notificaties stuurt zodra een trade wordt geopend of gesloten met winst/verlies percentage en bedrag.

3. **Standalone Python Backtesting & ML Engine**
   - `src/` modules: Risk management, strategy simulatie, runtime state checksums.
   - `tests/`: 37 unit en integratietests (`pytest tests/`).

---

## 🛠️ Installatie & Herstel op een Nieuwe Server

### 1. Repository klonen & Dependencies
```bash
git clone git@github.com:goldboy151215-a11y/tradingbot.git
cd tradingbot

# Python virtuele omgeving opzetten
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Docker & Freqtrade opstarten
```bash
cd ft_userdata
docker compose up -d
```

### 3. Telegram Notifier als Systemd Service starten
```bash
# Kopieer en herlaad systemd
sudo cp ft_userdata/quant-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quant-telegram.service
sudo systemctl status quant-telegram.service
```

### 4. Tests uitvoeren
```bash
source .venv/bin/activate
pytest -v tests/
```

---

## 🔒 Beveiliging

Alle gevoelige API-sleutels, Telegram bot tokens en database-bestanden worden via `.gitignore` lokaal gehouden en niet opgenomen in de publieke repository.

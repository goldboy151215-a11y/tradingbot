# 🛡️ FundedNext PropFirm Bot #1: Intraday Momentum & NY Open

High-frequency algorithmic futures trading bot connected to Tradovate WebSocket for FundedNext $50,000 Challenge Account #1.

---

## 📂 Source Code Files

* [`main.py`](file:///root/propfirm_bot_account1/main.py): Production asynchronous event loop, Tradovate WebSocket market data & order connection, PnL monitoring, and daily reset handler.
* [`strategy_shield.py`](file:///root/propfirm_bot_account1/strategy_shield.py): Quantitative strategy engine:
  - **New York Open 15m ORB:** 13:30 - 13:45 UTC range breakout logic.
  - **Intraday 5m Momentum:** Bollinger Band breakout + EMA20 rejection filters.
* [`order_manager.py`](file:///root/propfirm_bot_account1/order_manager.py): Tradovate order routing:
  - 6 contracts execution sizing.
  - Bracket orders (OCO StopLoss / TakeProfit).
  - **Breakeven trigger:** Exact entry lock strictly after **+30 points** profit is touched (trailing stop disabled).
* [`auth_manager.py`](file:///root/propfirm_bot_account1/auth_manager.py): OAuth authentication with automated token refreshing and caching.
* [`telegram_bot.py`](file:///root/propfirm_bot_account1/telegram_bot.py): Interactive Telegram bot with keyboard commands (`/status`, `/pnl`, `/close`, `/daily`).
* [`telegram_alerts.py`](file:///root/propfirm_bot_account1/telegram_alerts.py): Real-time trade, execution, and risk alert dispatcher.
* [`tradovate-bot.service`](file:///root/propfirm_bot_account1/tradovate-bot.service): Linux systemd unit service configuration.
* [`config.example.json`](file:///root/propfirm_bot_account1/config.example.json): Configuration template with rules & parameters.

---

## ⚙️ Risk Configuration
* **Contracts:** 6 contracts per trade
* **Stop Loss:** 30.0 pts MNQ (-$360) / 6.0 pts MGC
* **Take Profit:** 50.0 pts MNQ (+$600)
* **Breakeven:** Triggers at +30.0 pts; SL moved to entry ($0 risk)
* **Max Daily Loss:** -$450.00
* **Daily Profit Cap:** +$900.00

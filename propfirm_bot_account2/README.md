# 🎯 FundedNext PropFirm Bot #2: Multi-Asset 15m ORB

Institutional Opening Range Breakout (ORB) quant bot connected to Tradovate WebSocket for FundedNext $50,000 Challenge Account #2.

---

## 📂 Source Code Files

* [`main.py`](file:///root/propfirm_bot_account2/main.py): Production asynchronous event loop managing multi-asset connections and daily session locks.
* [`strategy_orb.py`](file:///root/propfirm_bot_account2/strategy_orb.py): Multi-Asset 15m Opening Range Breakout logic:
  - **Micro Gold (`MGCZ6`)**: European/London Open (08:00 - 08:15 UTC range).
  - **Micro Nasdaq (`MNQZ6`)**: New York Wall Street Open (13:30 - 13:45 UTC range).
  - Strictly **1 trade per asset per day** to eliminate overtrading.
* [`order_manager.py`](file:///root/propfirm_bot_account2/order_manager.py): Tradovate order routing:
  - 6 contracts execution sizing.
  - Bracket orders (OCO StopLoss / TakeProfit).
  - **Breakeven trigger:** Exact entry lock strictly after **+30 points** profit is touched (trailing stop disabled).
* [`auth_manager.py`](file:///root/propfirm_bot_account2/auth_manager.py): Isolated OAuth authentication with separate token management.
* [`telegram_alerts.py`](file:///root/propfirm_bot_account2/telegram_alerts.py): Alert dispatcher formatted with `🛡️ [FUNDEDNEXT ACC #2 - ORB MULTI-ASSET]`.
* [`tradovate-bot-acc2.service`](file:///root/propfirm_bot_account2/tradovate-bot-acc2.service): Linux systemd unit service configuration.
* [`config.example.json`](file:///root/propfirm_bot_account2/config.example.json): Configuration template with rules & parameters.

---

## ⚙️ Risk Configuration
* **Contracts:** 6 contracts per trade
* **Stop Loss:** 25.0 pts MNQ (-$300) / 4.0 pts MGC (-$240)
* **Take Profit:** 50.0 pts MNQ (+$600) / 8.0 pts MGC (+$480)
* **Breakeven:** Triggers at +30.0 pts; SL moved to entry ($0 risk)
* **Max Daily Loss:** -$400.00
* **Daily Profit Cap:** +$900.00

# 🏛️ Autonomous Trading Infrastructure & Code Backup

Production-grade algorithmic trading repository containing the complete source codes and strategies for both Crypto Futures (WEEX) and TradFi Regulated Futures (CME / FundedNext Tradovate).

---

## 📂 Repository Structure

```
.
├── weex_crypto_bot/             # ⚡ WEEX Crypto Futures Trading Systems
│   ├── engine/                  # Standalone CCXT autonomous execution bot
│   │   ├── runner.py            # Main execution loop & bias calculation
│   │   ├── exchange.py          # CCXT exchange wrapper with isolated margin
│   │   ├── risk.py              # Dynamic position sizing & risk controls
│   │   ├── strategy.py          # Dealing Range + Fibonacci POI strategy
│   │   └── runtime_state.py     # State manager with SHA-256 integrity checksums
│   ├── freqtrade_quant/         # Freqtrade 12x production quant suite
│   │   ├── weex_futures_quant.py# Dual-Regime 12x quant strategy
│   │   ├── weex_exchange_patch.py# WEEX API signature & connector patch
│   │   ├── telegram_command_center.py# Interactive Telegram command bot
│   │   ├── web_dashboard_server.py # Real-time HTTP dashboard server
│   │   ├── Dockerfile           # Production container build
│   │   ├── docker-compose.yml   # Multi-service container orchestration
│   │   └── config.example.json  # Sanitized configuration template
│   └── README.md
│
├── propfirm_bot_account1/       # 🛡️ FundedNext PropFirm Bot #1 ($50k Intraday Scalper)
│   ├── main.py                  # Async WebSocket event loop & risk monitors
│   ├── strategy_shield.py       # 15m NY Open ORB + 5m momentum strategy logic
│   ├── order_manager.py         # Tradovate order routing, 6 contracts, 30pt Breakeven
│   ├── auth_manager.py          # Tradovate OAuth authentication & token refresh
│   ├── telegram_bot.py          # Interactive Telegram control bot
│   ├── telegram_alerts.py       # Real-time execution alert dispatcher
│   ├── tradovate-bot.service    # Linux systemd daemon definition
│   ├── config.example.json      # Sanitized configuration template
│   └── README.md
│
├── propfirm_bot_account2/       # 🎯 FundedNext PropFirm Bot #2 ($50k Multi-Asset ORB)
│   ├── main.py                  # Async WebSocket event loop & session locks
│   ├── strategy_orb.py          # 15m Opening Range Breakout (Micro Nasdaq & Micro Gold)
│   ├── order_manager.py         # Tradovate order routing, 6 contracts, 30pt Breakeven
│   ├── auth_manager.py          # Isolated Tradovate OAuth authentication
│   ├── telegram_alerts.py       # Acc #2 branded telemetry alert dispatcher
│   ├── tradovate-bot-acc2.service # Linux systemd daemon definition
│   ├── config.example.json      # Sanitized configuration template
│   └── README.md
│
├── requirements.txt             # Python dependencies
└── README.md
```

---

## 🚀 Active Systems Summary

| System | Platform / Exchange | Strategy | Sizing | Breakeven Rule |
| :--- | :--- | :--- | :--- | :--- |
| **WEEX Bot** | WEEX Futures | Dual-Regime Quant + CCXT POI | 12x Dynamic Leverage | Auto-lock at +6% ROE |
| **PropFirm #1** | FundedNext / Tradovate | Intraday Scalper + 15m NY Open | 6 Contracts (`MNQZ6`/`MGCZ6`) | Move to BE at **+30.0 pts** |
| **PropFirm #2** | FundedNext / Tradovate | Multi-Asset 15m ORB (London/NY) | 6 Contracts (`MNQZ6`/`MGCZ6`) | Move to BE at **+30.0 pts** |

---

## 🔒 Security Notice
All private credentials, passwords, active Tradovate tokens, SQLite databases, and environment keys are strictly excluded via [`.gitignore`](file:///.gitignore). All configuration files in this repository use sanitized `config.example.json` templates.

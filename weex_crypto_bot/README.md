# ⚡ WEEX Crypto Futures Bot

Full quantitative algorithmic trading codebase for WEEX Crypto Futures.

---

## 📂 Architecture

### 1. `engine/` — Standalone CCXT Execution Bot
Pure Python autonomous trading runner built on top of `ccxt`:
* [`runner.py`](file:///root/weex_crypto_bot/engine/runner.py): Main event loop, 4H bias evaluation, 15m execution loop, state persistence.
* [`exchange.py`](file:///root/weex_crypto_bot/engine/exchange.py): CCXT exchange interface with isolated margin mode & rate-limiting.
* [`risk.py`](file:///root/weex_crypto_bot/engine/risk.py): Dynamic position sizing, risk-to-reward calculation, hard drawdown limits.
* [`strategy.py`](file:///root/weex_crypto_bot/engine/strategy.py): Dealing Range swing pivots + dynamic Fibonacci POI (0.618 - 0.786) zones & ATR calculations.
* [`runtime_state.py`](file:///root/weex_crypto_bot/engine/runtime_state.py): Atomic JSON state manager with SHA-256 integrity checksums.

### 2. `freqtrade_quant/` — 12x Quant System (Live Docker)
* [`weex_futures_quant.py`](file:///root/weex_crypto_bot/freqtrade_quant/weex_futures_quant.py): Active Dual-Regime production strategy (Bull Breakouts + Bearish Pullback Shorts) running with 12x dynamic leverage.
* [`weex_exchange_patch.py`](file:///root/weex_crypto_bot/freqtrade_quant/weex_exchange_patch.py): Custom CCXT exchange patch for WEEX Futures API signature & order lifecycle.
* [`telegram_command_center.py`](file:///root/weex_crypto_bot/freqtrade_quant/telegram_command_center.py): Full Telegram interactive bot for real-time monitoring and manual intervention.
* [`web_dashboard_server.py`](file:///root/weex_crypto_bot/freqtrade_quant/web_dashboard_server.py): Standalone live performance HTTP dashboard.
* [`Dockerfile`](file:///root/weex_crypto_bot/freqtrade_quant/Dockerfile) & [`docker-compose.yml`](file:///root/weex_crypto_bot/freqtrade_quant/docker-compose.yml): Production container definitions.
* [`config.example.json`](file:///root/weex_crypto_bot/freqtrade_quant/config.example.json): Configuration template.

# 🏛️ Institutional Quant & PropFirm Trading Suite

Production-grade algorithmic trading infrastructure running autonomous quantitative execution across Crypto Futures (WEEX) and TradFi Regulated Futures (CME / FundedNext Tradovate).

---

## 🚀 Active Systems Overview

```
                                  VPS Root Environment
                                           │
         ┌─────────────────────────────────┼─────────────────────────────────┐
         │                                 │                                 │
         ▼                                 ▼                                 ▼
   [WEEX BOT]                    [PROPFIRM ACC #1]                 [PROPFIRM ACC #2]
   Crypto Futures                Tradovate Intraday                Tradovate 15m ORB
   Freqtrade + Web/TG            Momentum + NY Open                Multi-Asset Breakout
   12x Leverage                  6 Contracts (MNQ/MGC)             6 Contracts (MNQ/MGC)
```

---

## 1. ⚡ WEEX Futures Quant Bot (`ft_userdata/`)
* **Engine:** Custom patched Freqtrade Futures with live WEEX exchange connector.
* **Active Strategy:** [`weex_futures_quant.py`](file:///root/ft_userdata/user_data/strategies/weex_futures_quant.py)
* **Regime:** Dual-Regime (Bull Breakout Longs + Bearish Pullback Shorts) on top-tier crypto assets.
* **Leverage & Risk:** 12x dynamic leverage, auto-compounding capital allocation, tiered stoploss (-20% to -25% ROE), flash breakeven lock (+6% ROE).
* **Control Center:** 
  - Telegram interactive command center ([`telegram_command_center.py`](file:///root/ft_userdata/user_data/telegram_command_center.py))
  - Real-time web performance dashboard on port 80 ([`web_dashboard_server.py`](file:///root/ft_userdata/user_data/web_dashboard_server.py))

---

## 2. 🛡️ FundedNext PropFirm Bot #1: Intraday Scalper (`tradovate_bot/`)
* **Account:** FundedNext $50k Account (`FNFTCHDONDIEGOTHEHU83523`)
* **Engine:** Pure Python asynchronous WebSocket client connecting directly to Tradovate Order & Market Data WS.
* **Active Strategy:** [`strategy_shield.py`](file:///root/tradovate_bot/strategy_shield.py)
  - **New York Open 15m ORB:** Captures the 13:30 - 13:45 UTC opening range on the Nasdaq (`MNQZ6`).
  - **Intraday 5m Momentum:** Bollinger Band breakouts (Long) & EMA20 Pullback Rejections (Short).
* **Execution & Position Sizing:**
  - **Contracts:** **6 contracts** per trade.
  - **Stop Loss:** 30.0 pts MNQ (-$360) / 6.0 pts MGC.
  - **Take Profit:** 50.0 pts MNQ (+$600).
  - **Breakeven Rule:** Triggered strictly when **+30.0 points** profit is touched; SL moves to exact Entry ($0 risk). No trailing stop.
* **Risk Shield:**
  - Max Daily Loss: `-$450.00`
  - Daily Profit Cap (40% Consistency rule): `+$900.00`
  - Max Overall Loss: `-$1,500.00`
* **Service:** `tradovate-bot.service`

---

## 3. 🎯 FundedNext PropFirm Bot #2: Multi-Asset 15m ORB (`tradovate_bot_acc2/`)
* **Account:** FundedNext $50k Account (`FNFTCHDONDIEGOTHEHU81239`)
* **Engine:** Dedicated isolated Tradovate WebSocket client with separate session token management.
* **Active Strategy:** [`strategy_orb.py`](file:///root/tradovate_bot_acc2/strategy_orb.py)
  - **Multi-Asset Session Edge:**
    1. **Micro Gold (`MGCZ6`)**: European/London Open (08:00 - 08:15 UTC range).
    2. **Micro Nasdaq (`MNQZ6`)**: New York Wall Street Open (13:30 - 13:45 UTC range).
  - **Anti-Overtrading Protocol:** Strictly **1 trade per market per day**. Once executed, the asset locks until the next session.
* **Execution & Position Sizing:**
  - **Contracts:** **6 contracts** per trade.
  - **Stop Loss:** 25.0 pts MNQ (-$300) / 4.0 pts MGC (-$240).
  - **Take Profit:** 50.0 pts MNQ (+$600) / 8.0 pts MGC (+$480) — *1:2 Risk/Reward*.
  - **Breakeven Rule:** Triggered strictly when **+30.0 points** profit is touched; SL moves to exact Entry ($0 risk).
* **Risk Shield:**
  - Max Daily Loss: `-$400.00` (Circuit breaker stops trading for the day).
  - Daily Profit Cap: `+$900.00` (Protects winning days from overtrading).
  - Max Overall Loss: `-$1,500.00`
  - Profit Target: `+$2,500.00`
* **Service:** `tradovate-bot-acc2.service`

---

## 📲 Telegram Unified Control & Telemetry

Both PropFirm accounts and the WEEX bot broadcast telemetry and alerts to the designated Telegram command center:
* **Account 1 Alerts:** Standard header with live position, PnL, and trade executions.
* **Account 2 Alerts:** Distinct `🛡️ [FUNDEDNEXT ACC #2 - ORB MULTI-ASSET]` header.
* **Unified Status:** Typing `/status` or tapping `📊 Status` generates a consolidated real-time overview of **both accounts** simultaneously.

---

## 🛠️ Service Management

```bash
# Check status of PropFirm bots
systemctl status tradovate-bot.service tradovate-bot-acc2.service

# Restart PropFirm bots
systemctl restart tradovate-bot.service
systemctl restart tradovate-bot-acc2.service

# View live trader logs
tail -f /root/tradovate_bot/tradovate_trader.log
tail -f /root/tradovate_bot_acc2/tradovate_trader_acc2.log

# Check WEEX Docker containers
docker ps
```

---

## 🔒 Security Notice
All private credentials, tokens, SQLite databases, and API keys are strictly excluded via [`.gitignore`](file:///root/.gitignore). Example configurations are provided via `config.example.json`.

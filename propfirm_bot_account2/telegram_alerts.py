import json
import logging
import requests

logger = logging.getLogger(__name__)

class TelegramAlertsAcc2:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, text: str, parse_mode: str = "Markdown") -> bool:
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            res = requests.post(self.api_url, json=payload, timeout=8)
            return res.status_code == 200
        except Exception as e:
            logger.error(f"[Acc2] Failed to send Telegram alert: {e}")
            return False

    def notify_startup(self, symbols: str, balance: float, account: str):
        msg = (
            f"🚀 *[FUNDEDNEXT ACC #2 - ORB BOT ONLINE]*\n\n"
            f"👤 *Account:* `{account}`\n"
            f"💰 *Startbalans:* `${balance:,.2f}`\n"
            f"📈 *Markten:* `{symbols}` (Max 5 contracten)\n"
            f"🎯 *Strategie:* 15m Opening Range Breakout (ORB)\n"
            f"⏱️ *Limiet:* Max 1 trade per markt per dag (Zero Overtrading)\n"
            f"🛡️ *FundedNext Shield:*\n"
            f"   • Dagstop: `-$400.00` | Winstcap: `+$900.00`\n"
            f"   • Max Drawdown: `-$1,500.00` | Doel: `+$2,500.00`\n\n"
            f"🟢 *Status:* Actief wachtend op Opening Ranges..."
        )
        self.send(msg)

    def notify_entry(self, side: str, symbol: str, price: float, qty: int, sl: float, tp: float, tag: str, point_val: float = None):
        icon = "🟢" if side.upper() == "BUY" else "🔴"
        action = "LONG" if side.upper() == "BUY" else "SHORT"
        pv = point_val or 2.0
        risk_usd = abs(price - sl) * pv * qty
        reward_usd = abs(tp - price) * pv * qty
        msg = (
            f"{icon} *[ACC #2] Trade Geopend: {action} {symbol}*\n\n"
            f"🏷️ *Setup:* `ORB 15m Breakout` (`{tag}`)\n"
            f"📊 *Positie:* `{qty}` contracten\n"
            f"📍 *Entry Koers:* `{price:.2f}`\n"
            f"🛑 *Stoploss:* `{sl:.2f}` (-${risk_usd:.2f})\n"
            f"🎯 *Take Profit:* `{tp:.2f}` (+${reward_usd:.2f} | 1:2 R:R)\n"
            f"🔒 *Beveiliging:* Automatische Breakeven lock geactiveerd!"
        )
        self.send(msg)

    def notify_exit(self, side: str, symbol: str, entry_price: float, exit_price: float, qty: int, pnl_usd: float, reason: str):
        icon = "🎉" if pnl_usd > 0 else "🛑"
        pts = (exit_price - entry_price) if side.upper() == "BUY" else (entry_price - exit_price)
        pnl_str = f"+${pnl_usd:.2f}" if pnl_usd >= 0 else f"-${abs(pnl_usd):.2f}"
        msg = (
            f"{icon} *[ACC #2] Trade Gesloten: {symbol}*\n\n"
            f"🏷️ *Reden:* `{reason}`\n"
            f"📍 *Exit Koers:* `{exit_price:.2f}` (Entry: `{entry_price:.2f}`)\n"
            f"📏 *Punten:* `{pts:+.2f} pts`\n"
            f"💵 *Gerealiseerde P&L:* `{pnl_str}`\n"
            f"🔒 *Regel:* Trading op {symbol} is voor vandaag afgerond."
        )
        self.send(msg)

    def notify_breakeven(self, symbol: str, new_sl: float, current_profit_pts: float):
        msg = (
            f"🔒 *[ACC #2] Breakeven Beveiliging: {symbol}*\n\n"
            f"Winstdoel halfweg bereikt (+{current_profit_pts:.2f} pts)!\n"
            f"Stoploss verplaatst naar Entry: `{new_sl:.2f}`.\n"
            f"Risico voor deze positie is nu **$0.00 (Free Trade)**."
        )
        self.send(msg)

    def notify_circuit_breaker(self, reason: str, daily_pnl: float):
        msg = (
            f"⚠️ *[ACC #2] CIRCUIT BREAKER GEACTIVEERD*\n\n"
            f"Reden: *{reason}*\n"
            f"Vandaag P&L: `${daily_pnl:,.2f}`\n"
            f"FundedNext limietbescherming actief: Bot stopt met handelen tot morgen."
        )
        self.send(msg)

    def notify_error(self, title: str, details: str):
        msg = (
            f"🚨 *[ACC #2] BOT ALARM / FOUTMELDING*\n\n"
            f"⚠️ *Probleem:* `{title}`\n"
            f"📝 *Details:*\n`{details[:400]}`\n\n"
            f"🔧 *Status:* Bot voert automatische herstart/herverbinding uit."
        )
        self.send(msg)

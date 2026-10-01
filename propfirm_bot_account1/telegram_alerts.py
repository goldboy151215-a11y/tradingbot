import json
import logging
import requests

logger = logging.getLogger(__name__)

class TelegramAlerts:
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
            logger.error(f"Failed to send Telegram alert: {e}")
            return False

    def notify_startup(self, symbol: str, balance: float, account: str):
        msg = (
            f"🛡️ *FundedNext Tradovate Bot Live!*\n\n"
            f"👤 *Account:* `{account}`\n"
            f"💰 *Balance:* `${balance:,.2f}`\n"
            f"📈 *Contract:* `{symbol}` (Micro Nasdaq)\n"
            f"🎯 *Target:* +$3,000 | Max Loss: -$2,000\n"
            f"🛡️ *Shield:* Daily Cap: +$900 | Daily Stop: -$400\n"
            f"⚡ *Strategy:* 5m BB Breakout + EMA Pullback"
        )
        self.send(msg)

    def notify_entry(self, side: str, symbol: str, price: float, qty: int, sl: float, tp: float, tag: str, point_val: float = None):
        icon = "🟢" if side.upper() == "BUY" else "🔴"
        action = "LONG" if side.upper() == "BUY" else "SHORT"
        pv = point_val or 2.0
        risk_usd = abs(price - sl) * pv * qty
        reward_usd = abs(tp - price) * pv * qty
        msg = (
            f"{icon} *Trade Opened: {action} {symbol}*\n\n"
            f"🏷️ *Setup:* `{tag}`\n"
            f"📊 *Quantity:* `{qty}` contracten\n"
            f"📍 *Entry Price:* `{price:.2f}`\n"
            f"🛑 *Stoploss:* `{sl:.2f}` (-${risk_usd:.2f})\n"
            f"🎯 *Take Profit:* `{tp:.2f}` (+${reward_usd:.2f})"
        )
        self.send(msg)

    def notify_exit(self, side: str, symbol: str, entry_price: float, exit_price: float, qty: int, pnl_usd: float, reason: str):
        icon = "🎉" if pnl_usd > 0 else "🛑"
        pts = (exit_price - entry_price) if side.upper() == "BUY" else (entry_price - exit_price)
        msg = (
            f"{icon} *Trade Closed: {symbol}*\n\n"
            f"🏷️ *Reason:* `{reason}`\n"
            f"📦 *Aantal:* `{qty}` contracten\n"
            f"📍 *Exit Price:* `{exit_price:.2f}` (Entry: `{entry_price:.2f}`)\n"
            f"📏 *Points:* `{pts:+.2f} pts`\n"
            f"💵 *P&L:* `+{pnl_usd:.2f}`" if pnl_usd >= 0 else f"💵 *P&L:* `-${abs(pnl_usd):.2f}`"
        )
        self.send(msg)

    def notify_partial_tp(self, symbol: str, exit_price: float, qty_closed: int, remaining_qty: int, pnl_usd: float, pts: float):
        msg = (
            f"💰 *GOUDEN OCHTEND TRADE: TP1 (+{pts:.1f} PTS) BEREIKT!*\n\n"
            f"🎯 *Symbool:* `{symbol}`\n"
            f"📦 *Deelsluiting:* `{qty_closed}` contracten verzilverd (+${pnl_usd:,.2f})\n"
            f"📍 *Exit Koers:* `{exit_price:.2f}`\n"
            f"🚀 *Runner Status:* `{remaining_qty}` contracten lopen door naar Max Dagwinst!\n"
            f"🔒 *Winstbeveiliging:* SL vergrendeld op winst (+35 pts) & Trailing Stop actief."
        )
        self.send(msg)

    def notify_breakeven(self, symbol: str, new_sl: float, current_profit_pts: float):
        msg = (
            f"🔒 *Breakeven Locked: {symbol}*\n\n"
            f"Trade is in profit (+{current_profit_pts:.2f} pts)!\n"
            f"Stoploss moved to Entry: `{new_sl:.2f}`.\n"
            f"Risk is now ZERO. Free trade mode active."
        )
        self.send(msg)

    def notify_trailing_activated(self, symbol: str, new_sl: float, current_profit_pts: float):
        msg = (
            f"🎯 *Dynamic Trailing Stop Geactiveerd: {symbol}*\n\n"
            f"📈 Winstpiek: `+{current_profit_pts:.2f} pts` bereikt!\n"
            f"🛑 Nieuwe SL: `{new_sl:.2f}` (10 pt buffer)\n"
            f"🔒 Winst is vergrendeld en stoploss loopt nu automatisch mee met de markt!"
        )
        self.send(msg)

    def notify_circuit_breaker(self, reason: str, daily_pnl: float):
        msg = (
            f"⚠️ *CIRCUIT BREAKER TRIGGERED*\n\n"
            f"Reason: *{reason}*\n"
            f"Today's P&L: `${daily_pnl:,.2f}`\n"
            f"Bot trading is locked until tomorrow to preserve capital!"
        )
        self.send(msg)

    def notify_error(self, title: str, details: str):
        msg = (
            f"🚨 *TRADOVATE BOT ALARM / VASTLOPER*\n\n"
            f"⚠️ *Probleem:* `{title}`\n"
            f"📝 *Foutdetails:*\n`{details[:400]}`\n\n"
            f"🔧 *Actie:* De bot probeert automatisch opnieuw te verbinden. Controleer de server als dit aanhoudt."
        )
        self.send(msg)

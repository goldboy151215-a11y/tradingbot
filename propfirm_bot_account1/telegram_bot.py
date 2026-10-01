import asyncio
import json
import logging
from datetime import datetime, timezone
import aiohttp

logger = logging.getLogger(__name__)

KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Status"}, {"text": "💰 Balans"}],
        [{"text": "📈 Positie"}, {"text": "🛑 Sluit Positie"}],
        [{"text": "⏸️ Pauzeer"}, {"text": "▶️ Hervat"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

class TelegramBot:
    def __init__(self, config: dict, orders, state):
        self.cfg = config
        self.orders = orders
        self.state = state
        self.token = config["telegram_token"]
        self.chat_id = str(config["telegram_chat_id"])
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.session: aiohttp.ClientSession = None
        self._running = False
        self.poll_task = None

    async def start(self):
        """Starts the Telegram polling task."""
        self._running = True
        self.session = aiohttp.ClientSession()
        self.poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram command bot listener started.")

    async def stop(self):
        self._running = False
        if self.poll_task and not self.poll_task.done():
            self.poll_task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def send(self, text: str, parse_mode: str = "Markdown", with_keyboard: bool = True) -> bool:
        """Sends a message to the authorized chat ID with optional keyboard."""
        try:
            if not self.session or self.session.closed:
                self.session = aiohttp.ClientSession()

            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            if with_keyboard:
                payload["reply_markup"] = KEYBOARD

            url = f"{self.api_url}/sendMessage"
            async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.warning(f"Telegram sendMessage failed ({resp.status}): {err}")
                    return False
                return True
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return False

    # Outbound notifications
    def notify_startup(self, symbol: str, balance: float, account: str):
        target = float(self.cfg.get("profit_target_usd", 2500.0))
        max_loss = float(self.cfg.get("max_overall_loss_usd", 1500.0))
        max_daily = float(self.cfg.get("max_daily_loss_usd", 400.0))
        max_cap = float(self.cfg.get("max_daily_profit_usd", 950.0))
        qty = self.cfg.get("max_contracts", 1)
        pv = self.cfg.get("point_value", 2.0) * qty
        msg = (
            f"🛡️ *FundedNext Tradovate Bot Live!*\n\n"
            f"👤 *Account:* `{account}`\n"
            f"💰 *Startbalans:* `${balance:,.2f}`\n"
            f"📈 *Positiegrootte:* `{qty} contracten {symbol}` (`${pv:.2f}/punt`)\n"
            f"🎯 *Doel:* `+${target:,.2f}` | Max Verlies: `-${max_loss:,.2f}`\n"
            f"🛡️ *Shield:* Dagstop: `-${max_daily:,.2f}` | Winstcap: `+${max_cap:,.2f}`\n"
            f"⚡ *Strategy:* 5m BB Breakout + EMA Pullback\n\n"
            f"💡 *Gebruik de knoppen hieronder om de bot direct te bedienen!*"
        )
        asyncio.create_task(self.send(msg))

    def notify_entry(self, side: str, symbol: str, price: float, qty: int, sl: float, tp: float, tag: str, point_val: float = None):
        icon = "🟢" if side.upper() == "BUY" else "🔴"
        action = "LONG" if side.upper() == "BUY" else "SHORT"
        pv = point_val or float(self.cfg.get("assets", {}).get(symbol, {}).get("point_value", 2.0))
        risk_usd = abs(price - sl) * pv * qty
        reward_usd = abs(tp - price) * pv * qty
        msg = (
            f"{icon} *Trade Geopend: {action} {symbol}*\n\n"
            f"🏷️ *Setup:* `{tag}`\n"
            f"📊 *Aantal:* `{qty}` contracten\n"
            f"📍 *Entry Koers:* `{price:.2f}`\n"
            f"🛑 *Stoploss:* `{sl:.2f}` (-${risk_usd:.2f})\n"
            f"🎯 *Take Profit:* `{tp:.2f}` (+${reward_usd:.2f})"
        )
        asyncio.create_task(self.send(msg))

    def notify_exit(self, side: str, symbol: str, entry_price: float, exit_price: float, qty: int, pnl_usd: float, reason: str):
        icon = "🎉" if pnl_usd > 0 else "🛑"
        pts = (exit_price - entry_price) if side.upper() == "BUY" else (entry_price - exit_price)
        pnl_str = f"+${pnl_usd:.2f}" if pnl_usd >= 0 else f"-${abs(pnl_usd):.2f}"
        msg = (
            f"{icon} *Trade Gesloten: {symbol}*\n\n"
            f"🏷️ *Reden:* `{reason}`\n"
            f"📍 *Exit Koers:* `{exit_price:.2f}` (Entry: `{entry_price:.2f}`)\n"
            f"📏 *Punten:* `{pts:+.2f} pts`\n"
            f"💵 *Gerealiseerde P&L:* `{pnl_str}`"
        )
        asyncio.create_task(self.send(msg))

    def notify_breakeven(self, symbol: str, new_sl: float, current_profit_pts: float):
        msg = (
            f"🔒 *Breakeven Beveiliging: {symbol}*\n\n"
            f"Positie staat ruim in winst (+{current_profit_pts:.2f} pts)!\n"
            f"Stoploss is verplaatst naar Entry: `{new_sl:.2f}`.\n"
            f"Het risico op verlies voor deze trade is nu NUL."
        )
        asyncio.create_task(self.send(msg))

    def notify_trailing_activated(self, symbol: str, new_sl: float, current_profit_pts: float):
        msg = (
            f"🎯 *Dynamic Trailing Stop Geactiveerd: {symbol}*\n\n"
            f"📈 Winstpiek: `+{current_profit_pts:.2f} pts` bereikt!\n"
            f"🛑 Nieuwe SL: `{new_sl:.2f}` (10 pt buffer)\n"
            f"🔒 Winst is vergrendeld en stoploss loopt nu automatisch mee met de markt!"
        )
        asyncio.create_task(self.send(msg))

    def notify_circuit_breaker(self, reason: str, daily_pnl: float):
        msg = (
            f"⚠️ *CIRCUIT BREAKER GEACTIVEERD*\n\n"
            f"Reden: *{reason}*\n"
            f"Vandaag P&L: `${daily_pnl:,.2f}`\n"
            f"Handelen is voor vandaag vergrendeld om kapitaal te beschermen!"
        )
        asyncio.create_task(self.send(msg))

    def notify_error(self, title: str, details: str):
        msg = (
            f"🚨 *TRADOVATE BOT ALARM / VASTLOPER*\n\n"
            f"⚠️ *Probleem:* `{title}`\n"
            f"📝 *Foutdetails:*\n`{details[:400]}`\n\n"
            f"🔧 *Actie:* De bot probeert automatisch opnieuw te verbinden. Controleer de server als dit aanhoudt."
        )
        asyncio.create_task(self.send(msg))

    # Inbound Telegram polling & command dispatcher
    async def _poll_loop(self):
        offset = 0
        while self._running:
            try:
                if not self.session or self.session.closed:
                    self.session = aiohttp.ClientSession()

                url = f"{self.api_url}/getUpdates?offset={offset}&timeout=20"
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        updates = data.get("result", [])
                        for update in updates:
                            offset = max(offset, update.get("update_id", 0) + 1)
                            await self._handle_update(update)
                    elif resp.status == 409:
                        logger.warning("Telegram conflict 409. Another poller active? Waiting 5s...")
                        await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Telegram poll error: {e}")
                await asyncio.sleep(3)

    async def _handle_update(self, update: dict):
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        sender_chat_id = str(chat.get("id"))
        if sender_chat_id != self.chat_id:
            logger.warning(f"Ignored unauthorized message from chat_id {sender_chat_id}")
            return

        text = message.get("text", "").strip()
        if not text:
            return

        logger.info(f"Received Telegram command: {text}")
        cmd = text.lower()

        if cmd in ["/start", "/help", "help"]:
            await self._cmd_help()
        elif cmd in ["/status", "status", "📊 status"]:
            await self._cmd_status()
        elif cmd in ["/balance", "/balans", "balans", "💰 balans"]:
            await self._cmd_balance()
        elif cmd in ["/position", "/positions", "/positie", "positie", "📈 positie"]:
            await self._cmd_position()
        elif cmd in ["/pause", "/pauzeer", "pauzeer", "⏸️ pauzeer"]:
            await self._cmd_pause()
        elif cmd in ["/resume", "/hervat", "hervat", "▶️ hervat"]:
            await self._cmd_resume()
        elif cmd in ["/close", "/sluit", "sluit positie", "🛑 sluit positie"]:
            await self._cmd_close()
        else:
            await self.send(
                f"Onbekend commando: `{text}`\nGebruik de knoppen hieronder of typ `/status`.",
                with_keyboard=True
            )

    async def _cmd_help(self):
        msg = (
            f"🛡️ *FundedNext Tradovate Command Center* 🛡️\n\n"
            f"Je Tradovate bot is **live verbonden** via API met account `{self.cfg['account_spec']}`.\n\n"
            f"*Beschikbare functies:*\n"
            f"📊 *Status* – Volledig overzicht, sessietijd, PnL & bot state\n"
            f"💰 *Balans* – Live saldo opvragen bij Tradovate\n"
            f"📈 *Positie* – Huidige actieve trade met koers & SL/TP\n"
            f"🛑 *Sluit Positie* – Noodsluiting van eventuele open trade\n"
            f"⏸️ *Pauzeer* – Tijdelijk automatisch openen van posities pauzeren\n"
            f"▶️ *Hervat* – Automatisch handelen weer inschakelen"
        )
        await self.send(msg)

    async def _cmd_status(self):
        # Trigger fresh balance sync
        try:
            await self.orders.sync_balance()
        except Exception:
            pass

        now_utc = datetime.now(timezone.utc)
        time_str = now_utc.strftime("%H:%M UTC")

        # Session check
        s_start = self.cfg.get("session_start_utc", "08:00")
        s_end = self.cfg.get("session_end_utc", "21:00")
        cur_hm = now_utc.strftime("%H:%M")
        is_in_session = (s_start <= cur_hm < s_end)
        session_badge = "🟢 OPEN" if is_in_session else f"⏳ GESLOTEN (sessie: {s_start}-{s_end} UTC)"

        # Trading mode
        mode_badge = "⏸️ GEPAUZEERD" if self.state.is_paused else "🟢 ACTIEF (Automatisch)"
        if self.orders.circuit_breaker_active:
            mode_badge = f"🛑 CIRCUIT BREAKER ({self.orders.cb_reason})"

        pnl = self.orders.daily_pnl
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pnl_icon = "🟢" if pnl >= 0 else "🔴"

        # Position info
        pos = self.orders.open_position
        if pos:
            sym = pos.get("symbol", self.cfg["symbol"])
            cur_p = self.state.latest_prices.get(sym, self.state.latest_price or pos["entry_price"])
            pts = (cur_p - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - cur_p)
            pt_val = pos.get("point_value", self.cfg.get("point_value", 2.0))
            unreal_pnl = pts * pt_val * pos["qty"]
            unreal_str = f"+${unreal_pnl:.2f}" if unreal_pnl >= 0 else f"-${abs(unreal_pnl):.2f}"
            if not self.cfg.get("trailing_stop_enabled", False):
                trail_status = "❌ Uitgeschakeld (Target: Volledige Take-Profit)"
            else:
                trail_status = "🟢 Actief (meelopend)" if pos.get("trailing_active") else "⏳ Wacht op trigger"
            pos_text = (
                f"`{pos['side']} {pos['qty']} {sym} @ {pos['entry_price']:.2f}`\n"
                f"   • Huidige koers: `{cur_p:.2f}` ({pts:+.2f} pts)\n"
                f"   • Ongerealiseerd: `{unreal_str}`\n"
                f"   • SL: `{pos['sl_price']:.2f}` | TP: `{pos['tp_price']:.2f}`\n"
                f"   • Trailing Stop: {trail_status}"
            )
        else:
            pos_text = "Geen open positie (1-positie account bescherming actief)"

        candle_lines = []
        assets = self.cfg.get("assets", {self.cfg.get("symbol", "MNQZ6"): {}})
        for sym in assets.keys():
            c_time = self.state.last_candle_times.get(sym)
            if c_time:
                c_rsi = self.state.last_rsi.get(sym, 50.0)
                c_sig = self.state.last_signals.get(sym, "HOLD")
                c_p = self.state.latest_prices.get(sym, 0.0)
                candle_lines.append(f"   • *{sym}* (`{c_p:.2f}`): RSI `{c_rsi:.1f}` | Signaal `{c_sig}`")
        if candle_lines:
            candle_text = "🕯️ *Laatste 5m Analyse:*\n" + "\n".join(candle_lines) + "\n\n"
        else:
            candle_text = ""

        target = float(self.cfg.get("profit_target_usd", 2500.0))
        max_loss = float(self.cfg.get("max_overall_loss_usd", 1500.0))
        start_bal = float(self.cfg.get("starting_balance", 50000.0))
        max_daily = float(self.cfg.get("max_daily_loss_usd", 400.0))
        max_cap = float(self.cfg.get("max_daily_profit_usd", 950.0))
        total_pnl = self.orders.cash_balance - start_bal
        pct_to_target = max(0.0, min(100.0, (total_pnl / target) * 100.0)) if target > 0 else 0.0
        tot_pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        loss_floor = start_bal - max_loss

        ws_status = "🟢 Verbonden" if self.orders.is_open() else "🔴 Verbinding herstellen..."

        msg = (
            f"📊 *Tradovate Bot Status Rapport*\n\n"
            f"🤖 *Bot Mode:* {mode_badge}\n"
            f"📡 *Tradovate API:* {ws_status}\n"
            f"👤 *Account:* `{self.cfg['account_spec']}`\n"
            f"💰 *Account Balans:* `${self.orders.cash_balance:,.2f}`\n"
            f"{pnl_icon} *Vandaag P&L:* `{pnl_str}`\n"
            f"🎯 *FundedNext Doel:* `+${target:,.2f}` (Voortgang: `{tot_pnl_str}` / `{pct_to_target:.1f}%`)\n"
            f"🛡️ *Max Overall Verlies:* `-${max_loss:,.2f}` (Ondergrens: `${loss_floor:,.2f}`)\n\n"
            f"🕒 *Handelssessie:* {session_badge} (`{s_start} - {s_end}`, nu `{time_str}`)\n"
            f"📈 *Contract:* `{self.cfg['symbol']}` ({self.cfg.get('max_contracts', 1)} contracten, ${self.cfg.get('point_value', 2.0) * self.cfg.get('max_contracts', 1):.2f}/pt)" + (f" (Koers: `{self.state.latest_price:.2f}`)" if self.state.latest_price else "") + f"\n\n"
            f"📍 *Open Positie:*\n{pos_text}\n\n"
            f"{candle_text}"
            f"🛡️ *Shield Bescherming:*\n"
            f"• Max Dagverlies Stop: `-${max_daily:,.2f}`\n"
            f"• Dagelijkse Winstcap (40% regel): `+${max_cap:,.2f}`"
        )

        # Append Account #2 snapshot if configured
        try:
            import os
            state_p2 = "/root/tradovate_bot_acc2/state.json"
            cfg_p2 = "/root/tradovate_bot_acc2/config.json"
            if os.path.exists(cfg_p2):
                with open(cfg_p2) as f2:
                    c2 = json.load(f2)
                acc2_user = c2.get("account_spec", "Acc2")
                if "PENDING" in acc2_user:
                    msg += f"\n\n━━━━━━━━━━━━━━━━━━━━\n🛡️ *FundedNext Account #2 (ORB Bot):*\n⏳ *Status:* Wacht op inloggegevens in config.json"
                elif os.path.exists(state_p2):
                    with open(state_p2) as f2:
                        s2 = json.load(f2)
                    a2_bal = s2.get("cash_balance", 50000.0)
                    a2_pnl = s2.get("daily_pnl", 0.0)
                    a2_pos = s2.get("open_position")
                    a2_pos_str = f"{a2_pos['side']} {a2_pos['qty']} {a2_pos['symbol']}" if a2_pos else "Geen open positie"
                    msg += (
                        f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"🛡️ *FundedNext Account #2 (ORB Bot):*\n"
                        f"👤 *Account:* `{acc2_user}`\n"
                        f"💰 *Balans:* `${a2_bal:,.2f}` | Vandaag: `{'+$' if a2_pnl>=0 else '-$'}{abs(a2_pnl):.2f}`\n"
                        f"📍 *Positie:* `{a2_pos_str}`\n"
                        f"🎯 *Strategie:* 15m Opening Range Breakout (MNQ & MGC)"
                    )
        except Exception as e:
            logger.debug(f"Acc2 status read error: {e}")

        await self.send(msg)

    async def _cmd_balance(self):
        try:
            await self.orders.sync_balance()
        except Exception as e:
            logger.warning(f"Error syncing balance in cmd: {e}")

        target = float(self.cfg.get("profit_target_usd", 2500.0))
        max_loss = float(self.cfg.get("max_overall_loss_usd", 1500.0))
        start_bal = float(self.cfg.get("starting_balance", 50000.0))
        total_pnl = self.orders.cash_balance - start_bal
        tot_pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        loss_floor = start_bal - max_loss
        pnl = self.orders.daily_pnl
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        msg = (
            f"💰 *Tradovate Live Balans*\n\n"
            f"👤 *Account:* `{self.cfg['account_spec']}`\n"
            f"💵 *Cash Saldo:* `${self.orders.cash_balance:,.2f}`\n"
            f"📊 *Dagelijkse P&L:* `{pnl_str}`\n"
            f"📈 *Totale P&L:* `{tot_pnl_str}`\n\n"
            f"🎯 *FundedNext Doel:* `+${target:,.2f}`\n"
            f"🛡️ *Max Overall Verlies:* `-${max_loss:,.2f}` (Stop bij `${loss_floor:,.2f}`)\n"
            f"🛡️ *Max Dagverlies:* `-${self.cfg.get('max_daily_loss_usd', 400.0):,.2f}`"
        )
        await self.send(msg)

    async def _cmd_position(self):
        try:
            await self.orders.sync_positions()
            await self.orders.sync_balance()
        except Exception as e:
            logger.warning(f"Error syncing in _cmd_position: {e}")

        pos = self.orders.open_position
        if not pos:
            cur_info = f" (Laatste MNQZ6 koers: `{self.state.latest_price:.2f}`)" if self.state.latest_price else ""
            await self.send(f"📈 *Positie Overzicht:*\n\nEr is momenteel **geen actieve positie**.{cur_info}\nDe bot scant rustig naar de volgende A+ setup.")
            return

        sym = pos.get("symbol", self.cfg["symbol"])
        pt_val = pos.get("point_value", self.cfg.get("point_value", 2.0))
        cur_p = self.state.latest_prices.get(sym, self.state.latest_price or pos["entry_price"])
        pts = (cur_p - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - cur_p)
        unreal_pnl = pts * pt_val * pos["qty"]
        unreal_str = f"+${unreal_pnl:.2f}" if unreal_pnl >= 0 else f"-${abs(unreal_pnl):.2f}"
        icon = "🟢" if unreal_pnl >= 0 else "🔴"

        if not self.cfg.get("trailing_stop_enabled", False):
            trail_status = "❌ Uitgeschakeld (Target: Volledige Take-Profit)"
        else:
            trail_status = "🟢 Actief (meelopend)" if pos.get("trailing_active") else "⏳ Wacht op trigger"
        peak_p = pos.get("peak_price", cur_p)
        peak_pts = (peak_p - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - peak_p)

        msg = (
            f"📈 *Actieve Positie Details*\n\n"
            f"🏷️ *Richting:* `{pos['side']}`\n"
            f"📊 *Aantal:* `{pos['qty']}` contracten `{sym}`\n"
            f"📍 *Entry Koers:* `{pos['entry_price']:.2f}`\n"
            f"💲 *Huidige Koers:* `{cur_p:.2f}`\n"
            f"🏔️ *Hoogste Piek:* `{peak_p:.2f}` (+{peak_pts:.2f} pts)\n"
            f"📏 *Winst / Verlies Punten:* `{pts:+.2f} pts`\n"
            f"{icon} *Ongerealiseerde P&L:* `{unreal_str}`\n\n"
            f"🛑 *Stoploss:* `{pos['sl_price']:.2f}`\n"
            f"🎯 *Take Profit:* `{pos['tp_price']:.2f}`\n"
            f"🎯 *Dynamic Trailing Stop:* {trail_status}\n"
            f"🕒 *Geopend om:* `{pos['open_time']}`\n\n"
            f"💡 *Gebruik '🛑 Sluit Positie' om deze trade direct handmatig te sluiten.*"
        )
        await self.send(msg)

    async def _cmd_pause(self):
        self.state.is_paused = True
        logger.info("Trading paused by Telegram user command.")
        await self.send(
            f"⏸️ *Automatisch Handelen Gepauzeerd*\n\n"
            f"Nieuwe trades worden tijdelijk **geblokkeerd**.\n"
            f"Bestaande open posities blijven gewoon beschermd met SL/TP/Breakeven.\n\n"
            f"Druk op **▶️ Hervat** wanneer je het automatisch handelen weer wilt inschakelen."
        )

    async def _cmd_resume(self):
        self.state.is_paused = False
        logger.info("Trading resumed by Telegram user command.")
        await self.send(
            f"▶️ *Automatisch Handelen Hervat!*\n\n"
            f"De bot scant weer actief naar A+ 5m setups tijdens de sessie (13:00 - 21:00 UTC)."
        )

    async def _cmd_close(self):
        pos = self.orders.open_position
        if not pos:
            await self.send("🛑 Er is momenteel geen openstaande positie om te sluiten.")
            return

        cur_p = self.state.latest_price or pos["entry_price"]
        await self.send(f"🛑 Noodsluiting verzonden naar Tradovate voor `{pos['side']} {pos['qty']} {self.cfg['symbol']}`...")
        await self.orders.close_position(cur_p, "handmatige_telegram_sluiting")
        await self.send("✅ Positie succesvol gesloten tegen marktprijs.")

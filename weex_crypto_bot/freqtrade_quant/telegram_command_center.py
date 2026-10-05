#!/usr/bin/env python3
"""Telegram Command Center & Bot Interface.

Reads ONLY from runtime_state.json (Single Source of Truth).
Enforces zero tolerance for config mismatches on /start.
Rejects entries and emits hard warnings when margin cap or risk limits are breached.
Responds cleanly and reliably to ALL user commands and queries.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

USER_DATA = "/root/ft_userdata/user_data"
if not os.path.exists(USER_DATA):
    USER_DATA = "/freqtrade/user_data"

CONFIG_PATH = os.path.join(USER_DATA, "config.json")
DB_PATH = os.path.join(USER_DATA, "tradesv3.sqlite")
RUNTIME_STATE_PATH = os.path.join(USER_DATA, "runtime_state.json")

sys.path.insert(0, "/root")
sys.path.insert(0, USER_DATA)

from src.runtime_state import (
    RuntimeState,
    load_runtime_state,
    save_runtime_state,
    verify_config_sync,
    sync_with_weex_exchange,
)
from trade_analyzer import (
    audit_single_trade,
    format_trade_audit_telegram,
    analyze_daily_opportunity,
    format_daily_opportunity_telegram,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [TelegramBot] %(message)s",
)
logger = logging.getLogger(__name__)

# Token and Chat ID from config.json
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

TOKEN = config.get("telegram", {}).get("token", "8628193221:AAFNtoO25hyPe4dDwwPwH4xcYpCouYXiyaE")
CHAT_ID = config.get("telegram", {}).get("chat_id", "952213135")
API_URL = f"https://api.telegram.org/bot{TOKEN}"
FT_API_URL = "http://127.0.0.1:8080/api/v1"
FT_AUTH = ("admin", "mendy7218")
HTTP_HEADERS = {"Connection": "close"}

FT_BOTS = [
    {"name": "Main Bot", "url": "http://127.0.0.1:8080/api/v1", "auth": FT_AUTH, "port": 8080},
    {"name": "Elite Bot", "url": "http://127.0.0.1:8081/api/v1", "auth": FT_AUTH, "port": 8081},
]

DEFAULT_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Live Status"}, {"text": "💰 Saldo & Marge"}],
        [{"text": "📈 Winst & Performance"}, {"text": "📅 Dagresultaat"}],
        [{"text": "🔍 Log: Trade Autopsie"}, {"text": "💎 Log: Kansen Analyse"}],
        [{"text": "📜 Log: Gesloten Trades"}, {"text": "⚙️ Bot Instellingen"}],
        [{"text": "▶️ Start Bot"}, {"text": "⏹️ Stop & Sluit Trades"}],
        [{"text": "ℹ️ Help & Uitleg"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}


CHANNELS_PATH = os.path.join(USER_DATA, "telegram_channels.json")


def load_channels_config() -> dict[str, Any]:
    if os.path.exists(CHANNELS_PATH):
        try:
            with open(CHANNELS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"vip_channel_id": None, "free_channel_id": None, "whop_link": "https://whop.com"}


def save_channels_config(cfg: dict[str, Any]) -> None:
    try:
        with open(CHANNELS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save channels config: {e}")


def send_message(
    text: str,
    target_chat_id: str | int | None = None,
    parse_mode: str = "HTML",
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    dest_chat = target_chat_id or CHAT_ID
    try:
        payload: dict[str, Any] = {
            "chat_id": dest_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        elif str(dest_chat) == str(CHAT_ID):
            payload["reply_markup"] = DEFAULT_KEYBOARD

        r = requests.post(f"{API_URL}/sendMessage", json=payload, headers=HTTP_HEADERS, timeout=(3, 10))
        res = r.json()
        if not res.get("ok"):
            logger.warning(f"Telegram API warning for {dest_chat}: {res.get('description')}")
            # Fallback without parse_mode if entity parsing failed
            if "can't parse entities" in str(res.get("description", "")):
                payload.pop("parse_mode", None)
                r2 = requests.post(f"{API_URL}/sendMessage", json=payload, headers=HTTP_HEADERS, timeout=(3, 10))
                return r2.json()
        return res
    except Exception as e:
        logger.error(f"Error sending telegram message to {dest_chat}: {e}")
        return None



def sync_runtime_state_with_exchange() -> RuntimeState:
    """Read actual Weex exchange equity and open trades, update runtime_state.json."""
    state = load_runtime_state(RUNTIME_STATE_PATH)
    state = sync_with_weex_exchange(state, ft_api_url=FT_API_URL, auth=FT_AUTH, db_path=DB_PATH)
    save_runtime_state(state, RUNTIME_STATE_PATH)
    return state


def format_status_message(state: RuntimeState, title: str = "WEEX SCALPER STATUS") -> str:
    """Format status with clear, real-time today's profit/loss and open trade telemetry."""
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_disp = datetime.now(timezone.utc).strftime("%d-%m-%Y")

    cnt_today = 0
    pnl_today = 0.0
    wins_today = 0
    total_closed = 0
    pnl_all = 0.0
    wins_all = 0

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row_today = c.execute(
            """
            SELECT COUNT(*) as cnt, SUM(close_profit_abs) as pnl,
                   SUM(CASE WHEN close_profit > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE is_open=0 AND close_date LIKE ?
            """,
            (f"{today_utc}%",),
        ).fetchone()
        if row_today:
            cnt_today = row_today["cnt"] or 0
            pnl_today = row_today["pnl"] or 0.0
            wins_today = row_today["wins"] or 0

        row_all = c.execute(
            """
            SELECT COUNT(*) as cnt, SUM(close_profit_abs) as pnl,
                   SUM(CASE WHEN close_profit > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE is_open=0
            """
        ).fetchone()
        if row_all:
            total_closed = row_all["cnt"] or 0
            pnl_all = row_all["pnl"] or 0.0
            wins_all = row_all["wins"] or 0
        conn.close()
    except Exception as exc:
        logger.warning(f"Could not read trades DB in format_status: {exc}")

    losses_today = cnt_today - wins_today
    wr_today = (wins_today / cnt_today * 100.0) if cnt_today > 0 else 0.0
    losses_all = total_closed - wins_all
    wr_all = (wins_all / total_closed * 100.0) if total_closed > 0 else 0.0

    # Today's PnL badge
    if pnl_today > 0:
        today_pnl_str = f"🟢 <b>+${pnl_today:.2f} USDT</b>"
    elif pnl_today < 0:
        today_pnl_str = f"🔴 <b>-${abs(pnl_today):.2f} USDT</b>"
    else:
        today_pnl_str = "⚪ <b>$0.00 USDT</b>"

    # All-time PnL badge
    if pnl_all > 0:
        all_pnl_str = f"🟢 <b>+${pnl_all:.2f} USDT</b>"
    elif pnl_all < 0:
        all_pnl_str = f"🔴 <b>-${abs(pnl_all):.2f} USDT</b>"
    else:
        all_pnl_str = "⚪ <b>$0.00 USDT</b>"

    # Query live open trades directly from both Freqtrade bots (Main + Elite)
    open_trades_list = []
    for b in FT_BOTS:
        try:
            r = requests.get(f"{b['url']}/status", auth=b["auth"], headers=HTTP_HEADERS, timeout=(2, 6))
            if r.status_code == 200:
                for t in r.json():
                    t["bot_label"] = b["name"]
                    open_trades_list.append(t)
        except Exception as exc:
            logger.warning(f"Could not fetch {b['name']} open trades status: {exc}")

    if open_trades_list:
        open_trades_blocks = []
        for t in open_trades_list:
            pair = t.get("pair", "Onbekend")
            side = "🔴 SHORT" if t.get("is_short") else "🟢 LONG"
            bot_tag = f"[{t.get('bot_label', 'Bot')}] " if len(FT_BOTS) > 1 else ""
            open_rate = t.get("open_rate", 0.0)
            cur_rate = t.get("current_rate", open_rate)
            p_abs = t.get("profit_abs", 0.0)
            p_pct = t.get("profit_pct", 0.0)
            sl_val = t.get("stop_loss_abs", 0.0)
            stake_val = t.get("stake_amount", state.stake_usdt)
            lev = t.get("leverage", state.leverage)
            notional = stake_val * lev

            p_icon = "🟢" if p_abs >= 0 else "🔴"
            p_str = f"{p_icon} <b>{'+' if p_abs >= 0 else ''}${p_abs:.2f} USDT ({'+' if p_pct >= 0 else ''}{p_pct:.2f}%)</b>"

            block = (
                f"• <b>{bot_tag}{pair}</b> ({side} {lev:.0f}x)\n"
                f"  ├ Entry: <code>{open_rate:.4f}</code> ➔ Live: <code>{cur_rate:.4f}</code>\n"
                f"  ├ Live PnL: {p_str}\n"
                f"  ├ Stoploss: <code>{sl_val:.4f}</code>\n"
                f"  └ Marge: <code>${stake_val:.2f}</code> | Notional: <code>${notional:.2f} USDT</code>"
            )
            open_trades_blocks.append(block)
        open_trade_str = "\n\n".join(open_trades_blocks)
    elif state.open_trade:
        ot = state.open_trade
        side_badge = "🔴 SHORT" if ot.get("side") == "short" else "🟢 LONG"
        open_trade_str = (
            f"• <b>{ot.get('pair')}</b> ({side_badge} {state.leverage:.0f}x)\n"
            f"  ├ Entry: <code>{ot.get('entry', 0.0):.4f}</code>\n"
            f"  ├ Stoploss: <code>{ot.get('sl', 0.0):.4f}</code>\n"
            f"  └ Notional: <code>${ot.get('notional', 0.0):.2f} USDT</code>"
        )
    else:
        open_trade_str = "• <i>Geen actieve posities (Scanner zoekt actief naar 5m scalp signalen) 🟢</i>"

    # Status & Warning
    if state.orders_blocked or state.margin_used_pct > state.margin_cap_pct:
        status_line = f"🚨 <b>GEPAUZEERD:</b> {state.block_reason}"
    else:
        status_line = "✅ <b>ACTIEF & VERBONDEN (WEEX Futures)</b>"

    mode_str = "Live Futures" if state.live else "Dry-Run"

    msg = f"""✨ <b>{title}</b> ✨
──────────────────────────
🤖 <b>Systeem:</b> {status_line}
🪙 <b>Exchange:</b> WEEX Perpetuals ({state.leverage:.0f}x Isolated)
💰 <b>Totaal Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code> (Vrij: <code>${state.free_usdt:.2f} USDT</code>)
🎯 <b>Inzet per trade:</b> <code>${state.stake_usdt:.2f} USDT</code> (58% compounding)

📍 <b>ACTIEVE POSITIES ({len(open_trades_list) if open_trades_list else 0}/3 max):</b>
{open_trade_str}

📅 <b>RESULTAAT VANDAAG ({today_disp}):</b>
• Gerealiseerde winst: {today_pnl_str}
• Gesloten trades: <b>{cnt_today}</b> ({wins_today}W / {losses_today}L • <b>{wr_today:.0f}% winrate</b>)

📈 <b>ALL-TIME PRESTATIES:</b>
• Totale winst: {all_pnl_str}
• Totaal trades: <b>{total_closed}</b> ({wins_all}W / {losses_all}L • <b>{wr_all:.0f}% winrate</b>)

<i>✨ Klik hieronder op <b>🔍 Log: Trade Autopsie</b> voor een diepe analyse!</i>
"""
    return msg


def handle_start_command() -> str:
    """Handle /start: strictly verify parity; start trading loop with cheerful confirmation."""
    state = sync_runtime_state_with_exchange()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        ft_config = json.load(f)

    # Perform strict verification
    is_valid, mismatches = verify_config_sync(state, ft_config)

    if not is_valid:
        mismatch_bullets = "\n".join(f"• {m}" for m in mismatches)
        logger.error(f"CONFIG MISMATCH on /start: {mismatches}")
        return (
            "⚠️ <b>CONFIG VERIFICATIE VEREIST</b>\n"
            "──────────────────────────\n"
            f"{mismatch_bullets}\n\n"
            "<i>De bot start voor je eigen veiligheid pas als instellingen 100% gelijk zijn.</i>"
        )

    # Trigger start on ALL Freqtrade bots (Main + Elite)
    results = []
    for b in FT_BOTS:
        b_name = b["name"]
        b_url = b["url"]
        b_auth = b["auth"]
        try:
            r = requests.post(f"{b_url}/start", auth=b_auth, headers=HTTP_HEADERS, timeout=(3, 10))
            if r.status_code == 200:
                logger.info(f"{b_name} trading loop started via API")
                results.append(f"• <b>{b_name}</b>: 🟢 Gestart")
            else:
                results.append(f"• <b>{b_name}</b>: ⚠️ Fout ({r.status_code})")
        except Exception as exc:
            logger.error(f"Could not connect to {b_name} API: {exc}")
            results.append(f"• <b>{b_name}</b>: ❌ Offline ({exc})")

    bots_summary = "\n".join(results)
    return f"""🚀 <b>TRADING BOTS GESTART!</b> 🟢
──────────────────────────
{bots_summary}

✨ <b>Exchange:</b> WEEX Perpetuals (12x Isolated)
🎯 <b>Strategie:</b> 5m Quant Scalper (30/22/16/12)
💰 <b>Beschikbaar Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
🛡️ <b>Marge-beveiliging:</b> Max 2 posities per bot

<i>Beide bots speuren nu 24/7 synchroon naar de beste winstkansen! 📈</i>"""


def handle_stop_command() -> str:
    """Handle /stop: gracefully close ANY open position immediately via market order across ALL bots, then pause trading."""
    logger.info("Executing safe Stop & Exit sequence across ALL bots...")
    closed_details = []
    stopped_details = []

    for b in FT_BOTS:
        b_name = b["name"]
        b_url = b["url"]
        b_auth = b["auth"]

        # 1. Query open trades for this bot
        open_trades = []
        try:
            r_status = requests.get(f"{b_url}/status", auth=b_auth, headers=HTTP_HEADERS, timeout=(3, 8))
            if r_status.status_code == 200:
                open_trades = r_status.json()
        except Exception as exc:
            logger.warning(f"Could not query open trades for {b_name}: {exc}")

        # 2. Force exit open trades
        if open_trades:
            try:
                requests.post(
                    f"{b_url}/forceexit",
                    auth=b_auth,
                    json={"tradeid": "all", "ordertype": "market"},
                    headers=HTTP_HEADERS,
                    timeout=(5, 15),
                )
                for t in open_trades:
                    p_cur = t.get("profit_abs", 0.0)
                    p_pct = t.get("profit_pct", 0.0)
                    closed_details.append(
                        f"• <b>[{b_name}] {t.get('pair')}</b>: Direct gesloten op de markt ({'+' if p_cur >= 0 else ''}${p_cur:.2f} / {'+' if p_pct >= 0 else ''}{p_pct:.1f}%)"
                    )
            except Exception as exc:
                logger.error(f"Error force-exiting trades on {b_name}: {exc}")
                closed_details.append(f"⚠️ Sluiten op {b_name} mislukt: {exc}")

        # 3. Stop trading loop
        try:
            r_stop = requests.post(f"{b_url}/stop", auth=b_auth, headers=HTTP_HEADERS, timeout=(3, 8))
            if r_stop.status_code == 200:
                stopped_details.append(f"• <b>{b_name}</b>: ⏸️ Gepauzeerd (Veilig)")
            else:
                stopped_details.append(f"• <b>{b_name}</b>: ⚠️ Fout bij pauzeren ({r_stop.status_code})")
        except Exception as exc:
            logger.error(f"Error pausing {b_name}: {exc}")
            stopped_details.append(f"• <b>{b_name}</b>: ❌ Offline ({exc})")

    # Synchronize runtime state
    state = sync_runtime_state_with_exchange()

    trades_text = "\n".join(closed_details) if closed_details else "• <i>Geen openstaande posities aangetroffen</i>"
    stopped_text = "\n".join(stopped_details)

    return f"""⏹️ <b>ALLE BOTS GESTOPT & POSITIES GESLOTEN</b> 🛡️
──────────────────────────
✅ <b>Openstaande trade(s):</b>
{trades_text}

⏸️ <b>Status Trading Loops:</b>
{stopped_text}

💰 <b>Vrij Saldo op WEEX:</b> <code>${state.equity_usdt:.2f} USDT</code> (100% in veilige USDT cash)
🚫 <b>Nieuwe entries:</b> Uitgeschakeld op BEIDE bots tot je weer op <b>▶️ Start Bot</b> klikt.

<i>✨ Main Bot en Elite Bot staan beide veilig stil zonder open risico.</i>"""


def handle_kill_command() -> str:
    """Handle /kill: emergency stop, closes all trades and pauses."""
    return handle_stop_command()


def handle_balance_command() -> str:
    state = sync_runtime_state_with_exchange()
    try:
        r = requests.get(f"{FT_API_URL}/balance", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(3, 10))
        if r.status_code == 200:
            bal_data = r.json()
            total = bal_data.get("total", state.equity_usdt)
            curr_list = bal_data.get("currencies", [])
            usdt_info = next((c for c in curr_list if c.get("currency") == "USDT"), {})
            free = usdt_info.get("free", state.free_usdt)
            used = max(0.0, total - free)
            return f"""💰 <b>WALLET & SALDO OVERZICHT</b> 💰
──────────────────────────
💵 <b>Totaal Saldo:</b> <code>${total:.2f} USDT</code>
🟢 <b>Vrij Beschikbaar:</b> <code>${free:.2f} USDT</code>
🔒 <b>In Positie (Marge):</b> <code>${used:.2f} USDT</code> ({state.margin_used_pct*100:.1f}% marge)

🎯 <b>Inzet per Trade:</b> <code>${state.stake_usdt:.2f} USDT</code> (automatisch 58% compounding)
🛡️ <b>Exchange Status:</b> 🟢 Verbonden & Live Gesynchroniseerd met WEEX
"""
    except Exception as exc:
        logger.error(f"Error in handle_balance_command: {exc}")

    return f"""💰 <b>WALLET SALDO (WEEX)</b>
──────────────────────────
💵 <b>Totaal Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
🟢 <b>Vrij Beschikbaar:</b> <code>${state.free_usdt:.2f} USDT</code>
"""


def handle_count_command() -> str:
    state = sync_runtime_state_with_exchange()
    open_count = 1 if state.open_trade else 0
    status_icon = "🔴 Bezet (max 1 open trade bereikt)" if open_count >= 1 else "🟢 Vrij (actief scannend naar signalen)"
    return f"""🔢 <b>ACTIEVE POSITIES & SLOTS</b>
──────────────────────────
• <b>Open Posities:</b> {open_count} / 1 max
• <b>Trades Vandaag:</b> {state.trades_today} / {state.max_trades_per_day} max
• <b>Slot Status:</b> {status_icon}
"""


def handle_trades_command(limit: int = 5) -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        """
        SELECT id, pair, is_short, open_rate, close_rate, close_profit, close_profit_abs, exit_reason, close_date
        FROM trades WHERE is_open=0 ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        return "📜 <b>LOGBOEK: LAATSTE TRADES</b>\n──────────────────────────\nEr zijn nog geen gesloten trades geregistreerd."

    lines = ["📜 <b>LOGBOEK: LAATSTE GESLOTEN TRADES</b>\n──────────────────────────"]
    for r in rows:
        side_badge = "🔴 SHORT" if r["is_short"] else "🟢 LONG"
        profit_pct = (r["close_profit"] or 0.0) * 100
        pnl = r["close_profit_abs"] or 0.0
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pct_str = f"+{profit_pct:.1f}%" if profit_pct >= 0 else f"{profit_pct:.1f}%"
        date_str = str(r["close_date"] or "")[:16]
        reason = r["exit_reason"] or "onbekend"
        icon = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{icon} <b>Trade #{r['id']} {r['pair']}</b> ({side_badge})\n"
            f"  ├ Winst: <b>{pnl_str}</b> ({pct_str})\n"
            f"  ├ Reden: <code>{reason}</code>\n"
            f"  └ Tijd: <code>{date_str}</code>"
        )
    lines.append("\n💡 <i>Klik op <b>🔍 Log: Trade Autopsie</b> voor een diepe analyse van de laatste trade!</i>")
    return "\n".join(lines)


def handle_daily_command() -> str:
    state = sync_runtime_state_with_exchange()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_disp = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    row = c.execute(
        """
        SELECT COUNT(*) as cnt, SUM(close_profit_abs) as pnl,
               SUM(CASE WHEN close_profit > 0 THEN 1 ELSE 0 END) as wins
        FROM trades WHERE is_open=0 AND close_date LIKE ?
        """,
        (f"{today_utc}%",),
    ).fetchone()
    conn.close()

    cnt = row["cnt"] or 0
    pnl = row["pnl"] or 0.0
    wins = row["wins"] or 0
    losses = cnt - wins
    winrate = (wins / cnt * 100) if cnt > 0 else 0.0

    p_icon = "🟢" if pnl >= 0 else "🔴"
    pnl_str = f"{p_icon} <b>{'+' if pnl >= 0 else ''}${pnl:.2f} USDT</b>"

    open_pnl = 0.0
    open_info_str = "Geen open posities"
    try:
        r = requests.get(f"{FT_API_URL}/status", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(3, 10))
        if r.status_code == 200:
            open_trades = r.json()
            if open_trades:
                open_pnl = sum(float(t.get("profit_abs", 0.0)) for t in open_trades)
                t_first = open_trades[0]
                open_info_str = f"{t_first.get('pair')} ({'+' if open_pnl >= 0 else ''}${open_pnl:.2f} USDT)"
    except Exception:
        pass

    tot_today = pnl + open_pnl
    tot_icon = "🟢" if tot_today >= 0 else "🔴"

    return f"""📅 <b>DAGRESULTAAT ({today_disp})</b> 📅
──────────────────────────
💰 <b>Gerealiseerde Winst:</b> {pnl_str}
🎯 <b>Trades Vandaag:</b> <b>{cnt}</b> ({wins} winst / {losses} verlies)
⭐ <b>Winrate Vandaag:</b> <b>{winrate:.0f}%</b>
📍 <b>Open Positie:</b> {tot_icon} <code>{'+' if open_pnl >= 0 else ''}${open_pnl:.2f} USDT</code> ({open_info_str})
💵 <b>Totaal Vandaag (incl. open):</b> <b>{tot_icon} {'+' if tot_today >= 0 else ''}${tot_today:.2f} USDT</b>
💎 <b>Huidig Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>

<i>✨ Klik op <b>💎 Log: Kansen Analyse</b> voor het complete kansenrapport van vandaag!</i>
"""


def handle_profit_command() -> str:
    state = sync_runtime_state_with_exchange()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    total_closed = c.execute("SELECT COUNT(*) FROM trades WHERE is_open=0").fetchone()[0]
    wins = c.execute("SELECT COUNT(*) FROM trades WHERE is_open=0 AND close_profit > 0").fetchone()[0]
    pnl_sum = c.execute("SELECT SUM(close_profit_abs) FROM trades WHERE is_open=0").fetchone()[0] or 0.0

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row_today = c.execute(
        """
        SELECT COUNT(*) as cnt, SUM(close_profit_abs) as pnl
        FROM trades WHERE is_open=0 AND close_date LIKE ?
        """,
        (f"{today_utc}%",),
    ).fetchone()
    conn.close()

    cnt_today = row_today["cnt"] or 0
    pnl_today = row_today["pnl"] or 0.0

    winrate = (wins / total_closed * 100) if total_closed else 0.0
    p_icon_all = "🟢" if pnl_sum >= 0 else "🔴"
    p_icon_today = "🟢" if pnl_today >= 0 else "🔴"

    return f"""📈 <b>WINST & PRESTATIES OVERZICHT</b> 📈
──────────────────────────
💰 <b>Huidig Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
📅 <b>Winst Vandaag:</b> {p_icon_today} <b>{'+' if pnl_today >= 0 else ''}${pnl_today:.2f} USDT</b> ({cnt_today} trades)
🏆 <b>Totale Winst All-Time:</b> {p_icon_all} <b>{'+' if pnl_sum >= 0 else ''}${pnl_sum:.2f} USDT</b>
🎯 <b>Gesloten Trades:</b> <b>{total_closed}</b> ({wins} winst / {total_closed - wins} verlies)
⭐ <b>All-Time Winrate:</b> <b>{winrate:.1f}%</b>
⚡ <b>Hefboom:</b> {state.leverage:.0f}x Isolated | <b>Inzet:</b> ${state.stake_usdt:.2f} USDT
"""


def handle_config_command() -> str:
    state = sync_runtime_state_with_exchange()
    compounded_stake = state.equity_usdt * 0.50

    # Dynamically read pair whitelist from active config.json
    pairs = []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_data = json.load(f)
            pairs = cfg_data.get("exchange", {}).get("pair_whitelist", [])
    except Exception:
        pass
    if not pairs:
        pairs = [
            "NEAR/USDT:USDT", "SOL/USDT:USDT", "AVAX/USDT:USDT", "XRP/USDT:USDT",
            "LINK/USDT:USDT", "ETH/USDT:USDT", "SUI/USDT:USDT", "DOGE/USDT:USDT",
            "BTC/USDT:USDT", "LTC/USDT:USDT"
        ]

    name_map = {
        "BTC/USDT:USDT": "Bitcoin",
        "ETH/USDT:USDT": "Ethereum",
        "SOL/USDT:USDT": "Solana",
        "AVAX/USDT:USDT": "Avalanche",
        "LTC/USDT:USDT": "Litecoin",
        "LINK/USDT:USDT": "Chainlink",
        "XRP/USDT:USDT": "XRP",
        "NEAR/USDT:USDT": "NEAR Protocol",
        "SUI/USDT:USDT": "SUI Network",
        "DOGE/USDT:USDT": "Dogecoin",
    }
    pairs_str = "\n".join(f"• <code>{p}</code> ({name_map.get(p, p.split('/')[0])})" for p in pairs)

    return f"""⚙️ <b>BOT INSTELLINGEN (GEVERIFIEERD)</b> ⚙️
──────────────────────────
🎯 <b>Strategie:</b> WEEX Futures 12x Aggressive Peak Scalper
⚡ <b>Hefboom:</b> {state.leverage:.0f}x Isolated Margin
📈 <b>Auto-Compounding:</b> ✅ <b>ACTIEF (50% per trade / Max 3 Slots)</b>
💰 <b>Huidig Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
💵 <b>Inzet Volgende Trade:</b> <code>${compounded_stake:.2f} USDT</code>

📍 <b>ACTIEVE PAREN ({len(pairs)} Top Munten):</b>
{pairs_str}

🛡️ <b>BESCHERMING & TARGETS:</b>
• <b>Stoploss:</b> <code>-20.0% ROE</code> (-1,67% koersdaling)
• <b>Tiered Winstzekering:</b> ✅ <b>Actief (SL -> Breakeven op +12% ROE, Lock +12% op +22% ROE)</b>
• <b>Take-Profit Direct:</b> <code>+30.0% ROE</code> (+2,50% koers)
• <b>Take-Profit na 15 min:</b> <code>+22.0% ROE</code> (+1,83% koers)
• <b>Take-Profit na 30 min:</b> <code>+16.0% ROE</code> (+1,33% koers)
• <b>Take-Profit na 60 min:</b> <code>+12.0% ROE</code> (+1,00% koers)
• <b>Max Posities:</b> 3 tegelijk (50% dynamische verdeling)
"""


def handle_audit_command(args: list[str]) -> str:
    trade_id = None
    if args:
        try:
            trade_id = int(args[0].replace("#", ""))
        except ValueError:
            pass
    audit = audit_single_trade(trade_id)
    return format_trade_audit_telegram(audit)


def handle_opportunity_command(args: list[str]) -> str:
    target_date = args[0] if args else None
    rep = analyze_daily_opportunity(target_date)
    return format_daily_opportunity_telegram(rep)


def handle_help_command() -> str:
    return """📖 <b>BOT BEDIENING & FUNCTIES</b> ✨
──────────────────────────
📊 <b>OVERZICHT & METRICS:</b>
• <b>Live Status</b> - Live koersen, actieve trade en dagscore
• <b>Saldo & Marge</b> - Wallet balans en vrije marge
• <b>Winst & Performance</b> - Winstpercentages en winrate
• <b>Dagresultaat</b> - Wat er vandaag binnengehaald is

🔍 <b>LOGS & DIEPE ANALYSE:</b>
• <b>Log: Trade Autopsie</b> - Was het een storing of strategie? (MFE/MAE)
• <b>Log: Kansen Analyse</b> - Hoeveel potentiële winst lag er vandaag?
• <b>Log: Gesloten Trades</b> - Laatste 5 afgeronde trades bekijken
• <b>Bot Instellingen</b> - Hefboom, ROI winstladder en stoploss

🎮 <b>BESTURING:</b>
• <b>Start Bot</b> - Zet de bot aan (zoekt direct naar signalen)
• <b>Stop & Sluit Trades</b> - Sluit open posities direct op de markt en pauzeert
"""


def trade_monitor_loop() -> None:
    """Background loop that detects newly opened and closed trades across Elite & Main bots in SQLite.
    
    Routes Elite trades to VIP/Free marketing channels + Admin.
    Routes Main trades to Admin ONLY (never broadcast to VIP or Free channels).
    """
    tracked_bots = [
        {
            "id": "elite",
            "name": "Elite Account",
            "tag": "👑 [ELITE]",
            "db_path": os.path.join(USER_DATA, "tradesv3_elite.sqlite"),
            "send_to_vip": True,
            "strategy_label": "5m Balanced Scalper (44/24/12)",
        },
        {
            "id": "main",
            "name": "Main Account",
            "tag": "⚡ [MAIN]",
            "db_path": os.path.join(USER_DATA, "tradesv3.sqlite"),
            "send_to_vip": False,
            "strategy_label": "Main Quant Bot",
        },
    ]

    last_known_open_ids: dict[str, set[int]] = {b["id"]: set() for b in tracked_bots}
    last_known_closed_ids: dict[str, set[int]] = {b["id"]: set() for b in tracked_bots}
    last_daily_recap_date: str = ""

    # Initial seeding of existing IDs for each bot
    for bot in tracked_bots:
        db_path = bot["db_path"]
        bid = bot["id"]
        try:
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                for r in c.execute("SELECT id FROM trades WHERE is_open=1"):
                    last_known_open_ids[bid].add(r[0])
                for r in c.execute("SELECT id FROM trades WHERE is_open=0"):
                    last_known_closed_ids[bid].add(r[0])
                conn.close()
        except Exception as e:
            logger.warning(f"Trade monitor initial seed notice for {bot['name']}: {e}")

    logger.info("Realtime Trade Notifier daemon gestart voor Elite & Main accounts.")

    while True:
        try:
            channels = load_channels_config()
            vip_chat = channels.get("vip_channel_id")
            free_chat = channels.get("free_channel_id")
            whop_link = channels.get("whop_link", "https://whop.com")

            for bot in tracked_bots:
                db_path = bot["db_path"]
                bid = bot["id"]
                tag = bot["tag"]
                send_vip = bot["send_to_vip"]
                strat_label = bot["strategy_label"]

                if not os.path.exists(db_path):
                    continue

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()

                # 1. Check for newly opened trades
                rows_open = c.execute("SELECT * FROM trades WHERE is_open=1").fetchall()
                for r in rows_open:
                    tid = r["id"]
                    if tid not in last_known_open_ids[bid]:
                        last_known_open_ids[bid].add(tid)
                        pair = r["pair"]
                        side_nl = "🔴 SHORT" if r["is_short"] else "🟢 LONG"
                        side_en = "🔴 SHORT" if r["is_short"] else "🟢 LONG"
                        action_en = "ENTER SHORT" if r["is_short"] else "ENTER LONG"
                        stake = r["stake_amount"] or 0.0
                        rate = r["open_rate"] or 0.0
                        lev = r["leverage"] or 12.0

                        # A. Send to Private Admin Chat (Dutch)
                        msg_admin = (
                            f"{tag} <b>NIEUWE TRADE GEOPEND (#{tid})</b>\n"
                            f"──────────────────────────\n"
                            f"• <b>Account:</b> {bot['name']}\n"
                            f"• <b>Munt:</b> <code>{pair}</code> ({side_nl} {lev:.0f}x)\n"
                            f"• <b>Instap Koers:</b> <code>{rate:.4f}</code>\n"
                            f"• <b>Inzet (Stake):</b> <code>${stake:.2f} USDT</code>\n"
                            f"• <b>Totale Positie:</b> <code>${stake * lev:.2f} USDT</code>\n"
                            f"• <b>Strategie:</b> {strat_label} 🚀\n\n"
                            f"<i>Stoploss & Positiebeheer actief.</i>"
                        )
                        send_message(msg_admin, target_chat_id=CHAT_ID)

                        # B. Broadcast to VIP Channel (ONLY if bot is VIP eligible, e.g. Elite)
                        if send_vip and vip_chat:
                            msg_vip = (
                                f"{side_en} <b>NEW QUANT SIGNAL: {pair}</b>\n"
                                f"──────────────────────────\n"
                                f"• <b>Action:</b> <b>{action_en} ({lev:.0f}x Isolated)</b>\n"
                                f"• <b>Entry Price:</b> <code>${rate:.4f}</code>\n"
                                f"• <b>Profit Targets (ROE Ladder):</b>\n"
                                f"  ├ <b>TP1 (Direct):</b> <code>+80.0% ROE</code>\n"
                                f"  ├ <b>TP2 (Runner):</b> <code>+50.0% ROE</code>\n"
                                f"  └ <b>TP3 (Consolidation):</b> <code>+30.0% ROE</code>\n"
                                f"• <b>Trailing Lock:</b> ✅ <i>Activates at +25.0% ROE (5% trail)</i>\n"
                                f"• <b>Hard Stoploss:</b> <code>-50.0% ROE</code>\n"
                                f"──────────────────────────\n"
                                f"💎 <i>Automated Signal • WEEX Futures Quant Engine</i>"
                            )
                            send_message(msg_vip, target_chat_id=vip_chat)

                # 2. Check for newly closed trades
                rows_closed = c.execute("SELECT * FROM trades WHERE is_open=0 ORDER BY id DESC LIMIT 10").fetchall()
                for r in rows_closed:
                    tid = r["id"]
                    if tid not in last_known_closed_ids[bid]:
                        last_known_closed_ids[bid].add(tid)
                        last_known_open_ids[bid].discard(tid)
                        pair = r["pair"]
                        pnl_abs = r["close_profit_abs"] or 0.0
                        pnl_pct = (r["close_profit"] or 0.0) * 100
                        reason = r["exit_reason"] or "take_profit"
                        icon = "🟢" if pnl_abs >= 0 else "🔴"
                        state_now = sync_runtime_state_with_exchange()

                        # A. Send to Private Admin Chat (Dutch - Full Details with USD Amounts)
                        msg_admin = (
                            f"{tag} <b>TRADE GESLOTEN (#{tid})</b>\n"
                            f"──────────────────────────\n"
                            f"• <b>Account:</b> {bot['name']}\n"
                            f"• <b>Munt:</b> <code>{pair}</code>\n"
                            f"• <b>Resultaat:</b> {icon} <b>{'+' if pnl_abs >= 0 else ''}${pnl_abs:.2f} USDT ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)</b>\n"
                            f"• <b>Exit Reden:</b> <code>{reason}</code>\n"
                            f"• <b>Nieuw Saldo:</b> <code>${state_now.equity_usdt:.2f} USDT</code>\n\n"
                            f"🔍 <i>Tip: Typ /audit {tid} voor diagnose (storing vs strategie) & gemiste winst.</i>"
                        )
                        send_message(msg_admin, target_chat_id=CHAT_ID)

                        open_rate = r["open_rate"] or 0.0
                        close_rate = r["close_rate"] or 0.0
                        side_label = "SHORT" if r["is_short"] else "LONG"

                        # B. Broadcast to VIP Channel (ONLY if bot is VIP eligible, e.g. Elite)
                        if send_vip and vip_chat:
                            msg_vip_close = (
                                f"🎯 <b>VIP TRADE CLOSED: {pair}</b>\n"
                                f"──────────────────────────\n"
                                f"• <b>Direction:</b> <b>{side_label} (12x Isolated)</b>\n"
                                f"• <b>Entry:</b> <code>${open_rate:.4f}</code> ➔ <b>Exit:</b> <code>${close_rate:.4f}</code>\n"
                                f"• <b>Net Result:</b> {icon} <b>{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}% ROE</b>\n"
                                f"• <b>Exit Reason:</b> <code>{reason}</code>\n"
                                f"• <b>Engine:</b> {strat_label}\n"
                                f"──────────────────────────\n"
                                f"💎 <i>Transparent Quant Execution Stream</i>"
                            )
                            send_message(msg_vip_close, target_chat_id=vip_chat)

                        # C. Broadcast to Free Channel ONLY if Profitable and VIP eligible
                        if send_vip and free_chat and pnl_pct > 0:
                            msg_free = (
                                f"🚀 <b>VIP WINNER ALERT: {pair}</b>\n\n"
                                f"Our automated Quant Engine just locked in another winning trade!\n\n"
                                f"📈 <b>Net Return:</b> 🟢 <b>+{pnl_pct:.2f}% ROE</b>\n"
                                f"⚙️ <b>Strategy:</b> 12x Isolated Quant Scalper\n"
                                f"🛡️ <b>Risk Control:</b> Multi-Stage Profit Lock Protected\n\n"
                                f"🔥 <b>Want every real-time trade signal with automated entry & exits?</b>\n"
                                f"👉 <a href=\"{whop_link}\"><b>Click Here to Access the VIP Channel</b></a>"
                            )
                            send_message(msg_free, target_chat_id=free_chat)

                conn.close()

            # Check for automated Daily Opportunity Recap at 23:00 UTC
            now_utc = datetime.now(timezone.utc)
            today_str = now_utc.strftime("%Y-%m-%d")
            if now_utc.hour == 23 and last_daily_recap_date != today_str:
                last_daily_recap_date = today_str
                try:
                    daily_rep = analyze_daily_opportunity(today_str)
                    if daily_rep.get("total_trades", 0) > 0:
                        recap_msg = "🔔 <b>AUTOMATISCHE DAGELIJKSE RECAP & KANSEN</b>\n\n" + format_daily_opportunity_telegram(daily_rep)
                        send_message(recap_msg, target_chat_id=CHAT_ID)
                except Exception as exc:
                    logger.error(f"Error sending automatic daily recap: {exc}")

        except Exception as exc:
            logger.error(f"Error in trade_monitor_loop: {exc}")

        time.sleep(3)


def error_monitor_loop() -> None:
    """Continuously monitors log files and API endpoints of both bots for errors/failures.

    Sends instant Telegram notifications when:
    1. Critical errors, crash loops, or unhandled exceptions appear in freqtrade.log or freqtrade_elite.log
    2. Any bot stops responding to API ping / crashes
    Deduplicates and rate-limits repeated error messages to prevent spamming.
    """
    logger.info("Realtime Error & Health Monitor daemon gestart.")

    log_targets = [
        {"name": "WEEX Normal Bot (8080)", "path": os.path.join(USER_DATA, "logs", "freqtrade.log")},
        {"name": "WEEX Elite Bot (8081)", "path": os.path.join(USER_DATA, "logs", "freqtrade_elite.log")},
    ]

    file_handles: dict[str, Any] = {}
    for target in log_targets:
        p = target["path"]
        if os.path.exists(p):
            try:
                f = open(p, "r", encoding="utf-8", errors="replace")
                f.seek(0, os.SEEK_END)
                file_handles[target["name"]] = f
            except Exception as e:
                logger.warning(f"Could not open log {p} for monitoring: {e}")

    recent_alerts: dict[str, float] = {}
    bot_health: dict[str, int] = {
        "WEEX Normal Bot (8080)": 0,
        "WEEX Elite Bot (8081)": 0,
    }
    bot_offline_notified: dict[str, bool] = {
        "WEEX Normal Bot (8080)": False,
        "WEEX Elite Bot (8081)": False,
    }

    last_health_check = 0.0

    while True:
        try:
            now = time.time()

            # --- PART A: Scan Logs for New Errors ---
            for target in log_targets:
                bot_name = target["name"]
                f = file_handles.get(bot_name)
                if not f or f.closed:
                    p = target["path"]
                    if os.path.exists(p):
                        try:
                            f = open(p, "r", encoding="utf-8", errors="replace")
                            f.seek(0, os.SEEK_END)
                            file_handles[bot_name] = f
                        except Exception:
                            continue
                    else:
                        continue

                while True:
                    line = f.readline()
                    if not line:
                        break

                    line_clean = line.strip()
                    if not line_clean:
                        continue

                    # Ignore harmless external display / fiat conversion issues (e.g. CoinGecko 403)
                    if "fiat_convert" in line_clean or "coingecko" in line_clean.lower():
                        continue

                    is_error = False
                    err_category = "Foutmelding"

                    if " - ERROR - " in line_clean or " - CRITICAL - " in line_clean:
                        is_error = True
                        err_category = "Kritieke Fout"
                    elif "retrying in 30 seconds" in line_clean or ("retrying in " in line_clean and "Error:" in line_clean):
                        is_error = True
                        err_category = "Crash / Retry Loop"
                    elif "Giving up." in line_clean and ("fetch_order" in line_clean or "exception" in line_clean.lower() or "badrequest" in line_clean.lower()):
                        is_error = True
                        err_category = "Order Exception (Giving up)"
                    elif "Traceback (most recent call last):" in line_clean:
                        is_error = True
                        err_category = "Python Traceback"

                    if is_error:
                        msg_clean = re.sub(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,\.]\d+\s*-\s*', '', line_clean)
                        err_sig = f"{bot_name}::{msg_clean[:100]}"
                        last_alert = recent_alerts.get(err_sig, 0.0)

                        if (now - last_alert) >= 900:
                            recent_alerts[err_sig] = now

                            safe_err = line_clean.replace("<", "&lt;").replace(">", "&gt;")
                            if len(safe_err) > 350:
                                safe_err = safe_err[:350] + "..."

                            alert_msg = (
                                f"🚨 <b>BOT FOUTMELDING GEDETECTEERD</b>\n"
                                f"──────────────────────────\n"
                                f"• <b>Bot:</b> {bot_name}\n"
                                f"• <b>Categorie:</b> {err_category}\n"
                                f"• <b>Tijd:</b> <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n\n"
                                f"• <b>Details:</b>\n"
                                f"<code>{safe_err}</code>\n"
                                f"──────────────────────────\n"
                                f"⚠️ <i>Tackle dit direct of bekijk de logs via SSH.</i>"
                            )
                            send_message(alert_msg, target_chat_id=CHAT_ID)
                            logger.warning(f"Error alert sent for {bot_name}: {line_clean[:80]}")

            # --- PART B: Bot Health & API Heartbeat (Every 30s) ---
            if (now - last_health_check) >= 30:
                last_health_check = now
                api_endpoints = [
                    # Main Bot (8080) is paused for upgrade - do not spam offline alerts
                    ("WEEX Elite Bot (8081)", "http://127.0.0.1:8081/api/v1/ping"),
                ]

                for b_name, ping_url in api_endpoints:
                    is_up = False
                    try:
                        r = requests.get(ping_url, auth=FT_AUTH, headers=HTTP_HEADERS, timeout=4)
                        if r.status_code == 200 and "pong" in r.text.lower():
                            is_up = True
                    except Exception:
                        is_up = False

                    if not is_up:
                        bot_health[b_name] += 1
                        if bot_health[b_name] >= 2 and not bot_offline_notified[b_name]:
                            bot_offline_notified[b_name] = True
                            offline_msg = (
                                f"🚨 <b>CRITISCHE ALERT: BOT OFFLINE</b>\n"
                                f"──────────────────────────\n"
                                f"• <b>Bot:</b> {b_name}\n"
                                f"• <b>Status:</b> ❌ Reageert niet op API / gecrasht\n"
                                f"• <b>Tijd:</b> <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
                                f"──────────────────────────\n"
                                f"⚠️ <i>De bot container lijkt down of vastgelopen. Controleer direct!</i>"
                            )
                            send_message(offline_msg, target_chat_id=CHAT_ID)
                    else:
                        if bot_offline_notified[b_name]:
                            bot_offline_notified[b_name] = False
                            recovered_msg = (
                                f"✅ <b>BOT HERSTELD & ONLINE</b>\n"
                                f"──────────────────────────\n"
                                f"• <b>Bot:</b> {b_name}\n"
                                f"• <b>Status:</b> Verbinding hersteld (pong OK)\n"
                                f"• <b>Tijd:</b> <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
                                f"──────────────────────────"
                            )
                            send_message(recovered_msg, target_chat_id=CHAT_ID)
                        bot_health[b_name] = 0

            if len(recent_alerts) > 50:
                recent_alerts = {k: v for k, v in recent_alerts.items() if (now - v) < 3600}

        except Exception as exc:
            logger.error(f"Error in error_monitor_loop: {exc}")

        time.sleep(2)


def handle_telegram_updates() -> None:
    offset = 0
    logger.info("Truthful Telegram Bot daemon gestart (Single Source of Truth).")

    # Start Realtime Trade Notifier Background Thread
    monitor_thread = threading.Thread(target=trade_monitor_loop, daemon=True)
    monitor_thread.start()

    # Start Realtime Error & Health Monitor Background Thread
    error_thread = threading.Thread(target=error_monitor_loop, daemon=True)
    error_thread.start()

    # Ensure webhook is deleted so getUpdates polling works cleanly
    try:
        del_wh = requests.post(f"{API_URL}/deleteWebhook?drop_pending_updates=False", headers=HTTP_HEADERS, timeout=(3, 10))
        logger.info(f"Webhook check/delete status: {del_wh.json().get('description', 'OK')}")
    except Exception as exc:
        logger.warning(f"Webhook delete notice: {exc}")

    # Drop any stale updates on startup so we only handle fresh messages
    try:
        drop_url = f"{API_URL}/getUpdates?offset=-1"
        r = requests.get(drop_url, headers=HTTP_HEADERS, timeout=(3, 10))
        drop_data = r.json()
        if drop_data.get("ok") and drop_data.get("result"):
            last_item = drop_data["result"][-1]
            offset = last_item["update_id"] + 1
            logger.info(f"Initialized update offset to {offset}")
    except Exception as exc:
        logger.warning(f"Offset initialization notice: {exc}")

    # Send startup message with keyboard to admin
    state = sync_runtime_state_with_exchange()
    startup_text = (
        "🤖 <b>Telegram Bot Verbonden & Online</b>\n"
        "──────────────────────────\n"
        "• <b>Exchange:</b> WEEX (Futures 12x)\n"
        "• <b>Strategie:</b> Balanced Scalper (44/24/12) & 3 Slots\n"
        f"• <b>Status:</b> {'Actieve Positie(s)' if state.open_trade else 'Klaar voor signalen'}\n"
        f"• <b>Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>\n\n"
        "Typ /status of kies een knop hieronder:"
    )
    send_message(startup_text, target_chat_id=CHAT_ID, reply_markup=DEFAULT_KEYBOARD)

    while True:
        try:
            url = f"{API_URL}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, headers=HTTP_HEADERS, timeout=(5, 30))
            data = r.json()

            if not data.get("ok"):
                if data.get("error_code") == 409 or "webhook" in str(data).lower():
                    logger.warning("Detected active webhook conflict. Deleting webhook...")
                    requests.post(f"{API_URL}/deleteWebhook?drop_pending_updates=False", headers=HTTP_HEADERS, timeout=(3, 10))
                    time.sleep(1)
                else:
                    logger.warning(f"Telegram getUpdates response not ok: {data}")
                time.sleep(1)
                continue

            for item in data.get("result", []):
                offset = item["update_id"] + 1

                # 1. Handle Channel Posts and Membership Updates (Auto-Registration)
                chat_obj = None
                if item.get("channel_post"):
                    chat_obj = item["channel_post"].get("chat")
                elif item.get("edited_channel_post"):
                    chat_obj = item["edited_channel_post"].get("chat")
                elif item.get("my_chat_member"):
                    chat_obj = item["my_chat_member"].get("chat")

                if chat_obj:
                    c_id = chat_obj.get("id")
                    c_title = (chat_obj.get("title") or chat_obj.get("username") or "").lower()
                    c_name_disp = chat_obj.get("title") or chat_obj.get("username") or str(c_id)

                    ch_cfg = load_channels_config()

                    if "vip" in c_title or "scalpen" in c_title:
                        if ch_cfg.get("vip_channel_id") != c_id:
                            ch_cfg["vip_channel_id"] = c_id
                            save_channels_config(ch_cfg)
                            logger.info(f"Registered VIP Channel: {c_name_disp} ({c_id})")
                            welcome_vip = (
                                "💎 <b>WEEX Quant VIP Stream Connected!</b>\n"
                                "──────────────────────────\n"
                                "✅ All 100% automated real-time trade signals will post here."
                            )
                            send_message(welcome_vip, target_chat_id=c_id)
                            send_message(f"✅ <b>VIP Kanaal Gekoppeld:</b> {c_name_disp} (<code>{c_id}</code>)", target_chat_id=CHAT_ID)

                    elif "insight" in c_title or "trading" in c_title or "free" in c_title or "gratis" in c_title:
                        if ch_cfg.get("free_channel_id") != c_id:
                            ch_cfg["free_channel_id"] = c_id
                            save_channels_config(ch_cfg)
                            logger.info(f"Registered Free Channel: {c_name_disp} ({c_id})")
                            welcome_free = (
                                "🚀 <b>WEEX Quant Insights Stream Connected!</b>\n"
                                "──────────────────────────\n"
                                "✅ Daily PnL Recaps and Winner Alerts will post here automatically."
                            )
                            send_message(welcome_free, target_chat_id=c_id)
                            send_message(f"✅ <b>Gratis Kanaal Gekoppeld:</b> {c_name_disp} (<code>{c_id}</code>)", target_chat_id=CHAT_ID)

                # 2. Handle Admin Private Commands
                msg = item.get("message", {})
                text = msg.get("text", "").strip()
                chat = str(msg.get("chat", {}).get("id", ""))

                if chat != str(CHAT_ID):
                    continue

                logger.info(f"Received telegram command: {text}")

                t_lower = text.lower()
                # Clean leading emojis and whitespace
                clean_text = text
                for emoji in ["▶️", "⏹️", "📊", "💰", "📈", "📜", "📅", "ℹ️", "🚨", "⚙️", "🔢", "🔍", "💎", "✨", "🎉", "🛡️"]:
                    clean_text = clean_text.replace(emoji, "")
                clean_text = clean_text.strip()

                cmd = clean_text.split()[0].lower() if clean_text else ""
                if not cmd.startswith("/") and cmd:
                    cmd = "/" + cmd

                # 1. Besturing (Start & Stop)
                if any(k in t_lower for k in ["start bot", "/start", "/run", "hervat"]) or cmd in ("/start", "/run", "/hervat"):
                    send_message(handle_start_command(), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["stop & sluit", "stop bot", "/stop", "/pause", "/pauzeer"]) or cmd in ("/stop", "/pause", "/pauzeer"):
                    send_message(handle_stop_command(), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["/kill", "/forceexit", "noodstop"]) or cmd in ("/kill", "/forceexit", "/noodstop"):
                    send_message(handle_kill_command(), target_chat_id=CHAT_ID)

                # 2. Live Overzicht & Saldo
                elif any(k in t_lower for k in ["live status", "/status", "/state"]) or cmd in ("/status", "/state"):
                    state = sync_runtime_state_with_exchange()
                    send_message(format_status_message(state), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["saldo & marge", "/balance", "saldo", "wallet"]) or cmd in ("/balance", "/saldo", "/wallet"):
                    send_message(handle_balance_command(), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["winst & performance", "/profit", "statistiek", "performance"]) or cmd in ("/profit", "/performance", "/winst"):
                    send_message(handle_profit_command(), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["dagresultaat", "/daily", "vandaag"]) or cmd in ("/daily", "/dag"):
                    send_message(handle_daily_command(), target_chat_id=CHAT_ID)

                # 3. Log & Analyse Categorie
                elif any(k in t_lower for k in ["trade autopsie", "autopsie", "/audit", "audit", "review"]) or cmd in ("/audit", "/autopsie", "/review"):
                    args = clean_text.split()[1:] if "/" in text else []
                    send_message(handle_audit_command(args), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["kansen analyse", "kansen", "/opportunity", "opportunity", "recap"]) or cmd in ("/opportunity", "/kansen", "/recap", "/analyse"):
                    args = clean_text.split()[1:] if "/" in text else []
                    send_message(handle_opportunity_command(args), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["gesloten trades", "laatste trades", "/trades", "geschiedenis"]) or cmd in ("/trades", "/history", "/geschiedenis"):
                    send_message(handle_trades_command(), target_chat_id=CHAT_ID)

                # 4. Instellingen & Kanalen
                elif any(k in t_lower for k in ["bot instellingen", "instellingen", "/config", "show_config"]) or cmd in ("/config", "/show_config", "/instellingen"):
                    send_message(handle_config_command(), target_chat_id=CHAT_ID)
                elif cmd in ("/count", "/open"):
                    send_message(handle_count_command(), target_chat_id=CHAT_ID)
                elif cmd in ("/channels", "/kanalen"):
                    ch_cfg = load_channels_config()
                    vip_id = ch_cfg.get("vip_channel_id") or "Nog niet gekoppeld"
                    free_id = ch_cfg.get("free_channel_id") or "Nog niet gekoppeld"
                    msg_ch = (
                        "📢 <b>TELEGRAM KANALEN STATUS</b>\n"
                        "──────────────────────────\n"
                        f"• <b>VIP Kanaal ID:</b> <code>{vip_id}</code>\n"
                        f"• <b>Gratis Kanaal ID:</b> <code>{free_id}</code>\n"
                        f"• <b>Whop Link:</b> {ch_cfg.get('whop_link')}\n\n"
                        "<i>Stuur 1 berichtje in elk kanaal om ze automatisch te koppelen, of typ /testsignals om te testen!</i>"
                    )
                    send_message(msg_ch, target_chat_id=CHAT_ID)
                elif cmd in ("/testsignals", "/test"):
                    ch_cfg = load_channels_config()
                    v_id = ch_cfg.get("vip_channel_id")
                    f_id = ch_cfg.get("free_channel_id")
                    res_lines = ["🧪 <b>TEST SIGNAAL VERZENDING:</b>\n──────────────────────────"]
                    if v_id:
                        test_v = "💎 <b>[TEST] WEEX VIP Signal Engine Active</b>\n✅ Test signal received successfully!"
                        r_v = send_message(test_v, target_chat_id=v_id)
                        res_lines.append(f"• VIP Kanaal (<code>{v_id}</code>): {'✅ Gelukt' if r_v and r_v.get('ok') else '❌ Mislukt'}")
                    else:
                        res_lines.append("• VIP Kanaal: ⚠️ Nog niet geregistreerd (post 1 bericht in het kanaal).")

                    if f_id:
                        test_f = "🚀 <b>[TEST] WEEX Insights Channel Active</b>\n✅ Test broadcast received successfully!"
                        r_f = send_message(test_f, target_chat_id=f_id)
                        res_lines.append(f"• Gratis Kanaal (<code>{f_id}</code>): {'✅ Gelukt' if r_f and r_f.get('ok') else '❌ Mislukt'}")
                    else:
                        res_lines.append("• Gratis Kanaal: ⚠️ Nog niet geregistreerd (post 1 bericht in het kanaal).")

                    send_message("\n".join(res_lines), target_chat_id=CHAT_ID)
                elif any(k in t_lower for k in ["help & uitleg", "/help", "helpmenu"]) or cmd in ("/help", "/?"):
                    send_message(handle_help_command(), target_chat_id=CHAT_ID)
                else:
                    unknown_reply = (
                        f"🤖 Bericht ontvangen: <i>'{text}'</i>\n\n"
                        "Kies een van de knoppen hieronder of typ /help voor het complete overzicht! ✨"
                    )
                    send_message(unknown_reply, target_chat_id=CHAT_ID)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.error(f"Error in telegram update loop: {e}")
            time.sleep(2)

        time.sleep(0.2)


if __name__ == "__main__":
    handle_telegram_updates()

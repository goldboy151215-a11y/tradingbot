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

DEFAULT_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ /start"}, {"text": "⏹️ /stop"}],
        [{"text": "📊 /status"}, {"text": "💰 /balance"}],
        [{"text": "📈 /profit"}, {"text": "📜 /trades"}],
        [{"text": "⚙️ /config"}, {"text": "📅 /daily"}],
        [{"text": "ℹ️ /help"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}


def send_message(text: str, parse_mode: str = "HTML", reply_markup: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        payload: dict[str, Any] = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = DEFAULT_KEYBOARD

        r = requests.post(f"{API_URL}/sendMessage", json=payload, headers=HTTP_HEADERS, timeout=(3, 10))
        res = r.json()
        if not res.get("ok"):
            logger.warning(f"Telegram API warning: {res.get('description')}")
            # Fallback without parse_mode if entity parsing failed
            if "can't parse entities" in str(res.get("description", "")):
                payload.pop("parse_mode", None)
                r2 = requests.post(f"{API_URL}/sendMessage", json=payload, headers=HTTP_HEADERS, timeout=(3, 10))
                return r2.json()
        return res
    except Exception as e:
        logger.error(f"Error sending telegram message: {e}")
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

    # Query live open trades directly from Freqtrade API
    open_trades_list = []
    try:
        r = requests.get(f"{FT_API_URL}/status", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(2, 3))
        if r.status_code == 200:
            open_trades_list = r.json()
    except Exception as exc:
        logger.warning(f"Could not fetch FT open trades status: {exc}")

    if open_trades_list:
        open_trades_blocks = []
        for t in open_trades_list:
            pair = t.get("pair", "Onbekend")
            side = "🔴 SHORT" if t.get("is_short") else "🟢 LONG"
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
                f"• <b>{pair}</b> ({side} {lev:.0f}x)\n"
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
        status_line = "✅ <b>ACTIEF (Verbonden met WEEX Futures)</b>"

    mode_str = "Live" if state.live else "Dry-run"

    msg = f"""<b>📊 {title}</b>
──────────────────────────
• <b>Exchange:</b> WEEX (Futures {state.leverage:.0f}x)
• <b>Modus:</b> {mode_str} | <b>Strategie:</b> 5m Scalper
• <b>Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code> (Vrij: <code>${state.free_usdt:.2f} USDT</code>)
• <b>Stake:</b> <code>${state.stake_usdt:.2f} USDT</code> ({state.leverage:.0f}x Isolated)

<b>📅 RESULTAAT VANDAAG ({today_disp}):</b>
• <b>Gerealiseerde Winst:</b> {today_pnl_str}
• <b>Trades Vandaag:</b> {cnt_today} gesloten ({wins_today}W / {losses_today}L)
• <b>Winrate Vandaag:</b> {wr_today:.1f}%

<b>📍 ACTIEVE POSITIE(S):</b>
{open_trade_str}

<b>📈 TOTAAL GESLOTEN TRADES:</b>
• <b>Totale Winst:</b> {all_pnl_str}
• <b>Totaal Trades:</b> {total_closed} gesloten ({wins_all}W / {losses_all}L)
• <b>All-Time Winrate:</b> {wr_all:.1f}%

<b>🛡️ Systeemstatus:</b>
{status_line}
"""
    return msg


def handle_start_command() -> str:
    """Handle /start: strictly verify parity; refuse to start on ANY mismatch."""
    state = sync_runtime_state_with_exchange()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        ft_config = json.load(f)

    # Perform strict verification
    is_valid, mismatches = verify_config_sync(state, ft_config)

    if not is_valid:
        mismatch_bullets = "\n".join(f"• {m}" for m in mismatches)
        logger.error(f"CONFIG MISMATCH on /start: {mismatches}")
        return (
            "⚠️ <b>CONFIG MISMATCH. Bot start NIET tot dit gelijk is.</b>\n"
            "──────────────────────────\n"
            f"{mismatch_bullets}\n\n"
            "<i>Trading loop is NIET gestart ter bescherming van kapitaal.</i>"
        )

    # If valid, trigger Freqtrade start
    try:
        r = requests.post(f"{FT_API_URL}/start", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(2, 4))
        if r.status_code == 200:
            logger.info("Freqtrade trading loop started via API")
            return format_status_message(state, title="BOT GESTART (Config Geverifieerd)")
        else:
            return f"❌ Fout bij starten Freqtrade API: {r.status_code} - {r.text}"
    except Exception as exc:
        logger.error(f"Could not connect to Freqtrade API: {exc}")
        return f"❌ Verbinding met Freqtrade API mislukt: {exc}"


def handle_stop_command() -> str:
    try:
        r = requests.post(f"{FT_API_URL}/stop", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(2, 4))
        return "⏸️ <b>Trading Loop Gepauzeerd (/stop)</b>\nGeen nieuwe entries worden geplaatst."
    except Exception as exc:
        return f"❌ Fout bij stoppen: {exc}"


def handle_kill_command() -> str:
    try:
        r = requests.post(f"{FT_API_URL}/forceexit", auth=FT_AUTH, json={}, headers=HTTP_HEADERS, timeout=(2, 4))
        return (
            "🚨 <b>NOODSTOP UITGEVOERD (/kill)</b>\n"
            "Alle openstaande orders en posities worden gesloten.\n"
            f"Response code: {r.status_code}"
        )
    except Exception as exc:
        return f"❌ Fout bij uitvoeren noodstop: {exc}"


def handle_balance_command() -> str:
    state = sync_runtime_state_with_exchange()
    try:
        r = requests.get(f"{FT_API_URL}/balance", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(2, 3))
        if r.status_code == 200:
            bal_data = r.json()
            total = bal_data.get("total", state.equity_usdt)
            curr_list = bal_data.get("currencies", [])
            usdt_info = next((c for c in curr_list if c.get("currency") == "USDT"), {})
            free = usdt_info.get("free", state.free_usdt)
            used = max(0.0, total - free)
            return f"""<b>💰 WALLET SALDO (WEEX)</b>
──────────────────────────
• <b>Totaal Saldo:</b> <code>${total:.2f} USDT</code>
• <b>Vrij Beschikbaar:</b> <code>${free:.2f} USDT</code>
• <b>In Positie (Marge):</b> <code>${used:.2f} USDT</code>
• <b>Marge Bezetting:</b> {state.margin_used_pct*100:.1f}% / {state.margin_cap_pct*100:.0f}% cap
• <b>Max Stake per Trade:</b> <code>${state.stake_usdt:.2f} USDT</code>
• <b>Status:</b> 🟢 Verbonden & Gesynchroniseerd
"""
    except Exception as exc:
        logger.error(f"Error in handle_balance_command: {exc}")

    return f"""<b>💰 WALLET SALDO (WEEX)</b>
──────────────────────────
• <b>Totaal Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
• <b>Vrij Beschikbaar:</b> <code>${state.free_usdt:.2f} USDT</code>
"""


def handle_count_command() -> str:
    state = sync_runtime_state_with_exchange()
    open_count = 1 if state.open_trade else 0
    status_icon = "🔴 Bezet (max 1 open trade bereikt)" if open_count >= 1 else "🟢 Vrij (actief scannend naar signalen)"
    return f"""<b>🔢 ACTIEVE POSITIES & SLOTS</b>
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
        return "<b>📜 RECENTE TRADES</b>\n──────────────────────────\nGeen gesloten trades gevonden."

    lines = ["<b>📜 LAATSTE GESLOTEN TRADES</b>\n──────────────────────────"]
    for r in rows:
        side_badge = "🔴 SHORT" if r["is_short"] else "🟢 LONG"
        profit_pct = (r["close_profit"] or 0.0) * 100
        pnl = r["close_profit_abs"] or 0.0
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pct_str = f"+{profit_pct:.2f}%" if profit_pct >= 0 else f"{profit_pct:.2f}%"
        date_str = str(r["close_date"] or "")[:16]
        reason = r["exit_reason"] or "onbekend"
        lines.append(
            f"• <b>#{r['id']} {r['pair']}</b> ({side_badge})\n"
            f"  Resultaat: <b>{pnl_str}</b> ({pct_str}) | Reden: <i>{reason}</i>\n"
            f"  Gesloten: <code>{date_str}</code>"
        )
    return "\n\n".join(lines)


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

    # Also check live open trade unrealized PnL
    open_pnl = 0.0
    open_info_str = "Geen open posities"
    try:
        r = requests.get(f"{FT_API_URL}/status", auth=FT_AUTH, headers=HTTP_HEADERS, timeout=(2, 3))
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

    return f"""<b>📅 RESULTATEN VANDAAG ({today_disp})</b>
──────────────────────────
• <b>Gerealiseerde Winst:</b> {pnl_str}
• <b>Gesloten Trades:</b> {cnt} ({wins}W / {losses}L)
• <b>Winrate Vandaag:</b> {winrate:.1f}%
• <b>Open Positie PnL:</b> {tot_icon} <code>{'+' if open_pnl >= 0 else ''}${open_pnl:.2f} USDT</code> ({open_info_str})
• <b>Totaal Vandaag (Incl. Open):</b> <b>{tot_icon} {'+' if tot_today >= 0 else ''}${tot_today:.2f} USDT</b>
• <b>Huidig Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
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

    return f"""<b>📈 PROFIT & RESULTATEN</b>
──────────────────────────
• <b>Saldo (Equity):</b> <code>${state.equity_usdt:.2f} USDT</code>
• <b>Winst Vandaag:</b> {p_icon_today} <b>{'+' if pnl_today >= 0 else ''}${pnl_today:.2f} USDT</b> ({cnt_today} trades)
• <b>Totale Gerealiseerde PnL:</b> {p_icon_all} <b>{'+' if pnl_sum >= 0 else ''}${pnl_sum:.2f} USDT</b>
• <b>Totaal Trades Gesloten:</b> {total_closed} ({wins}W / {total_closed - wins}L)
• <b>All-Time Winrate:</b> <b>{winrate:.1f}%</b>
• <b>Hefboom:</b> {state.leverage:.0f}x Isolated | <b>Stake:</b> ${state.stake_usdt:.2f} USDT
"""


def handle_config_command() -> str:
    state = sync_runtime_state_with_exchange()
    compounded_stake = state.equity_usdt * 0.58
    return f"""<b>⚙️ ACTIEVE BOT CONFIGURATIE</b>
──────────────────────────
• <b>Strategie:</b> WEEX Futures BB 1.8 Squeeze Scalper
• <b>Hefboom:</b> {state.leverage:.0f}x Isolated Margin
• <b>Auto-Compounding:</b> ✅ <b>ACTIEF (58% per trade)</b>
• <b>Huidig Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>
• <b>Inzet Volgende Trade:</b> <code>${compounded_stake:.2f} USDT</code> (58% van saldo)
• <b>Positiewaarde Volgende Trade:</b> <code>${compounded_stake * state.leverage:.2f} USDT</code>

<b>📍 ACTIEVE PAREN (Core 4 Kampioenen):</b>
• <code>BTC/USDT:USDT</code> (Bitcoin Perpetual)
• <code>ETH/USDT:USDT</code> (Ethereum Perpetual)
• <code>SOL/USDT:USDT</code> (Solana Perpetual)
• <code>AVAX/USDT:USDT</code> (Avalanche Perpetual)

<b>🛡️ RISICOBEHEER & DOELEN:</b>
• <b>Stoploss:</b> <code>-20.0% ROE</code> (-1,67% koersdaling)
• <b>Take-Profit Direct (Pump):</b> <code>+44.0% ROE</code> (+3,67% koers)
• <b>Take-Profit na 15 min:</b> <code>+24.0% ROE</code> (+2,00% koers)
• <b>Take-Profit na 30 min:</b> <code>+12.0% ROE</code> (+1,00% koers)
• <b>Max Open Posities:</b> 1 positie (100% marge-focus)
• <b>Cooldown:</b> 0 min (Directe herinstap bij signaal)
• <b>Realtime Alerts:</b> ✅ AAN (Push bij elke open & close)
"""


def handle_help_command() -> str:
    return """<b>📖 BESCHIKBARE COMMANDO'S</b>
──────────────────────────
/status - Actuele live status & positie overzicht
/balance - Wallet saldo & vrije marge op Weex
/profit - Winst, winrate & performance
/trades - Overzicht laatste gesloten trades
/count - Aantal actieve posities & beschikbare slots
/daily - Prestaties en winst van vandaag
/config - Geverifieerde parameters & stoploss/ROI
/start - Start trading (met strikte config parity check)
/stop - Pauzeer nieuwe entries
/kill - Noodstop (sluit openstaande orders)
/help - Dit helpmenu
"""


def trade_monitor_loop() -> None:
    """Background loop that detects newly opened and closed trades in SQLite and sends instant push notifications."""
    last_known_open_ids: set[int] = set()
    last_known_closed_ids: set[int] = set()

    # Initial seeding of existing IDs so we don't spam historical trades on bot restart
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for r in c.execute("SELECT id FROM trades WHERE is_open=1"):
                last_known_open_ids.add(r[0])
            for r in c.execute("SELECT id FROM trades WHERE is_open=0"):
                last_known_closed_ids.add(r[0])
            conn.close()
    except Exception as e:
        logger.warning(f"Trade monitor initial seed notice: {e}")

    logger.info("Realtime Trade Notifier daemon gestart.")

    while True:
        try:
            if os.path.exists(DB_PATH):
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()

                # 1. Check for newly opened trades
                rows_open = c.execute("SELECT * FROM trades WHERE is_open=1").fetchall()
                for r in rows_open:
                    tid = r["id"]
                    if tid not in last_known_open_ids:
                        last_known_open_ids.add(tid)
                        pair = r["pair"]
                        side = "🔴 SHORT" if r["is_short"] else "🟢 LONG"
                        stake = r["stake_amount"] or 0.0
                        rate = r["open_rate"] or 0.0
                        lev = r["leverage"] or 12.0
                        msg = (
                            f"⚡ <b>NIEUWE TRADE GEOPEND (#{tid})</b>\n"
                            f"──────────────────────────\n"
                            f"• <b>Munt:</b> <code>{pair}</code> ({side} {lev:.0f}x)\n"
                            f"• <b>Instap Koers:</b> <code>{rate:.4f}</code>\n"
                            f"• <b>Inzet (Stake):</b> <code>${stake:.2f} USDT</code>\n"
                            f"• <b>Totale Positie:</b> <code>${stake * lev:.2f} USDT</code>\n"
                            f"• <b>Strategie:</b> 5m BB Squeeze Breakout 🎯\n\n"
                            f"<i>De bot bewaakt automatisch de Stoploss (-20%) en Take-Profit ladder (+44%/+24%/+12%).</i>"
                        )
                        send_message(msg)

                # 2. Check for newly closed trades
                rows_closed = c.execute("SELECT * FROM trades WHERE is_open=0 ORDER BY id DESC LIMIT 10").fetchall()
                for r in rows_closed:
                    tid = r["id"]
                    if tid not in last_known_closed_ids:
                        last_known_closed_ids.add(tid)
                        last_known_open_ids.discard(tid)
                        pair = r["pair"]
                        pnl_abs = r["close_profit_abs"] or 0.0
                        pnl_pct = (r["close_profit"] or 0.0) * 100
                        reason = r["exit_reason"] or "take_profit"
                        icon = "🟢" if pnl_abs >= 0 else "🔴"
                        state_now = sync_runtime_state_with_exchange()
                        msg = (
                            f"🎯 <b>TRADE GESLOTEN (#{tid})</b>\n"
                            f"──────────────────────────\n"
                            f"• <b>Munt:</b> <code>{pair}</code>\n"
                            f"• <b>Resultaat:</b> {icon} <b>{'+' if pnl_abs >= 0 else ''}${pnl_abs:.2f} USDT ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)</b>\n"
                            f"• <b>Exit Reden:</b> <code>{reason}</code>\n"
                            f"• <b>Nieuw Saldo:</b> <code>${state_now.equity_usdt:.2f} USDT</code>\n\n"
                            f"<i>Volgende trade wordt automatisch gecaptured op basis van nieuw saldo (58% compounding).</i>"
                        )
                        send_message(msg)

                conn.close()
        except Exception as exc:
            logger.error(f"Error in trade_monitor_loop: {exc}")

        time.sleep(3)


def handle_telegram_updates() -> None:
    offset = 0
    logger.info("Truthful Telegram Bot daemon gestart (Single Source of Truth).")

    # Start Realtime Trade Notifier Background Thread
    monitor_thread = threading.Thread(target=trade_monitor_loop, daemon=True)
    monitor_thread.start()

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

    # Send startup message with keyboard
    state = sync_runtime_state_with_exchange()
    startup_text = (
        "🤖 <b>Telegram Bot Verbonden & Online</b>\n"
        "──────────────────────────\n"
        "• <b>Exchange:</b> WEEX (Futures)\n"
        f"• <b>Status:</b> {'Actieve Positie' if state.open_trade else 'Klaar voor signalen'}\n"
        f"• <b>Saldo:</b> <code>${state.equity_usdt:.2f} USDT</code>\n\n"
        "Typ /status of kies een knop hieronder:"
    )
    send_message(startup_text, reply_markup=DEFAULT_KEYBOARD)

    while True:
        try:
            url = f"{API_URL}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, headers=HTTP_HEADERS, timeout=(5, 30))
            data = r.json()

            if data.get("ok"):
                for item in data.get("result", []):
                    offset = item["update_id"] + 1
                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()
                    chat = str(msg.get("chat", {}).get("id", ""))

                    if chat != str(CHAT_ID):
                        continue

                    logger.info(f"Received telegram command: {text}")

                    # Clean leading emojis and whitespace
                    clean_text = text
                    for emoji in ["▶️", "⏹️", "📊", "💰", "📈", "📜", "📅", "ℹ️", "🚨", "⚙️", "🔢"]:
                        clean_text = clean_text.replace(emoji, "")
                    clean_text = clean_text.strip()

                    cmd = clean_text.split()[0].lower() if clean_text else ""
                    if not cmd.startswith("/") and cmd:
                        cmd = "/" + cmd

                    if cmd in ("/start", "/run", "/hervat"):
                        send_message(handle_start_command())
                    elif cmd in ("/status", "/state"):
                        state = sync_runtime_state_with_exchange()
                        send_message(format_status_message(state))
                    elif cmd in ("/balance", "/saldo", "/wallet"):
                        send_message(handle_balance_command())
                    elif cmd in ("/count", "/open"):
                        send_message(handle_count_command())
                    elif cmd in ("/profit", "/performance", "/winst"):
                        send_message(handle_profit_command())
                    elif cmd in ("/trades", "/history", "/geschiedenis"):
                        send_message(handle_trades_command())
                    elif cmd in ("/daily", "/dag"):
                        send_message(handle_daily_command())
                    elif cmd in ("/config", "/show_config", "/instellingen"):
                        send_message(handle_config_command())
                    elif cmd in ("/stop", "/pause", "/pauzeer"):
                        send_message(handle_stop_command())
                    elif cmd in ("/kill", "/forceexit", "/noodstop"):
                        send_message(handle_kill_command())
                    elif cmd in ("/help", "/?"):
                        send_message(handle_help_command())
                    else:
                        # Fallback for ANY message or unknown command
                        unknown_reply = (
                            f"🤖 Bericht ontvangen: <i>'{text}'</i>\n\n"
                            "Typ /help voor een overzicht of kies een van de knoppen hieronder om direct de status te bekijken."
                        )
                        send_message(unknown_reply)

        except requests.exceptions.Timeout:
            # Normal long-polling timeout, continue smoothly
            continue
        except Exception as e:
            logger.error(f"Error in telegram update loop: {e}")
            time.sleep(2)

        time.sleep(0.2)


if __name__ == "__main__":
    handle_telegram_updates()

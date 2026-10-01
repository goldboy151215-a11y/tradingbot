import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
import websockets
from websockets.protocol import State

from auth_manager import AuthManager
from order_manager import OrderManagerAcc2
from strategy_orb import StrategyORB
from telegram_alerts import TelegramAlertsAcc2

# Logging setup
LOG_PATH = "/root/tradovate_bot_acc2/tradovate_trader_acc2.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Acc2-ORB] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "/root/tradovate_bot_acc2/config.json"

class BotStateAcc2:
    def __init__(self):
        self.is_paused = False
        self.latest_prices = {}
        self.bars_loaded = {}
        self.last_candle_times = {}
        self.last_signals = {}
        self.last_candle_epoch = time.time()

async def _ws_keepalive(ws, name="Socket"):
    try:
        while ws and getattr(ws, "state", None) == State.OPEN:
            await asyncio.sleep(2.5)
            if ws and getattr(ws, "state", None) == State.OPEN:
                await ws.send("[]")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"[Acc2] {name} keepalive ended: {e}")

async def _market_data_watchdog(state: BotStateAcc2, cfg: dict, alerts: TelegramAlertsAcc2):
    last_watchdog_alert = 0
    while True:
        try:
            await asyncio.sleep(60)
            now_dt = datetime.now(timezone.utc)
            cur_hm = now_dt.strftime("%H:%M")
            s_start = cfg.get("session_start_utc", "07:30")
            s_end = cfg.get("session_end_utc", "18:00")

            if s_start <= cur_hm < s_end:
                now_epoch = time.time()
                if (now_epoch - state.last_candle_epoch) > 900:
                    if (now_epoch - last_watchdog_alert) > 1800:
                        last_watchdog_alert = now_epoch
                        mins = int((now_epoch - state.last_candle_epoch) / 60)
                        alerts.notify_error(
                            "Watchdog: Data Stilstand",
                            f"Al {mins} minuten geen nieuwe candle-data ontvangen tijdens handelssessie ({cur_hm} UTC)."
                        )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[Acc2] Watchdog error: {e}")

async def run_bot():
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    state = BotStateAcc2()
    auth = AuthManager(cfg["username"], cfg["password"])
    strategy = StrategyORB()
    alerts = TelegramAlertsAcc2(cfg["telegram_token"], cfg["telegram_chat_id"])
    orders = None

    logger.info("Starting FundedNext Acc #2 ORB Daemon...")

    # Check for pending credentials
    if "PENDING" in cfg.get("username", ""):
        logger.warning("Account 2 credentials still PENDING. Waiting for user input in config.json...")
        print("[Acc2] Please fill in username, password, and account_spec in /root/tradovate_bot_acc2/config.json")

    try:
        orders = OrderManagerAcc2(cfg, alerts, auth=auth)
        orders.start_watchdog()
        market_watchdog_task = asyncio.create_task(_market_data_watchdog(state, cfg, alerts))

        startup_notified = False
        last_error_alert = 0

        while True:
            # Check if credentials are ready
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            if "PENDING" in cfg.get("username", ""):
                await asyncio.sleep(10)
                continue

            md_keepalive_task = None
            try:
                # 1. Retrieve Tokens
                acc_tok, md_tok = await auth.get_tokens()
                logger.info("[Acc2] Tokens verified. Initializing WebSocket streams...")

                # 2. Connect Orders WebSocket
                await orders.connect_and_authorize(acc_tok)

                assets = cfg.get("assets", {cfg.get("symbol", "MNQZ6"): cfg})

                if not startup_notified:
                    startup_syms = ", ".join(assets.keys())
                    alerts.notify_startup(startup_syms, orders.cash_balance, cfg["account_spec"])
                    startup_notified = True

                # 3. Connect Market Data WebSocket
                md_url = "wss://md-demo.tradovateapi.com/v1/websocket"
                logger.info(f"[Acc2] Connecting to Market Data WS {md_url}...")
                async with websockets.connect(md_url, ping_interval=None) as md_ws:
                    await md_ws.recv()
                    await md_ws.send(f"authorize\n1\n\n{md_tok}")
                    auth_res = await md_ws.recv()
                    logger.info(f"[Acc2] MD Socket Authorized: {auth_res[:100]}")

                    md_keepalive_task = asyncio.create_task(_ws_keepalive(md_ws, "MarketData"))

                    req_id_to_symbol = {}
                    chart_id_to_symbol = {}
                    bars_history = {sym: [] for sym in assets}

                    req_counter = 2
                    for sym in assets.keys():
                        req_id_to_symbol[req_counter] = sym
                        chart_req = {
                            "symbol": sym,
                            "chartDescription": {
                                "underlyingType": "MinuteBar",
                                "elementSize": 5,
                                "elementSizeUnit": "UnderlyingUnits"
                            },
                            "timeRange": {
                                "asMuchAsElements": 60
                            }
                        }
                        await md_ws.send(f"md/getchart\n{req_counter}\n\n{json.dumps(chart_req)}")
                        req_counter += 1

                    while True:
                        if orders.circuit_breaker_active:
                            logger.info(f"[Acc2] Circuit breaker active: {orders.cb_reason}. Sleeping 60s...")
                            await asyncio.sleep(60)
                            continue

                        try:
                            frame = await asyncio.wait_for(md_ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue

                        if frame == "h" or frame == "o" or not frame.startswith("a"):
                            continue

                        data = json.loads(frame[1:])
                        for item in data:
                            req_id = item.get("i")
                            if req_id in req_id_to_symbol:
                                sym = req_id_to_symbol[req_id]
                                res_d = item.get("d", {})
                                h_id = res_d.get("historicalId")
                                r_id = res_d.get("realtimeId")
                                if h_id:
                                    chart_id_to_symbol[h_id] = sym
                                if r_id:
                                    chart_id_to_symbol[r_id] = sym
                                logger.info(f"[Acc2] Mapped chart IDs ({h_id}, {r_id}) -> {sym}")

                            event_type = item.get("e")
                            if event_type == "chart":
                                chart_data = item.get("d", {}).get("charts", [])
                                for c in chart_data:
                                    cid = c.get("id")
                                    sym = chart_id_to_symbol.get(cid)
                                    if not sym:
                                        sym = cfg["symbol"]

                                    new_bars = c.get("bars", [])
                                    status_code = c.get("s", "")
                                    sym_history = bars_history.setdefault(sym, [])

                                    if status_code in ["db", "s"]:
                                        for b in new_bars:
                                            sym_history.append(b)
                                        state.bars_loaded[sym] = len(sym_history)
                                        if sym_history:
                                            state.latest_prices[sym] = float(sym_history[-1].get("close", 0.0))
                                        logger.info(f"[Acc2] Loaded {len(sym_history)} historical 5m bars for {sym}")
                                    else:
                                        spec = assets.get(sym, cfg)
                                        now_utc = datetime.now(timezone.utc)
                                        cur_hm = now_utc.strftime("%H:%M")

                                        for b in new_bars:
                                            current_price = float(b.get("close", 0.0))
                                            state.latest_prices[sym] = current_price

                                            # Real-time position monitor for SL/TP and Breakeven lock
                                            await orders.check_open_position(current_price, sym, spec=spec)

                                            if sym_history and sym_history[-1].get("timestamp") == b.get("timestamp"):
                                                sym_history[-1] = b
                                            else:
                                                # Previous 5m candle closed!
                                                if sym_history and len(sym_history) >= 15:
                                                    closed_bar = sym_history[-1]
                                                    signal, tag, eval_price, rsi_v, oh, ol = strategy.analyze_bars(sym_history, spec=spec, symbol=sym)

                                                    state.last_candle_times[sym] = closed_bar.get("timestamp", "")
                                                    state.last_candle_epoch = time.time()
                                                    state.last_signals[sym] = signal

                                                    logger.info(
                                                        f"[Acc2] [{sym}] Candle [{state.last_candle_times[sym]}] closed @ {eval_price:.2f} | "
                                                        f"RSI: {rsi_v:.1f} | ORB H/L: [{oh:.2f} / {ol:.2f}] | Signal: {signal}{f' ({tag})' if tag else ''}"
                                                    )

                                                    if signal in ["BUY", "SELL"]:
                                                        await orders.execute_signal(signal, tag, current_price, symbol=sym, spec=spec)
                                                        strategy.mark_trade_executed(sym)

                                                sym_history.append(b)
                                                if len(sym_history) > 150:
                                                    sym_history.pop(0)

                                            state.bars_loaded[sym] = len(sym_history)

            except (websockets.exceptions.ConnectionClosedOK, websockets.exceptions.ConnectionClosedError) as ws_err:
                logger.warning(f"[Acc2] Tradovate WS closed ({ws_err}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.warning("[Acc2] Market Data WS timeout. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as exc:
                logger.error(f"[Acc2] Tradovate Bot encountered error: {exc}. Reconnecting in 5s...", exc_info=True)
                now = time.time()
                if (now - last_error_alert) > 300:
                    last_error_alert = now
                    alerts.notify_error("Bot Crash / Exceptie", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(5)
            finally:
                if md_keepalive_task and not md_keepalive_task.done():
                    md_keepalive_task.cancel()
                if orders:
                    await orders.disconnect()

    finally:
        if orders:
            await orders.stop()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("[Acc2] Tradovate Bot stopped by user.")

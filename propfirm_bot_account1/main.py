import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
import websockets

from auth_manager import AuthManager
from order_manager import OrderManager
from strategy_shield import StrategyShield
from telegram_bot import TelegramBot

from websockets.protocol import State

# Logging setup
LOG_PATH = "/root/tradovate_bot/tradovate_trader.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [TradovateShield] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "/root/tradovate_bot/config.json"
STARTUP_LOCK_FILE = "/root/tradovate_bot/last_startup_notify.txt"

class BotState:
    def __init__(self):
        self.is_paused = False
        self.latest_prices = {}
        self.bars_loaded = {}
        self.last_candle_times = {}
        self.last_rsi = {}
        self.last_ema20 = {}
        self.last_ema50 = {}
        self.last_signals = {}
        self.last_candle_epoch = time.time()

    @property
    def latest_price(self):
        return self.latest_prices.get("MNQZ6", 0.0)

    @latest_price.setter
    def latest_price(self, val):
        self.latest_prices["MNQZ6"] = val

    @property
    def last_candle_time(self):
        return self.last_candle_times.get("MNQZ6", "")

    @last_candle_time.setter
    def last_candle_time(self, val):
        self.last_candle_times["MNQZ6"] = val

def should_send_startup_alert() -> bool:
    """Ensure startup alert is not spammed on daemon restarts/reconnects (cooldown 1 hour)."""
    now = time.time()
    if os.path.exists(STARTUP_LOCK_FILE):
        try:
            with open(STARTUP_LOCK_FILE, "r") as f:
                last_time = float(f.read().strip())
            if now - last_time < 3600:
                return False
        except Exception:
            pass
    try:
        with open(STARTUP_LOCK_FILE, "w") as f:
            f.write(str(now))
    except Exception:
        pass
    return True

async def _ws_keepalive(ws, name="Socket"):
    """Sends SockJS empty array [] keepalive every 2.5 seconds as required by Tradovate."""
    try:
        while ws and getattr(ws, "state", None) == State.OPEN:
            await asyncio.sleep(2.5)
            if ws and getattr(ws, "state", None) == State.OPEN:
                await ws.send("[]")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"{name} keepalive ended: {e}")

async def _market_data_watchdog(state: BotState, cfg: dict, telegram_bot: TelegramBot):
    """Monitors if market data is actively flowing during trading session hours."""
    last_watchdog_alert = 0
    while True:
        try:
            await asyncio.sleep(60)
            now_dt = datetime.now(timezone.utc)
            cur_hm = now_dt.strftime("%H:%M")
            s_start = cfg.get("session_start_utc", "08:00")
            s_end = cfg.get("session_end_utc", "19:50")

            # CME Futures are closed on weekends (Friday 21:00 UTC through Sunday 22:00 UTC)
            weekday = now_dt.weekday()
            is_weekend = (weekday == 5) or (weekday == 6 and now_dt.hour < 22) or (weekday == 4 and now_dt.hour >= 21)
            if is_weekend:
                continue

            if s_start <= cur_hm < s_end:
                now_epoch = time.time()
                # If market has been open and no candle updated in last 15 min (900s)
                if (now_epoch - state.last_candle_epoch) > 900:
                    if (now_epoch - last_watchdog_alert) > 1800:
                        last_watchdog_alert = now_epoch
                        mins = int((now_epoch - state.last_candle_epoch) / 60)
                        telegram_bot.notify_error(
                            "Watchdog: Data Stilstand",
                            f"Al {mins} minuten geen nieuwe candle-data ontvangen tijdens handelssessie ({cur_hm} UTC). Mogelijke Tradovate socket freeze."
                        )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Watchdog exception: {e}")

async def run_bot():
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    state = BotState()
    auth = AuthManager(cfg["username"], cfg["password"])
    strategy = StrategyShield()

    # Temporary placeholder before orders is initialized
    orders = None
    telegram_bot = None

    logger.info("Starting FundedNext Tradovate PropFirm Bot daemon with interactive Telegram...")

    try:
        # Initialize OrderManager with auth and telegram_bot
        orders = OrderManager(cfg, None, auth=auth)
        telegram_bot = TelegramBot(cfg, orders, state)
        orders.alerts = telegram_bot

        # Start interactive Telegram polling & Order WS watchdog
        await telegram_bot.start()
        orders.start_watchdog()
        market_watchdog_task = asyncio.create_task(_market_data_watchdog(state, cfg, telegram_bot))

        startup_notified = not should_send_startup_alert()
        last_error_alert = 0

        while True:
            md_keepalive_task = None
            try:
                # 1. Retrieve Tokens
                acc_tok, md_tok = await auth.get_tokens()
                logger.info("Tokens verified. Initializing WebSocket streams...")

                # 2. Connect Orders WebSocket
                await orders.connect_and_authorize(acc_tok)

                assets = cfg.get("assets", {cfg.get("symbol", "MNQZ6"): cfg})

                # Send Telegram Startup Alert ONLY ONCE per session (with 1-hour anti-spam guard)
                if not startup_notified:
                    startup_syms = ", ".join(assets.keys())
                    telegram_bot.notify_startup(startup_syms, orders.cash_balance, cfg["account_spec"])
                    startup_notified = True

                # 3. Connect Market Data WebSocket
                md_url = "wss://md-demo.tradovateapi.com/v1/websocket"
                logger.info(f"Connecting to Market Data WS {md_url}...")
                async with websockets.connect(md_url, ping_interval=None) as md_ws:
                    # Receive open frame 'o'
                    await md_ws.recv()

                    # Authorize MD socket
                    await md_ws.send(f"authorize\n1\n\n{md_tok}")
                    auth_res = await md_ws.recv()
                    logger.info(f"MD Socket Authorized: {auth_res[:100]}")

                    # Start keepalive task for Market Data WS
                    md_keepalive_task = asyncio.create_task(_ws_keepalive(md_ws, "MarketData"))

                    # Subscribe to 5m candles for all configured assets
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
                        await md_ws.send(f"md/getChart\n{req_counter}\n\n{json.dumps(chart_req)}")
                        logger.info(f"Subscribed to 5m candles for {sym} (req_id={req_counter})")
                        req_counter += 1

                    last_exp_check = time.time()

                    while True:
                        # Check token expiration proactively: refresh before expiry (5 min buffer)
                        if time.time() - last_exp_check > 30:
                            last_exp_check = time.time()
                            if time.time() > auth.expiration_time - 300:
                                mins_left = (auth.expiration_time - time.time()) / 60
                                logger.info(f"Tradovate token nearing expiration ({mins_left:.1f}m left). Proactively refreshing session...")
                                break

                            # Ensure Order WS is actively connected
                            if not orders.is_open():
                                logger.warning("Order WS offline in market loop, restoring connection...")
                                await orders.ensure_connected()

                        try:
                            frame = await asyncio.wait_for(md_ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            logger.debug("Market data socket idle for 60s, keeping connection alive...")
                            continue

                        if frame == "h" or frame == "o":
                            continue

                        if not frame.startswith("a"):
                            continue

                        data = json.loads(frame[1:])
                        for item in data:
                            # Map request ID to chart IDs
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
                                logger.info(f"Mapped chart IDs ({h_id}, {r_id}) -> {sym}")

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

                                    # Status 'db' or 's' means historical bars
                                    if status_code in ["db", "s"]:
                                        for b in new_bars:
                                            sym_history.append(b)
                                        state.bars_loaded[sym] = len(sym_history)
                                        if sym_history:
                                            state.latest_prices[sym] = float(sym_history[-1].get("close", 0.0))
                                        logger.info(f"Loaded {len(sym_history)} historical 5m bars for {sym}")
                                    else:
                                        # Real-time bar updates
                                        for b in new_bars:
                                            current_price = float(b.get("close", 0.0))
                                            state.latest_prices[sym] = current_price

                                            # Monitor active position for TP/SL/Breakeven on every tick
                                            await orders.check_open_position(current_price, symbol=sym)

                                            # EOD Auto-close: Close open position before session end (19:50 UTC to precede prop firm liquidation)
                                            now_utc = datetime.now(timezone.utc)
                                            cur_hm = now_utc.strftime("%H:%M")
                                            if cur_hm >= "19:50" and orders.open_position:
                                                logger.warning(f"Session close approaching ({cur_hm} UTC). Auto-closing open position...")
                                                await orders.close_position(current_price, "einde_sessie_autoclose")

                                            # Check if this bar update is continuation of current bar or a new candle
                                            if sym_history and sym_history[-1].get("timestamp") == b.get("timestamp"):
                                                sym_history[-1] = b  # update forming bar
                                            else:
                                                # A new candle opened! The PREVIOUS bar in sym_history[-1] has just fully CLOSED!
                                                if sym_history and len(sym_history) >= 30:
                                                    closed_bar = sym_history[-1]
                                                    s_start = cfg.get("session_start_utc", "08:00")
                                                    s_end = cfg.get("session_end_utc", "21:00")
                                                    in_session = (s_start <= cur_hm < s_end)

                                                    spec = assets.get(sym, cfg)

                                                    # Evaluate completed closed bars with asset-specific spec
                                                    signal, tag, eval_price, rsi_v, ema20_v, ema50_v = strategy.analyze_bars(sym_history, spec=spec, symbol=sym)

                                                    state.last_candle_times[sym] = closed_bar.get("timestamp", "")
                                                    state.last_candle_epoch = time.time()
                                                    state.last_rsi[sym] = rsi_v
                                                    state.last_ema20[sym] = ema20_v
                                                    state.last_ema50[sym] = ema50_v
                                                    state.last_signals[sym] = signal

                                                    logger.info(
                                                        f"[{sym}] 5m Candle [{state.last_candle_times[sym]}] closed @ {eval_price:.2f} | "
                                                        f"RSI: {rsi_v:.1f} | EMA20: {ema20_v:.2f} | EMA50: {ema50_v:.2f} | "
                                                        f"Signal: {signal}{f' ({tag})' if tag else ''}"
                                                    )

                                                    if signal in ["BUY", "SELL"]:
                                                        if state.is_paused:
                                                            logger.info(f"Signal for {sym} skipped: Trading is paused via Telegram.")
                                                        elif not in_session:
                                                            logger.info(f"Signal for {sym} skipped: outside session ({s_start}-{s_end} UTC, current: {cur_hm}).")
                                                        else:
                                                            await orders.execute_signal(signal, tag, current_price, symbol=sym, spec=spec)

                                                # Append newly started candle
                                                sym_history.append(b)
                                                if len(sym_history) > 150:
                                                    sym_history.pop(0)

                                            state.bars_loaded[sym] = len(sym_history)

            except (websockets.exceptions.ConnectionClosedOK, websockets.exceptions.ConnectionClosedError) as ws_err:
                logger.warning(f"Tradovate WS closed ({ws_err}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.warning("Market Data WS timeout. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as exc:
                logger.error(f"Tradovate Bot encountered error: {exc}. Reconnecting in 5s...", exc_info=True)
                now = time.time()
                if (now - last_error_alert) > 300: # Max 1 melding per 5 minuten
                    last_error_alert = now
                    if telegram_bot:
                        telegram_bot.notify_error("Bot Exception / Crash", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(5)
            finally:
                if md_keepalive_task and not md_keepalive_task.done():
                    md_keepalive_task.cancel()
                if orders:
                    await orders.disconnect()

    finally:
        if market_watchdog_task and not market_watchdog_task.done():
            market_watchdog_task.cancel()
        if orders:
            await orders.stop()
        if telegram_bot:
            await telegram_bot.stop()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Tradovate Bot stopped by user.")

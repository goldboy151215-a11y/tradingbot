import asyncio
import json
import logging
import websockets
from datetime import datetime, timezone
from telegram_alerts import TelegramAlerts

from websockets.protocol import State

logger = logging.getLogger(__name__)

class OrderManager:
    def __init__(self, config: dict, alerts: TelegramAlerts, auth=None):
        self.cfg = config
        self.alerts = alerts
        self.auth = auth
        self.ws_url = "wss://demo.tradovateapi.com/v1/websocket"
        self.ws = None
        self.msg_id = 0
        self.authorized = False
        self.token = None

        # State tracking
        self.open_position = None # {"side": "BUY"|"SELL", "entry_price": float, "qty": int, "sl_price": float, "tp_price": float, "breakeven_locked": bool, "tag": str, "open_time": str}
        self.daily_pnl = 0.0
        self.circuit_breaker_active = False
        self.cb_reason = ""
        self.cash_balance = 50000.0

        # Background tasks & request futures
        self.keepalive_task = None
        self.reader_task = None
        self.watchdog_task = None
        self.pending_requests: dict[int, asyncio.Future] = {}
        self._reconnect_lock = None
        self._should_run = True
        self._is_disconnecting = False

    @property
    def reconnect_lock(self):
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()
        return self._reconnect_lock

    def is_open(self) -> bool:
        return self.ws is not None and getattr(self.ws, "state", None) == State.OPEN

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def _keepalive(self):
        """Sends SockJS [] heartbeat every 2.5s to prevent Tradovate idle timeout."""
        try:
            while self.is_open():
                await asyncio.sleep(2.5)
                if self.is_open():
                    await self.ws.send("[]")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Order WS keepalive ended: {e}")

    async def _reader(self):
        """Dedicated background loop to consume frames, discard heartbeats, and resolve RPC calls."""
        try:
            while self.is_open():
                frame = await self.ws.recv()
                if frame == "h" or frame == "o":
                    continue
                if not frame.startswith("a"):
                    continue

                try:
                    data = json.loads(frame[1:])
                    for item in data:
                        req_id = item.get("i")
                        if req_id is not None and req_id in self.pending_requests:
                            fut = self.pending_requests[req_id]
                            if not fut.done():
                                fut.set_result(item)
                except Exception as parse_err:
                    logger.warning(f"Error parsing Order WS frame: {parse_err}")
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.info("Order WS connection closed.")
            self.authorized = False
            if self._should_run and not self._is_disconnecting:
                asyncio.create_task(self.ensure_connected())
        except Exception as e:
            logger.warning(f"Order WS reader exception: {e}")
            self.authorized = False
            if self._should_run and not self._is_disconnecting:
                asyncio.create_task(self.ensure_connected())

    async def ensure_connected(self, max_retries: int = 3) -> bool:
        """Ensures Order WS is connected and authorized. Automatically reconnects if dropped."""
        if self.is_open() and self.authorized:
            return True

        if not self._should_run:
            return False

        async with self.reconnect_lock:
            # Recheck inside lock
            if self.is_open() and self.authorized:
                return True

            for attempt in range(1, max_retries + 1):
                logger.warning(f"Order WS disconnected. Auto-reconnect attempt {attempt}/{max_retries}...")
                try:
                    # Refresh token via AuthManager if available
                    if self.auth:
                        try:
                            force_refresh = (attempt >= 2)
                            acc_tok, _ = await self.auth.get_tokens(force_refresh=force_refresh)
                            self.token = acc_tok
                        except Exception as auth_err:
                            logger.error(f"Failed to fetch token during reconnect: {auth_err}")

                    if not self.token:
                        logger.error("No token available for Order WS reconnect!")
                        return False

                    await self._connect_and_authorize_internal(self.token)
                    if self.is_open() and self.authorized:
                        logger.info("Order WS auto-reconnect successful!")
                        return True
                except Exception as e:
                    logger.error(f"Order WS reconnect attempt {attempt} failed: {e}")
                    await asyncio.sleep(2.0 * attempt)

            return False

    async def _watchdog(self):
        """Monitors Order WS health every 3s and restores connection proactively if dropped."""
        while self._should_run:
            try:
                await asyncio.sleep(3)
                if self._should_run and not self._is_disconnecting and self.token:
                    if not self.is_open() or not self.authorized:
                        logger.warning("Order WS watchdog detected offline state. Reconnecting...")
                        await self.ensure_connected()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Order WS watchdog tick error: {e}")

    def start_watchdog(self):
        if self.watchdog_task is None or self.watchdog_task.done():
            self._should_run = True
            self.watchdog_task = asyncio.create_task(self._watchdog())
            logger.info("Order WS watchdog started.")

    async def request(self, endpoint: str, payload = None, timeout: float = 10.0) -> dict:
        """Sends an RPC request over Order WS and awaits matching response."""
        if endpoint != "authorize" and (not self.is_open() or not self.authorized):
            logger.warning(f"Order WS closed before '{endpoint}', attempting instant reconnect...")
            ok = await self.ensure_connected()
            if not ok or not self.is_open():
                raise RuntimeError(f"Order WS is not connected for {endpoint}!")

        if not self.is_open():
            raise RuntimeError(f"Order WS is not connected for {endpoint}!")

        req_id = self.next_id()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_requests[req_id] = fut

        if isinstance(payload, str):
            body = f"\n\n{payload}"
        elif payload is not None:
            body = f"\n\n{json.dumps(payload)}"
        else:
            body = ""
        msg = f"{endpoint}\n{req_id}{body}"

        try:
            await self.ws.send(msg)
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res
        finally:
            self.pending_requests.pop(req_id, None)

    async def _internal_disconnect(self):
        self._is_disconnecting = True
        try:
            if self.keepalive_task and not self.keepalive_task.done():
                self.keepalive_task.cancel()
            if self.reader_task and not self.reader_task.done():
                self.reader_task.cancel()

            # Fail any pending requests
            for fut in list(self.pending_requests.values()):
                if not fut.done():
                    fut.cancel()
            self.pending_requests.clear()

            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
            self.authorized = False
        finally:
            self._is_disconnecting = False

    async def disconnect(self):
        await self._internal_disconnect()

    async def stop(self):
        self._should_run = False
        if self.watchdog_task and not self.watchdog_task.done():
            self.watchdog_task.cancel()
        await self._internal_disconnect()

    async def connect_and_authorize(self, token: str):
        self.token = token
        async with self.reconnect_lock:
            await self._connect_and_authorize_internal(token)

    async def _connect_and_authorize_internal(self, token: str):
        self.token = token
        await self._internal_disconnect()
        logger.info(f"Connecting to Order WS {self.ws_url}...")
        # Use ping_interval=None because Tradovate SockJS server does not respond to WS ping control frames
        self.ws = await websockets.connect(self.ws_url, ping_interval=None)

        # Receive open frame 'o'
        await self.ws.recv()

        # Start background reader task now that open frame is consumed
        self.reader_task = asyncio.create_task(self._reader())
        # Start keepalive task
        self.keepalive_task = asyncio.create_task(self._keepalive())

        # Authorize using request()
        res = await self.request("authorize", token, timeout=10.0)
        status_code = res.get("s", 0)
        if status_code != 200:
            raise RuntimeError(f"Order WS authorization failed: {res}")
        self.authorized = True
        logger.info(f"Order WS Authorized: {res}")

        # Sync cash balance
        await self.sync_balance()
        # Sync any existing open positions from Tradovate
        await self.sync_positions()

    async def sync_positions(self):
        try:
            res = await self.request("position/list")
            positions = res.get("d", [])
            has_active_pos = False
            for p in positions:
                if p.get("accountId") == self.cfg["account_id"]:
                    net_pos = int(p.get("netPos", 0))
                    if net_pos != 0:
                        has_active_pos = True
                        if self.open_position is None:
                            side = "BUY" if net_pos > 0 else "SELL"
                            entry_price = float(p.get("netPrice", 0.0))
                            sl_pts = self.cfg["stoploss_pts"]
                            tp_pts = self.cfg["takeprofit_pts"]
                            sl_price = round(entry_price - sl_pts if side == "BUY" else entry_price + sl_pts, 2)
                            tp_price = round(entry_price + tp_pts if side == "BUY" else entry_price - tp_pts, 2)
                            self.open_position = {
                                "side": side,
                                "entry_price": entry_price,
                                "qty": abs(net_pos),
                                "sl_price": sl_price,
                                "tp_price": tp_price,
                                "peak_price": entry_price,
                                "trailing_active": False,
                                "breakeven_locked": False,
                                "tag": "synced_existing_position",
                                "open_time": datetime.now(timezone.utc).isoformat()
                            }
                            logger.warning(f"Restored existing open position from Tradovate: {self.open_position}")
            if not has_active_pos and self.open_position is not None:
                logger.warning(f"Tradovate reports netPos=0. Clearing stale local position: {self.open_position}")
                self.open_position = None
        except Exception as e:
            logger.error(f"Error syncing positions: {e}")

    async def sync_balance(self):
        try:
            await self.sync_positions()
            res = await self.request("cashBalance/getcashbalancesnapshot", {"accountId": self.cfg["account_id"]})
            payload = res.get("d", {})
            self.cash_balance = float(payload.get("totalCashValue", 50000.0))
            self.daily_pnl = float(payload.get("realizedPnL", 0.0)) + float(payload.get("openPnL", 0.0))
            logger.info(f"Synced Balance: ${self.cash_balance:,.2f} | Today's PnL: ${self.daily_pnl:,.2f}")
            # Persist state to disk for external dashboards and Telegram bots
            try:
                state_data = {
                    "cash_balance": self.cash_balance,
                    "daily_pnl": self.daily_pnl,
                    "starting_balance": float(self.cfg.get("starting_balance", 50000.0)),
                    "profit_buffer": self.cash_balance - float(self.cfg.get("starting_balance", 50000.0)),
                    "symbol": self.cfg.get("symbol", "MNQZ6"),
                    "open_position": self.open_position,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
                with open("/root/tradovate_bot/state.json", "w") as sf:
                    json.dump(state_data, sf, indent=2)
            except Exception as se:
                logger.debug(f"Could not persist state.json: {se}")
        except Exception as e:
            logger.error(f"Error syncing balance: {e}")

    def check_circuit_breakers(self) -> bool:
        starting_bal = float(self.cfg.get("starting_balance", 50000.0))
        target_profit = float(self.cfg.get("profit_target_usd", 2500.0))
        max_overall_loss = float(self.cfg.get("max_overall_loss_usd", 1500.0))
        max_daily_loss = float(self.cfg.get("max_daily_loss_usd", 400.0))
        max_daily_profit = float(self.cfg.get("max_daily_profit_usd", 950.0))

        # 1. Total Target Reached! (FundedNext $2,500 Target hit)
        total_pnl = self.cash_balance - starting_bal
        if total_pnl >= target_profit:
            self.circuit_breaker_active = True
            self.cb_reason = f"🎉 PROFIT TARGET BEREIKT (+${total_pnl:,.2f} / +${target_profit:,.2f})! Challenge Gehaald!"
            logger.info(f"TARGET PASSED: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 2. Overall Max Loss Protection (FundedNext $1,500 max loss floor: $48,500)
        # Buffer of $100 before hard breach
        if total_pnl <= -(max_overall_loss - 100.0):
            self.circuit_breaker_active = True
            self.cb_reason = f"Max Overall Loss Beveiliging (-${abs(total_pnl):,.2f} / -${max_overall_loss:,.2f})"
            logger.warning(f"CIRCUIT BREAKER: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 3. Max Daily Loss
        if self.daily_pnl <= -max_daily_loss:
            self.circuit_breaker_active = True
            self.cb_reason = f"Max Dagverlies Bereikt (-${abs(self.daily_pnl):,.2f} / -${max_daily_loss:,.2f})"
            logger.warning(f"CIRCUIT BREAKER: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 4. 40% Consistency Cap (Max profit in 1 single day)
        if self.daily_pnl >= max_daily_profit:
            self.circuit_breaker_active = True
            self.cb_reason = f"40% Consistentie Winstcap Bereikt (+${self.daily_pnl:,.2f} / +${max_daily_profit:,.2f})"
            logger.info(f"PROFIT CAP LOCK: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        return False

    async def execute_signal(self, side: str, tag: str, current_price: float, symbol: str = None, spec: dict = None):
        if self.open_position:
            active_sym = self.open_position.get('symbol', self.cfg['symbol'])
            target_sym = symbol or self.cfg['symbol']
            logger.info(f"1-Position Protection: Trade already active on {active_sym} ({self.open_position['side']}), skipping new signal on {target_sym}")
            return

        if self.circuit_breaker_active:
            logger.info(f"Trading blocked by circuit breaker: {self.cb_reason}")
            return

        sym = symbol or self.cfg["symbol"]
        spec = spec or self.cfg.get("assets", {}).get(sym, self.cfg)
        qty = spec.get("max_contracts", self.cfg.get("max_contracts", 5))
        sl_pts = spec.get("stoploss_pts", self.cfg.get("stoploss_pts", 30.0))
        tp_pts = spec.get("takeprofit_pts", self.cfg.get("takeprofit_pts", 50.0))
        point_val = spec.get("point_value", self.cfg.get("point_value", 2.0))

        if not self.is_open() or not self.authorized:
            logger.warning(f"Order WS not connected when signal {side} arrived! Forcing immediate reconnect...")
            ok = await self.ensure_connected(max_retries=2)
            if not ok or not self.is_open():
                logger.error(f"Failed to place order for {side}: Order WS could not reconnect!")
                return

        if side == "BUY":
            sl_price = round(current_price - sl_pts, 2)
            tp_price = round(current_price + tp_pts, 2)
        else:
            sl_price = round(current_price + sl_pts, 2)
            tp_price = round(current_price - tp_pts, 2)

        order_payload = {
            "accountSpec": self.cfg["account_spec"],
            "accountId": self.cfg["account_id"],
            "action": side.capitalize(),
            "symbol": sym,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "timeInForce": "Day"
        }

        logger.info(f"Placing order to Tradovate: {order_payload}")
        try:
            res = await self.request("order/placeorder", order_payload, timeout=10.0)
            logger.info(f"Order response: {res}")
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return

        # Record position
        self.open_position = {
            "symbol": sym,
            "side": side.upper(),
            "entry_price": current_price,
            "qty": qty,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "point_value": point_val,
            "peak_price": current_price,
            "trailing_active": False,
            "breakeven_locked": False,
            "tag": tag,
            "open_time": datetime.now(timezone.utc).isoformat()
        }

        self.alerts.notify_entry(side, sym, current_price, qty, sl_price, tp_price, tag, point_val=point_val)

    async def check_open_position(self, current_price: float, symbol: str = None):
        if not self.open_position:
            return

        pos = self.open_position
        if symbol and pos.get("symbol") != symbol:
            return

        side = pos["side"]
        entry = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        sym = pos.get("symbol", self.cfg["symbol"])

        profit_pts = (current_price - entry) if side == "BUY" else (entry - current_price)

        # 1. Take Profit hit
        if (side == "BUY" and current_price >= tp) or (side == "SELL" and current_price <= tp):
            logger.info(f"Take Profit HIT on {sym}! Price: {current_price}")
            await self.close_position(current_price, "take_profit")
            return

        # 2. Stoploss / Trailing Stop hit
        if (side == "BUY" and current_price <= sl) or (side == "SELL" and current_price >= sl):
            reason = "trailing_stop" if pos.get("trailing_active") else "stop_loss"
            logger.info(f"{reason.upper()} HIT on {sym}! Price: {current_price} (SL: {sl:.2f})")
            await self.close_position(current_price, reason)
            return

        # 3. Breakeven Lock (Pas naar entry zodra +30 punten winst aangetikt is)
        spec = self.cfg.get("assets", {}).get(sym, self.cfg)
        be_trigger = float(spec.get("breakeven_trigger_pts", self.cfg.get("breakeven_trigger_pts", 30.0)))

        if not pos.get("breakeven_locked", False) and profit_pts >= be_trigger:
            pos["breakeven_locked"] = True
            pos["sl_price"] = entry
            logger.info(f"Breakeven geactiveerd voor {sym}! +{profit_pts:.2f} pts bereikt. Stoploss verplaatst naar Entry: {entry:.2f}")
            self.alerts.notify_breakeven(sym, entry, profit_pts)

    async def close_position(self, exit_price: float, reason: str):
        if not self.open_position:
            return

        pos = self.open_position
        sym = pos.get("symbol", self.cfg["symbol"])
        qty = pos.get("qty", self.cfg.get("max_contracts", 5))
        pv = pos.get("point_value", self.cfg.get("point_value", 2.0))

        if not self.is_open() or not self.authorized:
            logger.warning(f"Order WS not connected for position close ({reason})! Forcing immediate reconnect...")
            await self.ensure_connected(max_retries=2)

        close_side = "Sell" if pos["side"] == "BUY" else "Buy"
        order_payload = {
            "accountSpec": self.cfg["account_spec"],
            "accountId": self.cfg["account_id"],
            "action": close_side,
            "symbol": sym,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "timeInForce": "Day"
        }

        logger.info(f"Sending position close to Tradovate: {order_payload}")
        try:
            res = await self.request("order/placeorder", order_payload, timeout=10.0)
            logger.info(f"Close order response: {res}")
        except Exception as e:
            logger.error(f"Failed to place close order: {e}")

        profit_pts = (exit_price - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - exit_price)
        pnl_usd = profit_pts * pv * qty
        self.daily_pnl += pnl_usd

        self.alerts.notify_exit(pos["side"], sym, pos["entry_price"], exit_price, qty, pnl_usd, reason)
        self.open_position = None

        # Check circuit breakers after trade close
        self.check_circuit_breakers()

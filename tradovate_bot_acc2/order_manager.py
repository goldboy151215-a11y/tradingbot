import asyncio
import json
import logging
from datetime import datetime, timezone
import websockets
from websockets.protocol import State

logger = logging.getLogger(__name__)

class OrderManagerAcc2:
    def __init__(self, config: dict, alerts, auth=None):
        self.cfg = config
        self.alerts = alerts
        self.auth = auth
        self.ws_url = "wss://demo.tradovateapi.com/v1/websocket"
        self.ws = None
        self.authorized = False
        self.token = None
        self.msg_id = 0

        # State
        self.cash_balance = float(config.get("starting_balance", 50000.0))
        self.daily_pnl = 0.0
        self.open_position = None
        self.circuit_breaker_active = False
        self.cb_reason = ""

        # Background tasks
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
        try:
            while self.is_open():
                await asyncio.sleep(2.5)
                if self.is_open():
                    await self.ws.send("[]")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[Acc2] Order WS keepalive ended: {e}")

    async def _reader(self):
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
                    logger.warning(f"[Acc2] Error parsing Order WS frame: {parse_err}")
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.info("[Acc2] Order WS connection closed.")
            self.authorized = False
            if self._should_run and not self._is_disconnecting:
                asyncio.create_task(self.ensure_connected())
        except Exception as e:
            logger.warning(f"[Acc2] Order WS reader exception: {e}")
            self.authorized = False
            if self._should_run and not self._is_disconnecting:
                asyncio.create_task(self.ensure_connected())

    async def ensure_connected(self, max_retries: int = 3) -> bool:
        if self.is_open() and self.authorized:
            return True

        if not self._should_run:
            return False

        async with self.reconnect_lock:
            if self.is_open() and self.authorized:
                return True

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"[Acc2] Attempting to reconnect Order WS (Attempt {attempt}/{max_retries})...")
                    if self.auth:
                        self.token, _ = await self.auth.get_tokens()
                    await self._connect_and_authorize_internal(self.token)
                    logger.info("[Acc2] Successfully reconnected Order WS!")
                    return True
                except Exception as e:
                    logger.warning(f"[Acc2] Order WS reconnect attempt {attempt} failed: {e}")
                    await asyncio.sleep(2 * attempt)
            return False

    def start_watchdog(self):
        if not self.watchdog_task or self.watchdog_task.done():
            self.watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self):
        while self._should_run:
            try:
                await asyncio.sleep(15)
                if self._should_run and (not self.is_open() or not self.authorized):
                    logger.info("[Acc2] Watchdog detected Order WS down. Reconnecting...")
                    await self.ensure_connected(max_retries=2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Acc2] Watchdog loop error: {e}")

    async def request(self, endpoint: str, payload=None, timeout: float = 8.0) -> dict:
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
        logger.info(f"[Acc2] Connecting to Order WS {self.ws_url}...")
        self.ws = await websockets.connect(self.ws_url, ping_interval=None)
        await self.ws.recv()

        self.reader_task = asyncio.create_task(self._reader())
        self.keepalive_task = asyncio.create_task(self._keepalive())

        res = await self.request("authorize", token, timeout=10.0)
        if res.get("s", 0) != 200:
            raise RuntimeError(f"[Acc2] Order WS authorization failed: {res}")
        self.authorized = True
        logger.info(f"[Acc2] Order WS Authorized: {res}")

        await self.sync_balance()

    async def sync_balance(self):
        try:
            res = await self.request("cashBalance/getcashbalancesnapshot", {"accountId": self.cfg.get("account_id", 0)})
            payload = res.get("d", {})
            self.cash_balance = float(payload.get("totalCashValue", self.cfg.get("starting_balance", 50000.0)))
            self.daily_pnl = float(payload.get("realizedPnL", 0.0)) + float(payload.get("openPnL", 0.0))
            logger.info(f"[Acc2] Synced Balance: ${self.cash_balance:,.2f} | Today's PnL: ${self.daily_pnl:,.2f}")

            try:
                state_data = {
                    "account": self.cfg.get("account_spec", "Acc2"),
                    "cash_balance": self.cash_balance,
                    "daily_pnl": self.daily_pnl,
                    "starting_balance": float(self.cfg.get("starting_balance", 50000.0)),
                    "open_position": self.open_position,
                    "circuit_breaker_active": self.circuit_breaker_active,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
                with open("/root/tradovate_bot_acc2/state.json", "w") as sf:
                    json.dump(state_data, sf, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[Acc2] Error syncing balance: {e}")

    def check_circuit_breakers(self) -> bool:
        starting_bal = float(self.cfg.get("starting_balance", 50000.0))
        target_profit = float(self.cfg.get("profit_target_usd", 2500.0))
        max_overall_loss = float(self.cfg.get("max_overall_loss_usd", 1500.0))
        max_daily_loss = float(self.cfg.get("max_daily_loss_usd", 400.0))
        max_daily_profit = float(self.cfg.get("max_daily_profit_usd", 900.0))

        total_pnl = self.cash_balance - starting_bal

        # 1. Total Target Reached! (FundedNext $2,500 Target hit)
        if total_pnl >= target_profit:
            self.circuit_breaker_active = True
            self.cb_reason = f"🎉 PROFIT TARGET BEREIKT (+${total_pnl:,.2f} / +${target_profit:,.2f})! Challenge Gehaald!"
            logger.info(f"[Acc2] TARGET PASSED: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 2. Overall Max Loss Protection ($1,500 max loss floor)
        if total_pnl <= -(max_overall_loss - 100.0):
            self.circuit_breaker_active = True
            self.cb_reason = f"Max Overall Loss Beveiliging (-${abs(total_pnl):,.2f} / -${max_overall_loss:,.2f})"
            logger.warning(f"[Acc2] CIRCUIT BREAKER: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 3. Max Daily Loss ($400 limit)
        if self.daily_pnl <= -max_daily_loss:
            self.circuit_breaker_active = True
            self.cb_reason = f"Max Dagverlies Bereikt (-${abs(self.daily_pnl):,.2f} / -${max_daily_loss:,.2f})"
            logger.warning(f"[Acc2] CIRCUIT BREAKER: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        # 4. Consistency Daily Profit Cap ($900 limit)
        if self.daily_pnl >= max_daily_profit:
            self.circuit_breaker_active = True
            self.cb_reason = f"FundedNext Winstcap Bereikt (+${self.daily_pnl:,.2f} / +${max_daily_profit:,.2f})"
            logger.info(f"[Acc2] PROFIT CAP LOCK: {self.cb_reason}")
            self.alerts.notify_circuit_breaker(self.cb_reason, self.daily_pnl)
            return True

        return False

    async def execute_signal(self, side: str, tag: str, current_price: float, symbol: str, spec: dict):
        if self.open_position:
            logger.info(f"[Acc2] Position already active ({self.open_position['side']} {self.open_position['symbol']}), skipping new signal.")
            return

        if self.circuit_breaker_active:
            logger.info(f"[Acc2] Trading blocked by circuit breaker: {self.cb_reason}")
            return

        qty = spec.get("max_contracts", 5)
        sl_pts = spec.get("stoploss_pts", 25.0)
        tp_pts = spec.get("takeprofit_pts", 50.0)
        point_val = spec.get("point_value", 2.0)

        if not self.is_open() or not self.authorized:
            ok = await self.ensure_connected(max_retries=2)
            if not ok or not self.is_open():
                logger.error(f"[Acc2] Could not place order: Order WS offline!")
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
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "timeInForce": "Day"
        }

        logger.info(f"[Acc2] Placing order to Tradovate: {order_payload}")
        try:
            res = await self.request("order/placeorder", order_payload, timeout=10.0)
            logger.info(f"[Acc2] Order response: {res}")
        except Exception as e:
            logger.error(f"[Acc2] Failed to place order: {e}")
            return

        self.open_position = {
            "symbol": symbol,
            "side": side.upper(),
            "entry_price": current_price,
            "qty": qty,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "point_value": point_val,
            "peak_price": current_price,
            "breakeven_locked": False,
            "tag": tag,
            "open_time": datetime.now(timezone.utc).isoformat()
        }

        self.alerts.notify_entry(side, symbol, current_price, qty, sl_price, tp_price, tag, point_val=point_val)

    async def check_open_position(self, current_price: float, symbol: str, spec: dict = None):
        if not self.open_position or self.open_position.get("symbol") != symbol:
            return

        pos = self.open_position
        side = pos["side"]
        entry = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]

        profit_pts = (current_price - entry) if side == "BUY" else (entry - current_price)

        # 1. Take Profit hit
        if (side == "BUY" and current_price >= tp) or (side == "SELL" and current_price <= tp):
            logger.info(f"[Acc2] Take Profit HIT on {symbol}! Exit @ {current_price}")
            await self.close_position(current_price, "take_profit")
            return

        # 2. Stop Loss hit
        if (side == "BUY" and current_price <= sl) or (side == "SELL" and current_price >= sl):
            reason = "breakeven_exit" if pos.get("breakeven_locked") else "stop_loss"
            logger.info(f"[Acc2] {reason.upper()} HIT on {symbol}! Exit @ {current_price} (SL: {sl:.2f})")
            await self.close_position(current_price, reason)
            return

        # 3. Breakeven Lock (Pas naar entry zodra +30 punten winst aangetikt is)
        spec = spec or {}
        be_trigger = float(spec.get("breakeven_trigger_pts", self.cfg.get("breakeven_trigger_pts", 30.0)))

        if not pos.get("breakeven_locked", False) and profit_pts >= be_trigger:
            pos["breakeven_locked"] = True
            pos["sl_price"] = entry
            logger.info(f"[Acc2] Breakeven geactiveerd voor {symbol}! +{profit_pts:.2f} pts bereikt. Stoploss verplaatst naar Entry: {entry:.2f}")
            self.alerts.notify_breakeven(symbol, entry, profit_pts)

    async def close_position(self, exit_price: float, reason: str):
        if not self.open_position:
            return

        pos = self.open_position
        sym = pos["symbol"]
        qty = pos["qty"]
        pv = pos["point_value"]

        if not self.is_open() or not self.authorized:
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

        try:
            res = await self.request("order/placeorder", order_payload, timeout=10.0)
            logger.info(f"[Acc2] Close order response: {res}")
        except Exception as e:
            logger.error(f"[Acc2] Failed to close position: {e}")

        profit_pts = (exit_price - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - exit_price)
        pnl_usd = profit_pts * pv * qty
        self.daily_pnl += pnl_usd

        self.alerts.notify_exit(pos["side"], sym, pos["entry_price"], exit_price, qty, pnl_usd, reason)
        self.open_position = None

        self.check_circuit_breakers()

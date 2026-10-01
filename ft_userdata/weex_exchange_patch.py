#!/usr/bin/env python3
"""
Weex Exchange Patch voor Freqtrade
Injecteert de Weex exchange klasse in freqtrade.exchange zodat
futures trading met cross margin & isolated margin stabiel werkt op Weex.
"""
import os
import sys

FREQTRADE_EXCHANGE_PATH = "/freqtrade/freqtrade/exchange"
WEEX_CLASS_FILE = os.path.join(FREQTRADE_EXCHANGE_PATH, "weex.py")
EXCHANGE_INIT = os.path.join(FREQTRADE_EXCHANGE_PATH, "__init__.py")

WEEX_CLASS_CODE = '''"""
Weex exchange subclass voor Freqtrade.
Voegt futures/swap trading ondersteuning toe via CCXT.
"""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import ccxt

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas

logger = logging.getLogger(__name__)

# Patch ccxt.weex.handle_errors to prevent treating {"msg": "success", "code": "200"} as an exception
_orig_weex_handle_errors = ccxt.weex.handle_errors

def _patched_weex_handle_errors(self, code: int, reason: str, url: str, method: str, headers: dict, body: str, response, requestHeaders, requestBody):
    if response and isinstance(response, dict):
        msg = str(response.get('msg', '')).lower()
        err_code = str(response.get('code', ''))
        if msg in ['success', 'ok'] or err_code in ['0', '00000', '200']:
            return None
    return _orig_weex_handle_errors(self, code, reason, url, method, headers, body, response, requestHeaders, requestBody)

ccxt.weex.handle_errors = _patched_weex_handle_errors


class Weex(Exchange):
    """
    Weex exchange klasse voor Freqtrade.
    Ondersteunt USDT-M perpetual futures (swap markets) met cross/isolated margin.
    """

    _ft_has: FtHas = {
        "ohlcv_has_history": True,
        "order_time_in_force": ["GTC", "FOK", "IOC"],
        "ws_enabled": False,
        "exchange_has_overrides": {
            "fetchL2OrderBook": False,
            "fetchOrders": False,
            "fetchClosedOrders": False,
        },
    }

    _ft_has_futures: FtHas = {
        "ohlcv_has_history": True,
        "stoploss_on_exchange": False,
    }

    _supported_trading_mode_margin_pairs = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    def _get_rate_from_ticker(
        self, side, ticker, conf_strategy, price_side
    ):
        ticker_rate = ticker.get(price_side)
        if ticker_rate is None or ticker_rate == 0:
            ticker_rate = ticker.get("last")
        if ticker.get("last") and ticker_rate:
            if side == "entry" and ticker_rate > ticker["last"]:
                balance = conf_strategy.get("price_last_balance", 0.0)
                ticker_rate = ticker_rate + balance * (ticker["last"] - ticker_rate)
            elif side == "exit" and ticker_rate < ticker["last"]:
                balance = conf_strategy.get("price_last_balance", 0.0)
                ticker_rate = ticker_rate - balance * (ticker_rate - ticker["last"])
        return ticker_rate

    def get_max_leverage(self, pair: str, stake_amount: float | None) -> float:
        market = self.markets.get(pair, {})
        lev_max = market.get("limits", {}).get("leverage", {}).get("max")
        if lev_max:
            return float(lev_max)
        return 50.0

    def get_contract_size(self, pair: str) -> float:
        """WEEX CCXT expects base currency quantity, not contract multipliers."""
        return 1.0

    def _amount_to_contracts(self, pair: str, amount: float) -> float:
        return amount

    def _contracts_to_amount(self, pair: str, amount: float) -> float:
        return amount

    def get_funding_fees(
        self, pair: str, amount: float, is_short: bool, open_date
    ) -> float:
        """Weex CCXT driver ondersteunt fetch_funding_history niet. Voorkom crash loop."""
        return 0.0

    def create_order(
        self,
        *,
        pair: str,
        ordertype: str,
        side: Any,
        amount: float,
        rate: float,
        leverage: float,
        time_in_force: str = "GTC",
        reduceOnly: bool = False,
        initial_order: bool = True,
    ) -> CcxtOrder:
        if reduceOnly and not self._config.get("dry_run", False):
            # Check actual live open position on WEEX before sending reduceOnly exit order
            try:
                positions = self._api.fetch_positions([pair])
                matching = [p for p in positions if p.get('symbol') == pair and abs(p.get('contracts', 0) or 0) > 0]
                if not matching:
                    logger.warning(f"Weex: Position for {pair} is already closed on exchange. Gracefully simulating fill.")
                    now_ts = int(datetime.now(timezone.utc).timestamp())
                    return {
                        "id": f"syn_{now_ts}",
                        "order_id": f"syn_{now_ts}",
                        "symbol": pair,
                        "type": ordertype,
                        "side": side,
                        "price": rate,
                        "average": rate,
                        "amount": amount,
                        "filled": amount,
                        "remaining": 0.0,
                        "status": "closed",
                        "timestamp": now_ts * 1000,
                        "datetime": datetime.now(timezone.utc).isoformat(),
                        "fee": None,
                        "info": {},
                    }
                actual_pos_contracts = abs(float(matching[0].get('contracts', amount)))
                if actual_pos_contracts < amount:
                    logger.info(f"Weex: Adjusting exit amount from {amount} to actual position size {actual_pos_contracts}")
                    amount = actual_pos_contracts
            except Exception as e:
                logger.warning(f"Weex: Pre-exit position check error: {e}")

        try:
            return super().create_order(
                pair=pair,
                ordertype=ordertype,
                side=side,
                amount=amount,
                rate=rate,
                leverage=leverage,
                time_in_force=time_in_force,
                reduceOnly=reduceOnly,
                initial_order=initial_order,
            )
        except Exception as exc:
            # Fallback for exit orders (stoploss / take profit) if validation fails on Weex
            if reduceOnly and not self._config.get("dry_run", False):
                logger.warning(f"Weex: Regular exit order failed ({exc}). Falling back to direct /capi/v3/closePositions...")
                try:
                    market = self.markets.get(pair, {})
                    sym_id = market.get("id", pair.replace("/", "").split(":")[0])
                    res = self._api.contractPrivatePostCapiV3ClosePositions({"symbol": sym_id})
                    logger.info(f"Weex: Successfully closed position for {pair} via direct closePositions: {res}")
                    now_ts = int(datetime.now(timezone.utc).timestamp())
                    return {
                        "id": f"closepos_{now_ts}",
                        "order_id": f"closepos_{now_ts}",
                        "symbol": pair,
                        "type": ordertype,
                        "side": side,
                        "price": rate,
                        "average": rate,
                        "amount": amount,
                        "filled": amount,
                        "remaining": 0.0,
                        "status": "closed",
                        "timestamp": now_ts * 1000,
                        "datetime": datetime.now(timezone.utc).isoformat(),
                        "fee": None,
                        "info": {},
                    }
                except Exception as fallback_exc:
                    logger.error(f"Weex: Direct closePositions fallback failed: {fallback_exc}")
            raise exc

    def fetch_order(self, order_id: str, pair: str, params: dict | None = None) -> CcxtOrder:
        """Intercept synthetic order IDs (closepos_*, syn_*) to prevent infinite fetch_order error loops.

        When the Weex fallback creates a synthetic order (non-integer ID), Freqtrade's
        update_trades_without_assigned_fees() will try to fetch that order repeatedly and fail
        because WEEX only accepts integer order IDs. We intercept those here and return a
        pre-filled closed order so Freqtrade marks the fee as updated and moves on.
        """
        order_id_str = str(order_id)
        if order_id_str.startswith("closepos_") or order_id_str.startswith("syn_"):
            logger.info(
                f"Weex: Intercepted synthetic order {order_id_str} for {pair}. "
                f"Returning pre-filled closed order to prevent fetch loop."
            )
            now_ts = int(datetime.now(timezone.utc).timestamp())
            return {
                "id": order_id_str,
                "order_id": order_id_str,
                "symbol": pair,
                "type": "market",
                "side": "sell",
                "price": 0.0,
                "average": 0.0,
                "amount": 0.0,
                "filled": 0.0,
                "remaining": 0.0,
                "cost": 0.0,
                "status": "closed",
                "timestamp": now_ts * 1000,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "fee": {"cost": 0.0, "currency": "USDT", "rate": 0.0002},
                "fees": [{"cost": 0.0, "currency": "USDT", "rate": 0.0002}],
                "info": {},
            }
        return super().fetch_order(order_id, pair, params)

    def _fetch_orders(self, pair: str, since: datetime, params: dict | None = None) -> list[CcxtOrder]:
        try:
            return super()._fetch_orders(pair, since, params)
        except Exception as e:
            logger.warning(f"Weex: _fetch_orders failed ({e}), returning empty list to prevent stuck recovery loop.")
            return []
'''

def inject_weex_class():
    # 1. Schrijf weex.py
    with open(WEEX_CLASS_FILE, "w") as f:
        f.write(WEEX_CLASS_CODE)
    print(f"[OK] Weex exchange klasse geschreven naar {WEEX_CLASS_FILE}")

    # 2. Voeg import toe aan __init__.py als dat nog niet bestaat
    with open(EXCHANGE_INIT, "r") as f:
        init_content = f.read()

    if "from freqtrade.exchange.weex import Weex" not in init_content:
        with open(EXCHANGE_INIT, "a") as f:
            f.write("\nfrom freqtrade.exchange.weex import Weex  # Weex custom subclass\n")
        print(f"[OK] Weex import toegevoegd aan {EXCHANGE_INIT}")
    else:
        print(f"[OK] Weex import al aanwezig in {EXCHANGE_INIT}")

if __name__ == "__main__":
    inject_weex_class()
    print("[KLAAR] Weex exchange patch succesvol toegepast!")

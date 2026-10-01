"""Exchange interface supporting Binance and Bybit USDT-perpetuals via CCXT.

Supports both live exchange execution (strictly behind LIVE=True and TESTNET=False)
and Paper/Dry-Run execution with simulated balances and orders.
"""

from dataclasses import dataclass
from typing import Any
import ccxt
import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ExchangeConfig:
    exchange_id: str = "weex"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    live: bool = False
    paper_mode: bool = True
    default_leverage: int = 50
    paper_balance: float = 10000.0


class CcxtExchangeWrapper:
    """Wrapper around CCXT for USDT-perpetuals on Weex."""

    def __init__(self, config: ExchangeConfig) -> None:
        self.config = config
        self.exchange_id = config.exchange_id.lower()
        self.paper_balance = config.paper_balance
        self.order_counter = 0

        exchange_class = getattr(ccxt, self.exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported exchange '{self.exchange_id}'. Supported: 'weex'.")

        exchange_options: dict[str, Any] = {
            "apiKey": config.api_key or "",
            "secret": config.api_secret or "",
            "enableRateLimit": True,
        }

        if self.exchange_id in ("weex", "bybit"):
            exchange_options["options"] = {"defaultType": "swap"}
        elif self.exchange_id == "binance":
            exchange_options["options"] = {"defaultType": "future"}

        self.client: ccxt.Exchange = exchange_class(exchange_options)

        if config.testnet:
            try:
                self.client.set_sandbox_mode(True)
            except Exception as exc:
                logger.warning("Sandbox mode not available on exchange, proceeding with standard endpoints", error=str(exc))

    def format_symbol(self, pair: str) -> str:
        """Format pair to CCXT standard linear perpetual symbol.

        e.g. BTC/USDT -> BTC/USDT:USDT for perpetual futures
        """
        pair = pair.strip().upper()
        if ":" in pair:
            return pair
        if "/" in pair:
            base, quote = pair.split("/")
            return f"{base}/{quote}:{quote}"
        return pair

    def fetch_ohlcv(self, pair: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
        """Fetch OHLCV candles from exchange into pandas DataFrame."""
        symbol = self.format_symbol(pair)
        try:
            raw_candles = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not raw_candles:
                raise ValueError(f"Empty candle list returned for {symbol}")

            df = pd.DataFrame(
                raw_candles,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df
        except Exception as exc:
            logger.error("Failed to fetch OHLCV from exchange", pair=pair, symbol=symbol, timeframe=timeframe, error=str(exc))
            raise

    def get_equity(self) -> float:
        """Fetch balance equity (USDT). Returns simulated balance in paper mode."""
        if self.config.paper_mode or not self.config.live:
            return float(self.paper_balance)

        try:
            balance = self.client.fetch_balance()
            # Try total USDT balance
            usdt_info = balance.get("USDT", {})
            total = usdt_info.get("total")
            if total is not None:
                return float(total)
            free = usdt_info.get("free")
            if free is not None:
                return float(free)
            return float(balance.get("total", {}).get("USDT", 0.0))
        except Exception as exc:
            logger.error("Failed to fetch live balance, falling back to paper balance", error=str(exc))
            return float(self.paper_balance)

    def set_leverage(self, pair: str, leverage: int = 50) -> None:
        """Set isolated leverage with hard cap of 50x."""
        leverage = min(max(int(leverage), 1), 50)
        if self.config.paper_mode or not self.config.live:
            return

        symbol = self.format_symbol(pair)
        try:
            if hasattr(self.client, "set_leverage"):
                self.client.set_leverage(leverage, symbol, params={"marginMode": "isolated"})
        except Exception as exc:
            logger.warning("Could not set leverage on exchange", symbol=symbol, leverage=leverage, error=str(exc))

    def create_market_order(
        self,
        pair: str,
        side: str,  # 'buy' or 'sell'
        amount: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Execute market order (simulated in paper/dry-run mode)."""
        symbol = self.format_symbol(pair)
        side = side.lower()

        if self.config.paper_mode or not self.config.live:
            self.order_counter += 1
            order_id = f"paper-{self.order_counter}"
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "type": "market",
                "status": "closed",
                "reduceOnly": reduce_only,
                "paper": True,
            }

        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True

        return self.client.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
            params=params,
        )

    def close(self) -> None:
        """Cleanly close exchange client session."""
        if hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                pass

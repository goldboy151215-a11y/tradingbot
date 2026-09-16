"""Tests for exchange wrapper and runner integration."""

import os
from pathlib import Path
import pytest
from src.exchange import CcxtExchangeWrapper, ExchangeConfig
from src.runner import TradingRunner, generate_synthetic_ohlcv


def test_exchange_initialization_defaults():
    """Verify exchange wrapper initializes cleanly with default paper settings."""
    cfg = ExchangeConfig(
        exchange_id="binance",
        paper_mode=True,
        live=False,
        testnet=True,
    )
    ex = CcxtExchangeWrapper(cfg)
    assert ex.get_equity() == 10000.0

    # Test market order in paper mode
    order = ex.create_market_order("BTC/USDT", "buy", 0.1)
    assert order["paper"] is True
    assert order["side"] == "buy"
    assert order["amount"] == 0.1
    ex.close()


def test_bybit_exchange_initialization():
    """Verify Bybit swap configuration."""
    cfg = ExchangeConfig(
        exchange_id="bybit",
        paper_mode=True,
        live=False,
        testnet=True,
    )
    ex = CcxtExchangeWrapper(cfg)
    assert ex.exchange_id == "bybit"
    ex.close()


def test_synthetic_ohlcv_generation():
    """Verify synthetic OHLCV generator generates valid swing highs and lows."""
    df = generate_synthetic_ohlcv(base_level=100.0, span=50.0, n_candles=100)
    assert len(df) == 100
    assert "high" in df.columns
    assert "low" in df.columns
    assert df["high"].max() > df["low"].min()


def test_trading_runner_demo_execution(tmp_path):
    """Verify trading runner executes demo mode without errors and produces state.json."""
    state_file = tmp_path / "test_state.json"
    runner = TradingRunner(
        config_path=".env.example",
        state_file=str(state_file),
        offline_demo=True,
    )
    runner.pairs = ["BTC/USDT", "ETH/USDT"]
    runner.run_once()

    assert state_file.exists()
    assert runner.exchange.get_equity() > 0.0

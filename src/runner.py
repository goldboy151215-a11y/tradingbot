"""Runtime loop and orchestrator for the Dealing Range + Fibonacci POI Trading Bot.

Loads configuration from .env, evaluates 4H bias swings, computes dynamic
Fibonacci POI zones, processes execution timeframe signals, manages risk,
tracks open trades and persists state safely into state.json.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
from dotenv import load_dotenv
import numpy as np
import pandas as pd
import structlog

from src.exchange import CcxtExchangeWrapper, ExchangeConfig
from src.risk import (
    ActivePosition,
    PositionSizingResult,
    RiskManager,
    RiskParameters,
    TradeState,
    calculate_position_size,
)
from src.strategy import (
    DealingRange,
    FibonacciZones,
    SignalResult,
    SignalType,
    calculate_atr,
    calculate_rsi,
    calculate_working_range,
    calculate_zones,
    detect_pivots,
    evaluate_signal,
    is_trade_invalidated_by_range,
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()


def load_state(filepath: Path, risk_manager: RiskManager) -> None:
    """Load persisted trading state from JSON file."""
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            risk_manager.from_dict(data)
            logger.info("State loaded successfully from file", path=str(filepath))
        except Exception as exc:
            logger.error("Failed to load state file, starting fresh", path=str(filepath), error=str(exc))


def save_state(filepath: Path, risk_manager: RiskManager) -> None:
    """Atomically save trading state to JSON file."""
    try:
        temp_file = filepath.with_suffix(".tmp")
        data = risk_manager.to_dict()
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_file.replace(filepath)
    except Exception as exc:
        logger.error("Failed to save state", path=str(filepath), error=str(exc))


def generate_synthetic_ohlcv(
    base_level: float = 100.0,
    span: float = 50.0,
    n_candles: int = 150,
) -> pd.DataFrame:
    """Generate deterministic synthetic candles with swing highs and lows for offline demo."""
    timestamps = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=n_candles,
        freq="4h",
    )
    t = np.linspace(0, 4 * np.pi, n_candles)
    # Sine wave creates genuine swing highs and lows
    closes = base_level + (span / 2.0) * np.sin(t)
    highs = closes + (span * 0.05)
    lows = closes - (span * 0.05)
    opens = closes.copy()
    opens[1:] = closes[:-1]

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.ones(n_candles) * 100.0,
    })


class TradingRunner:
    """Core runtime engine."""

    def __init__(
        self,
        config_path: str = ".env",
        state_file: str | None = None,
        offline_demo: bool = False,
    ) -> None:
        load_dotenv(config_path)

        self.offline_demo = offline_demo
        self.state_file = Path(state_file or os.getenv("STATE_FILE", "state.json"))

        self.exchange_id = os.getenv("EXCHANGE", "binance").strip()
        self.api_key = os.getenv("API_KEY", "").strip()
        self.api_secret = os.getenv("API_SECRET", "").strip()
        self.testnet = os.getenv("TESTNET", "true").lower() == "true"
        self.live = os.getenv("LIVE", "false").lower() == "true"
        self.paper_mode = os.getenv("PAPER_MODE", "true").lower() == "true"

        self.tf_bias = os.getenv("TIMEFRAME_BIAS", "4h").strip()
        self.tf_exec = os.getenv("TIMEFRAME_EXEC", "15m").strip()
        self.risk_pct = float(os.getenv("RISK_PCT", "0.0075"))
        self.max_notional = float(os.getenv("MAX_NOTIONAL", "10000.0"))
        self.default_leverage = int(os.getenv("DEFAULT_LEVERAGE", "1"))
        self.cooldown_candles = int(os.getenv("COOLDOWN_CANDLES", "8"))
        self.max_daily_trades = int(os.getenv("MAX_DAILY_TRADES", "2"))

        pairs_raw = os.getenv("PAIRS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,LINK/USDT")
        self.pairs = [p.strip() for p in pairs_raw.split(",") if p.strip()]

        self.risk_params = RiskParameters(
            risk_pct=self.risk_pct,
            max_notional=self.max_notional,
            default_leverage=float(self.default_leverage),
            max_isolated_leverage=3.0,
            max_daily_trades_per_pair=self.max_daily_trades,
            cooldown_candles=self.cooldown_candles,
        )
        self.risk_manager = RiskManager(self.risk_params)

        self.exchange_config = ExchangeConfig(
            exchange_id=self.exchange_id,
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
            live=self.live,
            paper_mode=self.paper_mode or not self.live,
            default_leverage=self.default_leverage,
        )
        self.exchange = CcxtExchangeWrapper(self.exchange_config)

        load_state(self.state_file, self.risk_manager)

    def fetch_data(self, pair: str, timeframe: str) -> pd.DataFrame:
        """Fetch real or synthetic data depending on run mode."""
        if self.offline_demo:
            return generate_synthetic_ohlcv(base_level=100.0, span=40.0)
        try:
            return self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=120)
        except Exception:
            logger.warning("Could not fetch remote candles, falling back to synthetic candles", pair=pair, timeframe=timeframe)
            return generate_synthetic_ohlcv(base_level=100.0, span=40.0)

    def process_pair(self, pair: str) -> None:
        """Process one iteration for a single pair."""
        # 1. Decrement cooldown on each execution step
        self.risk_manager.on_candle_close(pair)

        # 2. Fetch 4H bias candles and compute pivots and working range
        df_4h = self.fetch_data(pair, self.tf_bias)
        atr_4h_series = calculate_atr(df_4h)
        atr_4h = float(atr_4h_series.iloc[-1])

        pivot_high, pivot_low = detect_pivots(df_4h)
        curr_4h_high = float(df_4h["high"].iloc[-1])
        curr_4h_low = float(df_4h["low"].iloc[-1])

        dealing_range: DealingRange = calculate_working_range(
            pivot_high=pivot_high,
            pivot_low=pivot_low,
            current_high=curr_4h_high,
            current_low=curr_4h_low,
            atr_4h=atr_4h,
        )

        zones: FibonacciZones = calculate_zones(
            dealing_range.range_low, dealing_range.range_high
        )

        # 3. Fetch execution timeframe candles
        df_exec = self.fetch_data(pair, self.tf_exec)
        atr_exec_series = calculate_atr(df_exec)
        atr_exec = float(atr_exec_series.iloc[-1])

        rsi_exec_series = calculate_rsi(df_exec)
        rsi_exec = float(rsi_exec_series.iloc[-1])

        latest_candle = df_exec.iloc[-1]
        c_open = float(latest_candle["open"])
        c_high = float(latest_candle["high"])
        c_low = float(latest_candle["low"])
        c_close = float(latest_candle["close"])

        # 4. Manage open position if any
        existing_pos = self.risk_manager.active_positions.get(pair)
        if existing_pos is not None:
            # Check for range invalidation (new 4H pivot breaks range against position)
            if is_trade_invalidated_by_range(
                existing_pos.direction,
                c_close,
                dealing_range.range_low,
                dealing_range.range_high,
            ):
                closed_qty = self.risk_manager.close_invalidated_trade(pair)
                self.exchange.create_market_order(
                    pair,
                    side="sell" if existing_pos.direction == SignalType.LONG else "buy",
                    amount=closed_qty,
                    reduce_only=True,
                )
                logger.info(
                    "Trade closed due to range invalidation",
                    pair=pair,
                    closed_size=closed_qty,
                    exit_price=c_close,
                )
            else:
                # Check TP1, TP2, and SL
                trade_state, closed_qty = self.risk_manager.evaluate_tp_and_sl(
                    pair, c_high, c_low
                )
                if closed_qty > 0.0:
                    order_side = "sell" if existing_pos.direction == SignalType.LONG else "buy"
                    order_res = self.exchange.create_market_order(
                        pair, side=order_side, amount=closed_qty, reduce_only=True
                    )
                    logger.info(
                        "Position reduced or closed",
                        pair=pair,
                        trade_state=trade_state.value,
                        order_id=order_res.get("id"),
                        closed_amount=closed_qty,
                        current_sl=existing_pos.current_stop_loss,
                    )

        # 5. Evaluate potential new signal
        has_pos = pair in self.risk_manager.active_positions
        rem_cooldown = self.risk_manager.cooldowns.get(pair, 0)
        can_open, can_open_reason = self.risk_manager.can_open_trade(pair)

        signal_res: SignalResult = evaluate_signal(
            candle_open=c_open,
            candle_high=c_high,
            candle_low=c_low,
            candle_close=c_close,
            rsi_val=rsi_exec,
            dealing_range=dealing_range,
            zones=zones,
            atr_exec=atr_exec,
            has_open_position=has_pos,
            in_cooldown=(rem_cooldown > 0),
        )

        # Log current state dynamically without hardcoded reference levels
        logger.info(
            "Candle tick processed",
            pair=pair,
            range_low=dealing_range.range_low,
            range_high=dealing_range.range_high,
            range_valid=dealing_range.is_valid,
            poi_long=zones.poi_long,
            poi_short=zones.poi_short,
            rsi=round(rsi_exec, 2),
            signal=signal_res.signal.value,
            order_id=None,
            sl=signal_res.stop_loss if signal_res.signal != SignalType.NONE else None,
            tp1=signal_res.tp1 if signal_res.signal != SignalType.NONE else None,
            tp2=signal_res.tp2 if signal_res.signal != SignalType.NONE else None,
        )

        # 6. Execute signal if valid
        if signal_res.signal != SignalType.NONE and can_open:
            equity = self.exchange.get_equity()
            sizing: PositionSizingResult = calculate_position_size(
                equity=equity,
                entry_price=signal_res.entry_price,
                stop_loss=signal_res.stop_loss,
                params=self.risk_params,
                requested_leverage=float(self.default_leverage),
            )

            if sizing.is_valid and sizing.size > 0.0:
                side = "buy" if signal_res.signal == SignalType.LONG else "sell"
                order_result = self.exchange.create_market_order(
                    pair, side=side, amount=sizing.size
                )

                new_pos = ActivePosition(
                    pair=pair,
                    direction=signal_res.signal,
                    entry_price=signal_res.entry_price,
                    size=sizing.size,
                    initial_stop_loss=signal_res.stop_loss,
                    current_stop_loss=signal_res.stop_loss,
                    tp1=signal_res.tp1,
                    tp2=signal_res.tp2,
                    remaining_size=sizing.size,
                    tp1_hit=False,
                    entry_timestamp=datetime.now(timezone.utc).isoformat(),
                    state=TradeState.OPEN,
                )
                self.risk_manager.record_trade_opened(new_pos)

                logger.info(
                    "Signal executed",
                    pair=pair,
                    direction=signal_res.signal.value,
                    order_id=order_result.get("id"),
                    size=sizing.size,
                    entry_price=signal_res.entry_price,
                    sl=signal_res.stop_loss,
                    tp1=signal_res.tp1,
                    tp2=signal_res.tp2,
                )
            else:
                logger.warning(
                    "Position sizing rejected order",
                    pair=pair,
                    reason=sizing.reason,
                )

        save_state(self.state_file, self.risk_manager)

    def run_once(self) -> None:
        """Execute a single pass over all configured pairs."""
        for pair in self.pairs:
            try:
                self.process_pair(pair)
            except Exception as exc:
                logger.error("Error processing pair", pair=pair, error=str(exc))

    def run_loop(self, interval_seconds: int = 60) -> None:
        """Main loop."""
        logger.info(
            "Starting Trading Runner",
            exchange=self.exchange_id,
            testnet=self.testnet,
            live=self.live,
            paper_mode=self.paper_mode,
            pairs=self.pairs,
        )
        try:
            while True:
                self.run_once()
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Runner stopped by user.")
        finally:
            self.exchange.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic Dealing Range + Fib POI Bot")
    parser.add_argument("--demo", action="store_true", help="Run once in synthetic demo mode")
    parser.add_argument("--once", action="store_true", help="Run a single loop over pairs")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    parser.add_argument("--config", type=str, default=".env", help="Path to env config file")
    args = parser.parse_args()

    # Create dummy .env if none exists
    if not Path(args.config).exists():
        example = Path(".env.example")
        if example.exists():
            import shutil
            shutil.copy(example, args.config)

    runner = TradingRunner(
        config_path=args.config,
        offline_demo=args.demo,
    )

    if args.demo or args.once:
        runner.run_once()
    else:
        runner.run_loop(interval_seconds=args.interval)


if __name__ == "__main__":
    main()

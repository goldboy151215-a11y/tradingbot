"""Risk management, position sizing, cooldowns, and trade tracking."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.strategy import SignalType


class TradeState(str, Enum):
    OPEN = "OPEN"
    TP1_HIT = "TP1_HIT"
    CLOSED_TP2 = "CLOSED_TP2"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_INVALIDATED = "CLOSED_INVALIDATED"


@dataclass
class RiskParameters:
    risk_pct: float = 0.0075
    max_notional: float = 10000.0
    default_leverage: float = 1.0
    max_isolated_leverage: float = 3.0
    max_daily_trades_per_pair: int = 2
    cooldown_candles: int = 8


@dataclass
class PositionSizingResult:
    size: float
    risk_amount: float
    notional: float
    effective_leverage: float
    is_valid: bool
    reason: str


@dataclass
class ActivePosition:
    pair: str
    direction: SignalType
    entry_price: float
    size: float
    initial_stop_loss: float
    current_stop_loss: float
    tp1: float
    tp2: float
    remaining_size: float
    tp1_hit: bool
    entry_timestamp: str
    state: TradeState


def calculate_position_size(
    equity: float,
    entry_price: float,
    stop_loss: float,
    params: RiskParameters,
    requested_leverage: float = 1.0,
) -> PositionSizingResult:
    """Calculate position size according to risk percentage and caps.

    - Risk amount = equity * risk_pct
    - Size = risk_amount / abs(entry_price - stop_loss)
    - Leverage: capped at max 3x isolated (default 1x)
    - Notional cap: max_notional
    """
    if equity <= 0.0 or entry_price <= 0.0:
        return PositionSizingResult(
            size=0.0,
            risk_amount=0.0,
            notional=0.0,
            effective_leverage=1.0,
            is_valid=False,
            reason="Non-positive equity or entry price",
        )

    distance = abs(entry_price - stop_loss)
    if distance <= 0.0:
        return PositionSizingResult(
            size=0.0,
            risk_amount=0.0,
            notional=0.0,
            effective_leverage=1.0,
            is_valid=False,
            reason="Stop loss is equal to entry price",
        )

    risk_amount = equity * params.risk_pct
    raw_size = risk_amount / distance

    effective_leverage = max(
        1.0, min(requested_leverage, params.max_isolated_leverage)
    )
    max_notional_by_leverage = equity * effective_leverage
    allowed_max_notional = min(params.max_notional, max_notional_by_leverage)

    raw_notional = raw_size * entry_price
    final_size = raw_size
    reason = "Risk-based sizing"

    if raw_notional > allowed_max_notional:
        final_size = allowed_max_notional / entry_price
        reason = "Capped by max allowed notional"

    final_notional = final_size * entry_price
    return PositionSizingResult(
        size=final_size,
        risk_amount=risk_amount,
        notional=final_notional,
        effective_leverage=effective_leverage,
        is_valid=final_size > 0.0,
        reason=reason,
    )


class RiskManager:
    """Manages risk limits, daily trade limits, cooldowns, and active positions."""

    def __init__(self, params: RiskParameters | None = None) -> None:
        self.params = params or RiskParameters()
        self.active_positions: dict[str, ActivePosition] = {}
        # daily_trades: pair -> {date_iso: count}
        self.daily_trades: dict[str, dict[str, int]] = {}
        # cooldowns: pair -> remaining candles
        self.cooldowns: dict[str, int] = {}

    def get_current_utc_date(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def get_daily_trade_count(self, pair: str, utc_date: str | None = None) -> int:
        date_str = utc_date or self.get_current_utc_date()
        return self.daily_trades.get(pair, {}).get(date_str, 0)

    def can_open_trade(
        self, pair: str, utc_date: str | None = None
    ) -> tuple[bool, str]:
        if pair in self.active_positions:
            return False, f"Position already open for {pair}"

        rem_cooldown = self.cooldowns.get(pair, 0)
        if rem_cooldown > 0:
            return False, f"Pair {pair} is in cooldown ({rem_cooldown} candles remaining)"

        count = self.get_daily_trade_count(pair, utc_date)
        if count >= self.params.max_daily_trades_per_pair:
            return (
                False,
                f"Daily trade limit reached for {pair} ({count}/{self.params.max_daily_trades_per_pair})",
            )

        return True, "OK"

    def record_trade_opened(
        self, position: ActivePosition, utc_date: str | None = None
    ) -> None:
        pair = position.pair
        date_str = utc_date or self.get_current_utc_date()

        self.active_positions[pair] = position
        if pair not in self.daily_trades:
            self.daily_trades[pair] = {}
        self.daily_trades[pair][date_str] = (
            self.daily_trades[pair].get(date_str, 0) + 1
        )

    def record_stop_loss_hit(self, pair: str) -> None:
        if pair in self.active_positions:
            pos = self.active_positions.pop(pair)
            pos.state = TradeState.CLOSED_SL
        self.cooldowns[pair] = self.params.cooldown_candles

    def decrement_cooldown(self, pair: str) -> None:
        if pair in self.cooldowns and self.cooldowns[pair] > 0:
            self.cooldowns[pair] -= 1
            if self.cooldowns[pair] == 0:
                del self.cooldowns[pair]

    def on_candle_close(self, pair: str) -> None:
        """Call on each execution candle close to decrement cooldowns."""
        self.decrement_cooldown(pair)

    def update_stop_loss_tightening(self, pair: str, new_stop: float) -> float:
        """Update stop loss ensuring it only stays the same or tightens, never widens."""
        pos = self.active_positions.get(pair)
        if pos is None:
            return new_stop

        if pos.direction == SignalType.LONG:
            # Long SL can only increase (move upwards)
            if new_stop > pos.current_stop_loss:
                pos.current_stop_loss = new_stop
        elif pos.direction == SignalType.SHORT:
            # Short SL can only decrease (move downwards)
            if new_stop < pos.current_stop_loss:
                pos.current_stop_loss = new_stop

        return pos.current_stop_loss

    def evaluate_tp_and_sl(
        self, pair: str, current_high: float, current_low: float
    ) -> tuple[TradeState, float]:
        """Check if TP1, TP2, or SL was triggered during the candle.

        Returns (TradeState, closed_size).
        """
        pos = self.active_positions.get(pair)
        if pos is None:
            return TradeState.CLOSED_SL, 0.0

        # Check Stop Loss first
        if pos.direction == SignalType.LONG:
            if current_low <= pos.current_stop_loss:
                remaining = pos.remaining_size
                self.record_stop_loss_hit(pair)
                return TradeState.CLOSED_SL, remaining

            # Check TP1 if not yet hit
            if not pos.tp1_hit and current_high >= pos.tp1:
                pos.tp1_hit = True
                pos.state = TradeState.TP1_HIT
                closed_part = pos.size * 0.50
                pos.remaining_size -= closed_part
                # Move SL to break-even (entry price)
                self.update_stop_loss_tightening(pair, pos.entry_price)
                return TradeState.TP1_HIT, closed_part

            # Check TP2
            if pos.tp1_hit and current_high >= pos.tp2:
                remaining = pos.remaining_size
                pos.state = TradeState.CLOSED_TP2
                self.active_positions.pop(pair, None)
                return TradeState.CLOSED_TP2, remaining

        elif pos.direction == SignalType.SHORT:
            if current_high >= pos.current_stop_loss:
                remaining = pos.remaining_size
                self.record_stop_loss_hit(pair)
                return TradeState.CLOSED_SL, remaining

            # Check TP1 if not yet hit
            if not pos.tp1_hit and current_low <= pos.tp1:
                pos.tp1_hit = True
                pos.state = TradeState.TP1_HIT
                closed_part = pos.size * 0.50
                pos.remaining_size -= closed_part
                # Move SL to break-even (entry price)
                self.update_stop_loss_tightening(pair, pos.entry_price)
                return TradeState.TP1_HIT, closed_part

            # Check TP2
            if pos.tp1_hit and current_low <= pos.tp2:
                remaining = pos.remaining_size
                pos.state = TradeState.CLOSED_TP2
                self.active_positions.pop(pair, None)
                return TradeState.CLOSED_TP2, remaining

        return pos.state, 0.0

    def close_invalidated_trade(self, pair: str) -> float:
        """Close trade due to dealing range invalidation."""
        pos = self.active_positions.pop(pair, None)
        if pos:
            pos.state = TradeState.CLOSED_INVALIDATED
            return pos.remaining_size
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for crash-safe persistence."""
        return {
            "daily_trades": self.daily_trades,
            "cooldowns": self.cooldowns,
            "active_positions": {
                k: {
                    **asdict(v),
                    "direction": v.direction.value,
                    "state": v.state.value,
                }
                for k, v in self.active_positions.items()
            },
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        """Load state from dictionary."""
        self.daily_trades = data.get("daily_trades", {})
        self.cooldowns = data.get("cooldowns", {})
        positions = {}
        for k, v in data.get("active_positions", {}).items():
            positions[k] = ActivePosition(
                pair=v["pair"],
                direction=SignalType(v["direction"]),
                entry_price=v["entry_price"],
                size=v["size"],
                initial_stop_loss=v["initial_stop_loss"],
                current_stop_loss=v["current_stop_loss"],
                tp1=v["tp1"],
                tp2=v["tp2"],
                remaining_size=v["remaining_size"],
                tp1_hit=v["tp1_hit"],
                entry_timestamp=v["entry_timestamp"],
                state=TradeState(v["state"]),
            )
        self.active_positions = positions

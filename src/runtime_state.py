"""Single Source of Truth for Dealing Range + Fibonacci POI Bot.

Defines, validates, and synchronizes the canonical runtime state shared by
Freqtrade, the Web Dashboard, and the Telegram Bot.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Canonical locations
DEFAULT_PATHS = [
    Path("/root/ft_userdata/user_data/runtime_state.json"),
    Path("/freqtrade/user_data/runtime_state.json"),
    Path("/root/runtime_state.json"),
]

CANONICAL_MINIMAL_ROI: dict[str, float] = {
    "0": 0.0367,
    "15": 0.020,
    "30": 0.010,
}

REQUIRED_KEYS = [
    "strategy_id",
    "live",
    "equity_usdt",
    "stake_usdt",
    "leverage",
    "margin_mode",
    "risk_pct",
    "max_loss_usdt",
    "stoploss_price_pct",
    "minimal_roi",
    "max_trades_per_day",
    "trades_today",
    "open_trade",
    "margin_used_pct",
    "margin_cap_pct",
    "position_adjustment",
]


@dataclass
class OpenTradeInfo:
    pair: str
    side: str  # "long" or "short"
    entry: float
    sl: float
    tp1: float
    tp2: float
    notional: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "side": self.side,
            "entry": round(self.entry, 6),
            "sl": round(self.sl, 6),
            "tp1": round(self.tp1, 6),
            "tp2": round(self.tp2, 6),
            "notional": round(self.notional, 2),
        }


@dataclass
class RuntimeState:
    strategy_id: str = "weex_futures_quant"
    strategy_name: str = "WEEX Futures Scalper 50x"
    exchange: str = "weex"
    pair: str = "BTC/USDT:USDT"
    live: bool = True
    equity_usdt: float = 168.40
    free_usdt: float = 151.83
    stake_usdt: float = 25.0
    leverage: float = 50.0
    margin_mode: str = "isolated"
    risk_pct: float = 0.02
    max_loss_usdt: float = 3.368
    stoploss_price_pct: float = 0.018
    minimal_roi: dict[str, float] = field(default_factory=lambda: dict(CANONICAL_MINIMAL_ROI))
    max_trades_per_day: int = 50
    trades_today: int = 0
    open_trade: dict[str, Any] | None = None
    margin_used_pct: float = 0.0984
    margin_cap_pct: float = 0.50
    position_adjustment: bool = False
    orders_blocked: bool = False
    block_reason: str = ""

    def __post_init__(self) -> None:
        self.recalculate()

    def recalculate(self) -> None:
        """Derive parameters cleanly while respecting configured scalp values."""
        equity = float(self.equity_usdt) if float(self.equity_usdt) > 0 else 100.0
        self.equity_usdt = round(equity, 2)
        if not self.risk_pct or self.risk_pct <= 0.0:
            self.risk_pct = 0.02
        self.max_loss_usdt = round(self.equity_usdt * self.risk_pct, 4)
        if self.max_loss_usdt <= 0.0:
            self.max_loss_usdt = round(100.0 * self.risk_pct, 4)

        if self.free_usdt <= 0.0:
            self.free_usdt = self.equity_usdt

        self.stake_usdt = round(float(self.stake_usdt or 25.0), 2)
        self.leverage = min(max(float(self.leverage or 50.0), 1.0), 50.0)
        self.margin_mode = "isolated"
        self.max_trades_per_day = max(int(self.max_trades_per_day or 50), 1)
        self.position_adjustment = False
        self.strategy_id = "weex_futures_quant"
        self.strategy_name = "WEEX Futures Scalper 50x"
        self.minimal_roi = dict(CANONICAL_MINIMAL_ROI)
        self.margin_cap_pct = 0.50

        if not self.stoploss_price_pct or self.stoploss_price_pct <= 0.0:
            self.stoploss_price_pct = 0.018

        # Orders blocked check: margin > 25% or trades_today >= 2
        reasons = []
        if self.margin_used_pct > self.margin_cap_pct:
            reasons.append(f"Marge {self.margin_used_pct*100:.1f}% > cap {self.margin_cap_pct*100:.0f}%")
        if self.trades_today >= self.max_trades_per_day:
            reasons.append(f"Max trades/dag bereikt ({self.trades_today}/{self.max_trades_per_day})")

        if reasons:
            self.orders_blocked = True
            self.block_reason = " | ".join(reasons)
        else:
            self.orders_blocked = False
            self.block_reason = ""

    def to_dict(self) -> dict[str, Any]:
        self.recalculate()
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "exchange": self.exchange,
            "pair": self.pair,
            "live": self.live,
            "equity_usdt": self.equity_usdt,
            "free_usdt": round(self.free_usdt, 2),
            "stake_usdt": self.stake_usdt,
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "risk_pct": self.risk_pct,
            "max_loss_usdt": self.max_loss_usdt,
            "stoploss_price_pct": self.stoploss_price_pct,
            "minimal_roi": self.minimal_roi,
            "max_trades_per_day": self.max_trades_per_day,
            "trades_today": self.trades_today,
            "open_trade": self.open_trade,
            "margin_used_pct": round(self.margin_used_pct, 4),
            "margin_cap_pct": self.margin_cap_pct,
            "position_adjustment": self.position_adjustment,
            "orders_blocked": self.orders_blocked,
            "block_reason": self.block_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeState":
        return cls(
            strategy_id=str(data.get("strategy_id", "weex_futures_quant")),
            strategy_name=str(data.get("strategy_name", "WEEX Futures Scalper 50x")),
            exchange=data.get("exchange", "weex"),
            pair=data.get("pair", "BTC/USDT:USDT"),
            live=bool(data.get("live", True)),
            equity_usdt=float(data.get("equity_usdt", 168.40)),
            free_usdt=float(data.get("free_usdt", 151.83)),
            stake_usdt=float(data.get("stake_usdt", 25.0)),
            leverage=float(data.get("leverage", 50.0)),
            margin_mode="isolated",
            risk_pct=float(data.get("risk_pct", 0.02)),
            max_loss_usdt=float(data.get("max_loss_usdt", 3.368)),
            stoploss_price_pct=float(data.get("stoploss_price_pct", 0.018)),
            minimal_roi=data.get("minimal_roi", dict(CANONICAL_MINIMAL_ROI)),
            max_trades_per_day=int(data.get("max_trades_per_day", 50)),
            trades_today=int(data.get("trades_today", 0)),
            open_trade=data.get("open_trade"),
            margin_used_pct=float(data.get("margin_used_pct", 0.0984)),
            margin_cap_pct=float(data.get("margin_cap_pct", 0.50)),
            position_adjustment=False,
            orders_blocked=bool(data.get("orders_blocked", False)),
            block_reason=str(data.get("block_reason", "")),
        )


def extract_weex_balance(ft_balance_data: dict[str, Any]) -> tuple[float, float]:
    """Extract actual USDT balance and free USDT from Weex exchange via Freqtrade API."""
    usdt_bal = 0.0
    usdt_free = 0.0
    for curr in ft_balance_data.get("currencies", []):
        if curr.get("currency") == "USDT":
            usdt_bal = float(curr.get("balance", 0.0))
            usdt_free = float(curr.get("free", 0.0))
            break
    if usdt_bal <= 0.0:
        usdt_bal = float(ft_balance_data.get("total", 168.40))
        usdt_free = float(ft_balance_data.get("free", usdt_bal))
    return round(usdt_bal, 2), round(usdt_free, 2)


def get_state_file_path() -> Path:
    for p in DEFAULT_PATHS:
        if p.parent.exists():
            return p
    return DEFAULT_PATHS[0]


def load_runtime_state(filepath: Path | str | None = None) -> RuntimeState:
    path = Path(filepath) if filepath else get_state_file_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return RuntimeState.from_dict(data)
        except Exception as exc:
            logger.warning(f"Could not parse state file at {path}: {exc}, using defaults")
    state = RuntimeState()
    save_runtime_state(state, path)
    return state


def save_runtime_state(state: RuntimeState, filepath: Path | str | None = None) -> None:
    path = Path(filepath) if filepath else get_state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = state.to_dict()
    temp_dir = path.parent
    with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, indent=2)
        temp_name = tf.name

    os.replace(temp_name, path)

    # Sync to other default paths if they exist
    for other in DEFAULT_PATHS:
        if other != path and other.parent.exists():
            try:
                if not other.is_symlink():
                    with open(other, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
            except Exception:
                pass


def sync_with_weex_exchange(
    state: RuntimeState,
    ft_api_url: str = "http://127.0.0.1:8080/api/v1",
    auth: tuple[str, str] = ("admin", "mendy7218"),
    db_path: str = "/root/ft_userdata/user_data/tradesv3.sqlite",
) -> RuntimeState:
    """Synchronize state with actual live Weex balance and open positions."""
    # 1. Fetch live balance from Freqtrade API
    try:
        r = requests.get(f"{ft_api_url}/balance", auth=auth, timeout=4)
        if r.status_code == 200:
            bal_data = r.json()
            total_usdt, free_usdt = extract_weex_balance(bal_data)
            if total_usdt > 0:
                state.equity_usdt = total_usdt
                state.free_usdt = free_usdt
    except Exception as exc:
        logger.warning(f"Could not fetch Weex balance via FT API: {exc}")

    # 2. Query trades from DB
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM trades WHERE open_date LIKE ?", (f"{today}%",))
        state.trades_today = c.fetchone()[0]

        c.execute("SELECT * FROM trades WHERE is_open=1 ORDER BY id DESC")
        open_rows = c.fetchall()
        conn.close()

        # Real margin used on Weex = total equity - free equity
        used_margin = max(0.0, state.equity_usdt - state.free_usdt)
        if state.equity_usdt > 0:
            state.margin_used_pct = round(used_margin / state.equity_usdt, 4)

        if open_rows:
            r = open_rows[0]
            side = "short" if bool(r["is_short"]) else "long"
            state.open_trade = {
                "pair": r["pair"],
                "side": side,
                "entry": float(r["open_rate"] or 0.0),
                "sl": float(r["stop_loss"] or 0.0),
                "tp1": 0.0,
                "tp2": 0.0,
                "notional": float(r["stake_amount"] or 0.0),
            }
        else:
            state.open_trade = None

    except Exception as exc:
        logger.warning(f"Could not query trades db: {exc}")

    state.recalculate()
    return state


def verify_config_sync(
    runtime_state: RuntimeState | dict[str, Any],
    freqtrade_config: dict[str, Any],
    strategy_obj: Any | None = None,
) -> tuple[bool, list[str]]:
    """Check whether Freqtrade config and strategy match runtime_state."""
    mismatches: list[str] = []
    rs = runtime_state if isinstance(runtime_state, dict) else runtime_state.to_dict()

    # 1. Minimal ROI
    expected_roi = rs.get("minimal_roi", CANONICAL_MINIMAL_ROI)
    cfg_roi = freqtrade_config.get("minimal_roi")

    if str(cfg_roi.get("0") if isinstance(cfg_roi, dict) else "") == "0.25":
        mismatches.append(f"ROI bevat verboden 0.25 (+25%): {cfg_roi}")
    if cfg_roi != expected_roi:
        mismatches.append(f"ROI mismatch: runtime_state={expected_roi} vs config={cfg_roi}")

    # 2. Stoploss
    rs_sl = rs.get("stoploss_price_pct", 0.018)
    cfg_sl = abs(float(freqtrade_config.get("stoploss", 0.0)))
    if abs(round(rs_sl, 4) - round(cfg_sl, 4)) > 0.005:
        mismatches.append(f"Stoploss mismatch: runtime_state={rs_sl:.4f} vs config={cfg_sl:.4f}")

    # 3. Stake
    rs_stake = float(rs.get("stake_usdt", 25.0))
    cfg_stake = float(freqtrade_config.get("stake_amount", 0.0))
    if round(rs_stake, 2) != round(cfg_stake, 2):
        mismatches.append(f"Stake mismatch: runtime_state=${rs_stake:.2f} vs config=${cfg_stake:.2f}")

    # 4. Leverage
    rs_lev = float(rs.get("leverage", 50.0))
    if rs_lev > 50.0 or rs_lev < 1.0:
        mismatches.append(f"Leverage mismatch: runtime_state={rs_lev} (max 50.0)")

    # 5. Margin mode
    rs_mm = str(rs.get("margin_mode", "")).lower()
    cfg_mm = str(freqtrade_config.get("margin_mode", "")).lower()
    if rs_mm != "isolated" or cfg_mm != "isolated":
        mismatches.append(f"Margin mode mismatch: runtime_state={rs_mm} vs config={cfg_mm} (moet isolated zijn)")

    # 6. Max trades per day
    rs_max_trades = int(rs.get("max_trades_per_day", 50))
    if rs_max_trades < 1:
        mismatches.append(f"Max trades/dag mismatch: {rs_max_trades} (moet >= 1 zijn)")

    # 7. Max open trades
    cfg_max_open = int(freqtrade_config.get("max_open_trades", 0))
    if cfg_max_open < 1:
        mismatches.append(f"Max open trades mismatch: config={cfg_max_open}")

    # 8. Strategy checks if object or class provided
    if strategy_obj is not None:
        strat_roi = getattr(strategy_obj, "minimal_roi", None)
        if strat_roi != expected_roi:
            mismatches.append(f"Strategy minimal_roi mismatch: {strat_roi} != {expected_roi}")
        strat_pos_adj = getattr(strategy_obj, "position_adjustment_enable", None)
        if strat_pos_adj is True:
            mismatches.append("Strategy position_adjustment_enable is True (moet False zijn)")

    return len(mismatches) == 0, mismatches

"""Test guard against hardcoded market price levels.

Inspects the AST of strategy.py to guarantee that no concrete market prices
exist in the codebase. All numbers must be mathematical ratios, multipliers,
percentages (0-100), or integer lookbacks.
"""

import ast
from pathlib import Path


def test_grep_guard_no_literal_price_constants_in_strategy():
    strategy_path = Path("/root/src/strategy.py")
    assert strategy_path.exists(), "src/strategy.py must exist"

    with open(strategy_path, "r", encoding="utf-8") as f:
        source_code = f.read()

    tree = ast.parse(source_code)

    # Allowed ratios, multipliers, lookbacks, and standard indicators
    allowed_floats = {
        0.0,
        1.0,
        1.5,  # ATR multiplier for range validity
        0.25,  # ATR multiplier for stop distance
        0.236,  # Fib ratio
        0.382,  # Fib ratio
        0.50,  # Fib ratio (TP1 / midpoint)
        0.786,  # Fib ratio
        0.886,  # Fib ratio
        30.0,  # RSI lower bound
        55.0,  # RSI upper bound for long
        45.0,  # RSI lower bound for short
        70.0,  # RSI upper bound for short
        50.0,  # RSI midpoint fill
        100.0,  # RSI scale 0-100
    }

    found_floats: list[float] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            val = node.value
            if isinstance(val, float):
                found_floats.append(val)
                # Verify that no float resembles a market price (e.g. > 100 or outside ratios)
                assert (
                    val in allowed_floats
                ), f"Unauthorized float constant {val} found in strategy.py. Hardcoded prices are strictly prohibited."

    # Also verify no large integer constants that could represent hardcoded satoshi/dollar levels
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert (
                node.value <= 100
            ), f"Large integer constant {node.value} found in strategy.py, possible hardcoded price level."

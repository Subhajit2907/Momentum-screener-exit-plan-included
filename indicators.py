"""
indicators.py

Technical indicator calculations used by the screener.
Contains no Yahoo Finance code and no DataFrame logic.
"""

from typing import List, Optional, Tuple


def calc_ema(prices: List[float], period: int) -> Optional[float]:
    """
    Calculate Exponential Moving Average.
    """

    if len(prices) < period:
        return None

    multiplier = 2 / (period + 1)

    ema = sum(prices[:period]) / period

    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return round(ema, 2)


def calc_ema_series(prices, period):
    """
    Calculate the EMA value for every price in the series.

    Returns a list with the same length as prices.
    Values before the first valid EMA are None.
    """

    if len(prices) < period:
        return [None] * len(prices)

    result = [None] * len(prices)

    multiplier = 2 / (period + 1)

    ema = sum(prices[:period]) / period

    result[period - 1] = ema

    for i in range(period, len(prices)):
        ema = (
            prices[i] * multiplier
            + ema * (1 - multiplier)
        )

        result[i] = ema

    return result

def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Calculate RSI using Wilder's smoothing.
    """

    if len(prices) < period + 1:
        return None

    deltas = []

    for i in range(1, len(prices)):
        deltas.append(prices[i] - prices[i - 1])

    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return round(rsi, 2)


def calc_average_volume(
    volumes: List[float],
    period: int = 20
) -> Optional[float]:
    """
    Calculate average trading volume.
    """

    recent = [
        volume
        for volume in volumes[-period:]
        if volume is not None and volume > 0
    ]

    if not recent:
        return None

    return sum(recent) / len(recent)


def calc_52week_metrics(
    closes: List[float]
) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns

    (52-week high,
     percentage below high)
    """

    if len(closes) < 2:
        return None, None

    high = max(closes)

    current = closes[-1]

    pct_from_high = (
        (high - current)
        / high
        * 100
    )

    return round(high, 2), round(pct_from_high, 1)


def pct_above_ema(
    current_price: float,
    ema: float
) -> float:
    """
    Percentage above EMA.
    """

    return round(
        ((current_price - ema) / ema) * 100,
        1,
    )


def format_volume(avg_volume: float) -> str:
    """
    Convert volume to readable string.
    """

    return f"{avg_volume / 1_000_000:.2f}M"
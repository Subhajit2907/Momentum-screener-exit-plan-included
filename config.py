"""
config.py
Configuration for the Nifty Midcap Momentum Screener.

All strategy parameters should be modified here.
No screening logic should be hardcoded elsewhere.
"""

# ==========================================================
# Application
# ==========================================================

APP_NAME = "Nifty Midcap Momentum Screener"
VERSION = "4.0.0"

# ==========================================================
# Market Configuration
# ==========================================================

MARKET_SUFFIX = ".NS"
LOOKBACK_PERIOD = "1y"
INTERVAL = "1d"

# ==========================================================
# EMA Settings
# ==========================================================

EMA_SHORT = 50
EMA_MEDIUM = 100
EMA_LONG = 200

# ==========================================================
# Momentum Filters
# ==========================================================

RSI_PERIOD = 14
RSI_MIN = 50

# ==========================================================
# Liquidity Filter
# ==========================================================

AVG_VOLUME_PERIOD = 20
MIN_AVG_VOLUME = 500_000

# ==========================================================
# 52 Week High Filter
# ==========================================================

NEAR_HIGH_PERCENT = 20

# ==========================================================
# Composite Score Weights
# (Must total 1.00)
# ==========================================================

WEIGHT_EMA200 = 0.30
WEIGHT_RSI = 0.25
WEIGHT_52W_HIGH = 0.25
WEIGHT_VOLUME = 0.20

# ==========================================================
# Portfolio Health Score
# (Phase 2)
# ==========================================================

HEALTH_EMA50 = 15
HEALTH_EMA100 = 15
HEALTH_EMA200 = 25
HEALTH_RSI = 15
HEALTH_NEAR_HIGH = 10
HEALTH_VOLUME = 10
HEALTH_RELATIVE_STRENGTH = 10

# ==========================================================
# Recommendation Thresholds
# (Phase 2)
# ==========================================================

STRONG_HOLD_SCORE = 90
HOLD_SCORE = 75
WATCH_SCORE = 60
REDUCE_SCORE = 40

# ==========================================================
# Report Settings
# ==========================================================

TOP_PICK_COUNT = 10
REPORT_FOLDER = "Reports"

PASSED_SHEET = "✅ Passed"
EMA_ONLY_SHEET = "⚠ EMA Pass Only"

# ==========================================================
# Download Settings
# ==========================================================

YFINANCE_THREADS = True
AUTO_ADJUST = True
SHOW_PROGRESS = True

# ==========================================================
# Portfolio Settings
# (Phase 2)
# ==========================================================

PORTFOLIO_FOLDER = "Portfolio"
HOLDINGS_FILE = "holdings.csv"

# ==========================================================
# Logging
# ==========================================================

LOG_FOLDER = "Logs"

# ==========================================================
# Colours (Excel)
# ==========================================================

HEADER_FILL = "1F4E79"
HEADER_FONT = "FFFFFF"

ALT_ROW_FILL = "EBF5FB"
TOP10_FILL = "E2EFDA"

WARNING_FILL = "7F3F00"

# ==========================================================
# Validation
# ==========================================================

TOTAL_WEIGHT = (
    WEIGHT_EMA200
    + WEIGHT_RSI
    + WEIGHT_52W_HIGH
    + WEIGHT_VOLUME
)

if round(TOTAL_WEIGHT, 2) != 1.00:
    raise ValueError(
        f"Composite score weights must total 1.00 (Current={TOTAL_WEIGHT})"
    )
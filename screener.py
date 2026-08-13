"""
Core screener logic — callable as a library by app.py.
No sys.exit(), no print() — returns data or raises exceptions.
"""

import io
import os
from datetime import datetime

import pandas as pd
import yfinance as yf
import config

from indicators import (
    calc_ema,
    calc_ema_series,
    calc_rsi,
    calc_average_volume,
    calc_52week_metrics,
    format_volume,
    pct_above_ema,
)

from ranking import rank_stocks

from reports import build_excel

# ── Indicators ────────────────────────────────────────────


def extract_series(raw, ticker, field, tickers):
    series = None
    if len(tickers) > 1:
        if isinstance(raw.columns, pd.MultiIndex):
            if (field, ticker) in raw.columns:
                series = raw[(field, ticker)].dropna()
            elif (ticker, field) in raw.columns:
                series = raw[(ticker, field)].dropna()
            else:
                try:
                    series = raw.xs(ticker, axis=1, level=1)[field].dropna()
                except Exception:
                    try:
                        series = raw.xs(ticker, axis=1, level=0)[field].dropna()
                    except Exception:
                        pass
        else:
            if ticker in raw.columns:
                series = raw[ticker].dropna()
            elif field in raw.columns:
                series = raw[field].dropna()
    else:
        if field in raw.columns:
            series = raw[field].dropna()
    return series.values.tolist() if series is not None else []

def extract_series_with_dates(raw, ticker, field, tickers):
    """
    Extract a yfinance series while preserving its dates.

    Returns:
        pandas.Series
    """

    series = None

    if len(tickers) > 1:

        if isinstance(raw.columns, pd.MultiIndex):

            if (field, ticker) in raw.columns:
                series = raw[(field, ticker)]

            elif (ticker, field) in raw.columns:
                series = raw[(ticker, field)]

            else:
                try:
                    series = raw.xs(
                        ticker,
                        axis=1,
                        level=1,
                    )[field]

                except Exception:

                    try:
                        series = raw.xs(
                            ticker,
                            axis=1,
                            level=0,
                        )[field]

                    except Exception:
                        pass

        else:

            if ticker in raw.columns:
                series = raw[ticker]

            elif field in raw.columns:
                series = raw[field]

    else:

        if field in raw.columns:
            series = raw[field]

    if series is None:
        return pd.Series(dtype=float)

    series = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    return series


# ── Load symbols from uploaded CSV bytes ───────────────────

def load_symbols_from_bytes(file_bytes):
    """Parse CSV from uploaded file bytes. Returns (symbols list, info df)."""
    df = pd.read_csv(io.BytesIO(file_bytes))
    # Accept 'Symbol' or 'symbol' column
    col_map = {c.strip().lower(): c for c in df.columns}
    if "symbol" not in col_map:
        raise ValueError(f"CSV must have a 'Symbol' column. Found: {list(df.columns)}")
    sym_col = col_map["symbol"]
    symbols = df[sym_col].dropna().astype(str).str.strip().unique().tolist()
    keep = [c for c in [sym_col, "Company Name", "Industry"] if c in df.columns]
    info = df[keep].copy().rename(columns={sym_col: "Symbol"})
    return symbols, info


# ── Load holdings from uploaded CSV bytes ──────────────────

def load_holdings_from_bytes(file_bytes):
    """
    Parse holdings CSV. Expected columns:
      Symbol, Entry Date (DD-MM-YYYY), Entry Price
    Returns list of dicts.
    """
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip() for c in df.columns]
    col_map = {c.lower(): c for c in df.columns}

    if "symbol" not in col_map:
        raise ValueError(f"Holdings CSV must have a 'Symbol' column. Found: {list(df.columns)}")

    sym_col   = col_map["symbol"]
    date_col  = col_map.get("entry date", None)
    price_col = col_map.get("entry price", None)

    holdings = []
    for _, row in df.iterrows():
        sym = str(row[sym_col]).strip()
        if not sym:
            continue
        entry_date  = str(row[date_col]).strip()  if date_col  else "—"
        entry_price = row[price_col]               if price_col else None
        holdings.append({
            "symbol":      sym,
            "entry_date":  entry_date,
            "entry_price": float(entry_price) if entry_price and str(entry_price) != "nan" else None,
        })
    return holdings


# ── Exit signal check (weekly 50 EMA) ─────────────────────

def check_exit_signals(holdings, progress_callback=None):
    """
    Phase 2A portfolio exit engine.

    Signals are based on:

    1. Weekly 20 EMA
    2. Weekly 50 EMA
    3. Weekly RSI(14)
    4. Consecutive weekly closes below W50
    5. Drawdown from peak since entry
    6. Exit Score out of 100

    The weekly 50 EMA remains the primary trend signal.
    """

    if not holdings:
        return []

    symbols = [
        h["symbol"]
        for h in holdings
    ]

    tickers = [
        s + config.MARKET_SUFFIX
        for s in symbols
    ]

    if progress_callback:
        progress_callback(
            f"Checking portfolio exit signals for "
            f"{len(symbols)} holdings (weekly + daily data)…"
        )

    # ----------------------------------------------------------
    # Download 5 years of weekly data
    # ----------------------------------------------------------

    try:

        raw = yf.download(
            tickers,
            period="5y",
            interval="1wk",
            group_by="ticker",
            auto_adjust=config.AUTO_ADJUST,
            progress=False,
            threads=config.YFINANCE_THREADS,
        )

    except Exception as e:

        return [
            {
                "Symbol": h["symbol"],
                "Signal": "✗ Data error",
                "Action": "Check manually",
                "Error": str(e)[:80],
            }
            for h in holdings
        ]

    # ----------------------------------------------------------
    # Download daily data for current P&L and peak drawdown.
    #
    # Weekly data remains the source for:
    #   - Weekly 20/50 EMA
    #   - Weekly RSI
    #   - Weekly W50 confirmation
    #
    # Daily data is used for:
    #   - Latest available price
    #   - Position P&L
    #   - Peak drawdown since entry
    #
    # This is important because the -20% hard exit is a
    # position-risk rule and should not wait for a weekly close.
    # ----------------------------------------------------------

    try:

        daily_raw = yf.download(
            tickers,
            period="5y",
            interval="1d",
            group_by="ticker",
            auto_adjust=config.AUTO_ADJUST,
            progress=False,
            threads=config.YFINANCE_THREADS,
        )

    except Exception:

        daily_raw = None

    results = []

    for holding in holdings:

        symbol = holding["symbol"]
        ticker = symbol + config.MARKET_SUFFIX

        try:

            # --------------------------------------------------
            # Get weekly close series WITH dates
            # --------------------------------------------------

            close_series = extract_series_with_dates(
                raw,
                ticker,
                "Close",
                tickers,
            )

            if close_series.empty:
                results.append({
                    "Symbol": symbol,
                    "Entry Date": holding["entry_date"],
                    "Entry Price": holding["entry_price"],
                    "Current Price": "—",
                    "Weekly 20 EMA": "—",
                    "Weekly 50 EMA": "—",
                    "vs W50 EMA": "—",
                    "Weekly RSI": "—",
                    "W50 Below Weeks": "—",
                    "Peak Drawdown": "—",
                    "Trend Score": "—",
                    "Momentum Score": "—",
                    "Drawdown Score": "—",
                    "P&L Score": "—",
                    "Confirmation Score": "—",
                    "Exit Score": "—",
                    "P&L %": "—",
                    "Signal": "⚠ Insufficient data",
                    "Action": "Check manually",
                })
                continue

            # --------------------------------------------------
            # Need at least 50 weeks for W50
            # --------------------------------------------------

            if len(close_series) < config.PORTFOLIO_WEEKLY_SLOW_EMA:

                results.append({
                    "Symbol": symbol,
                    "Entry Date": holding["entry_date"],
                    "Entry Price": holding["entry_price"],
                    "Current Price": "—",
                    "Weekly 20 EMA": "—",
                    "Weekly 50 EMA": "—",
                    "vs W50 EMA": "—",
                    "Weekly RSI": "—",
                    "W50 Below Weeks": "—",
                    "Peak Drawdown": "—",
                    "Trend Score": "—",
                    "Momentum Score": "—",
                    "Drawdown Score": "—",
                    "P&L Score": "—",
                    "Confirmation Score": "—",
                    "Exit Score": "—",
                    "P&L %": "—",
                    "Signal": "⚠ Insufficient data",
                    "Action": "Check manually",
                })
                continue

            closes = (
                close_series
                .astype(float)
                .tolist()
            )

            # --------------------------------------------------
            # Current price
            #
            # Prefer latest daily close. Fall back to weekly close
            # if daily data is unavailable.
            # --------------------------------------------------

            daily_close_series = pd.Series(dtype=float)

            if daily_raw is not None:
                daily_close_series = extract_series_with_dates(
                    daily_raw,
                    ticker,
                    "Close",
                    tickers,
                )

            if not daily_close_series.empty:
                current = float(daily_close_series.iloc[-1])
            else:
                current = float(closes[-1])

            # --------------------------------------------------
            # Phase 2B — Daily momentum snapshot for rotation
            #
            # These fields deliberately use the same daily indicators
            # as the normal universe screener so an existing holding
            # and a new candidate are comparable on the same basis.
            # --------------------------------------------------
            daily_closes = daily_close_series.astype(float).tolist()

            if len(daily_closes) >= 200:
                daily_ema50 = calc_ema(daily_closes, config.EMA_SHORT)
                daily_ema100 = calc_ema(daily_closes, config.EMA_MEDIUM)
                daily_ema200 = calc_ema(daily_closes, config.EMA_LONG)
                daily_rsi = calc_rsi(
                    daily_closes,
                    config.RSI_PERIOD,
                )
                daily_avg_vol = None

                daily_volume_series = pd.Series(dtype=float)
                if daily_raw is not None:
                    daily_volume_series = extract_series_with_dates(
                        daily_raw,
                        ticker,
                        "Volume",
                        tickers,
                    )

                if not daily_volume_series.empty:
                    daily_avg_vol = calc_average_volume(
                        daily_volume_series.astype(float).tolist(),
                        config.AVG_VOLUME_PERIOD,
                    )

                daily_high_52w, daily_pct_from_high = calc_52week_metrics(
                    daily_closes
                )
            else:
                daily_ema50 = None
                daily_ema100 = None
                daily_ema200 = None
                daily_rsi = None
                daily_avg_vol = None
                daily_high_52w = None
                daily_pct_from_high = None

            # --------------------------------------------------
            # Weekly EMA series
            # --------------------------------------------------

            ema20_series = calc_ema_series(
                closes,
                config.PORTFOLIO_WEEKLY_FAST_EMA,
            )

            ema50_series = calc_ema_series(
                closes,
                config.PORTFOLIO_WEEKLY_SLOW_EMA,
            )

            weekly_ema20 = ema20_series[-1]
            weekly_ema50 = ema50_series[-1]

            # --------------------------------------------------
            # Weekly RSI
            # --------------------------------------------------

            weekly_rsi = calc_rsi(
                closes,
                config.PORTFOLIO_WEEKLY_RSI_PERIOD,
            )

            # --------------------------------------------------
            # Distance from W50
            # --------------------------------------------------

            w50_buffer = (
                (current - weekly_ema50)
                / weekly_ema50
                * 100
            )

            # --------------------------------------------------
            # Consecutive weekly closes below THEIR W50
            #
            # This is important:
            # We compare each historical close against
            # that week's actual W50, not today's W50.
            # --------------------------------------------------

            below_w50_count = 0

            for i in range(
                len(closes) - 1,
                -1,
                -1,
            ):

                ema50 = ema50_series[i]

                if ema50 is None:
                    break

                if closes[i] < ema50:
                    below_w50_count += 1
                else:
                    break

            # --------------------------------------------------
            # Parse entry date
            # --------------------------------------------------

            entry_date_raw = holding.get(
                "entry_date",
                "—",
            )

            entry_date = None

            if entry_date_raw not in (
                None,
                "",
                "—",
                "nan",
            ):

                try:

                    entry_date = pd.to_datetime(
                        entry_date_raw,
                        dayfirst=True,
                        errors="coerce",
                    )

                except Exception:

                    entry_date = None

            # --------------------------------------------------
            # Peak since entry
            #
            # Use daily closes when available so drawdown reflects
            # the actual position history rather than only weekly
            # observations.
            # --------------------------------------------------

            peak_source = (
                daily_close_series
                if not daily_close_series.empty
                else close_series
            )

            if entry_date is not None and not pd.isna(entry_date):

                entry_mask = (
                    peak_source.index
                    >= entry_date
                )

                post_entry = peak_source.loc[
                    entry_mask
                ]

            else:

                # If Entry Date is unavailable,
                # use the complete available history.
                post_entry = peak_source

            if post_entry.empty:

                post_entry = peak_source

            peak_close = float(
                post_entry.max()
            )

            peak_drawdown = (
                (peak_close - current)
                / peak_close
                * 100
            )

            peak_drawdown = max(
                0.0,
                peak_drawdown,
            )

            # --------------------------------------------------
            # P&L
            # --------------------------------------------------

            entry_price = holding.get(
                "entry_price"
            )

            pnl = None

            if entry_price is not None:

                pnl = (
                    (current - entry_price)
                    / entry_price
                    * 100
                )
            # ==================================================
            # EXIT SCORE
            # ==================================================

            score = 0

            # --------------------------------------------------
            # 1. TREND — 35 points
            # --------------------------------------------------

            trend_score = 0

            # Price above weekly 20 EMA
            if current > weekly_ema20:
                trend_score += 7

            # Price above weekly 50 EMA
            if current > weekly_ema50:
                trend_score += 8

            # Weekly 20 EMA above weekly 50 EMA
            if weekly_ema20 > weekly_ema50:
                trend_score += 5

            # Strength of price position above W50
            if w50_buffer >= 20:
                trend_score += 15

            elif w50_buffer >= 15:
                trend_score += 10

            elif w50_buffer >= 10:
                trend_score += 6

            elif w50_buffer >= 5:
                trend_score += 3

            elif w50_buffer >= 0:
                trend_score += 1

            score += trend_score

            # --------------------------------------------------
            # 2. MOMENTUM — 20 points
            # --------------------------------------------------

            momentum_score = 0

            if weekly_rsi is not None:

                if weekly_rsi >= config.PORTFOLIO_RSI_HEALTHY:
                    momentum_score = 20

                elif weekly_rsi >= config.PORTFOLIO_RSI_WARNING:
                    momentum_score = 15

                elif weekly_rsi >= config.PORTFOLIO_RSI_SEVERE:
                    momentum_score = 8

                else:
                    momentum_score = 0

            score += momentum_score

            # --------------------------------------------------
            # 3. DRAWDOWN — 15 points
            # --------------------------------------------------

            if peak_drawdown < 5:
                drawdown_score = 15

            elif peak_drawdown < config.PORTFOLIO_DD_WARNING:
                drawdown_score = 11

            elif peak_drawdown < config.PORTFOLIO_DD_REDUCE:
                drawdown_score = 6

            else:
                drawdown_score = 0

            score += drawdown_score

            # --------------------------------------------------
            # 4. POSITION P&L — 10 points
            # --------------------------------------------------

            pnl_score = 0

            if pnl is not None:

                if pnl >= 10:
                    pnl_score = 10

                elif pnl >= 0:
                    pnl_score = 7

                elif pnl >= -5:
                    pnl_score = 4

                elif pnl >= -10:
                    pnl_score = 1

                else:
                    pnl_score = 0

            score += pnl_score

            # --------------------------------------------------
            # 5. W50 CONFIRMATION — 20 points
            # --------------------------------------------------

            if below_w50_count == 0:
                confirmation_score = 20

            elif below_w50_count == 1:
                confirmation_score = 10

            else:
                confirmation_score = 0

            score += confirmation_score

            # Final score
            score = int(score)

            # ==================================================
            # SIGNAL
            # ==================================================

            # ==================================================
            # HARD EXIT OVERRIDES
            # ==================================================

            # --------------------------------------------------
            # 1. Position loss reaches hard limit
            # --------------------------------------------------

            if (
                    pnl is not None
                    and pnl <= config.PORTFOLIO_HARD_EXIT_PNL
            ):

                signal = "🔴 EXIT"

                action = (
                    "Exit — position loss reached "
                    f"{config.PORTFOLIO_HARD_EXIT_PNL:.0f}%"
                )


            # --------------------------------------------------
            # 2. Confirmed weekly trend breakdown
            # --------------------------------------------------

            elif (
                    below_w50_count
                    >= config.PORTFOLIO_EXIT_CONFIRMATION_WEEKS
                    and weekly_rsi is not None
                    and weekly_rsi < config.PORTFOLIO_RSI_SEVERE
            ):

                signal = "🔴 EXIT"

                action = (
                    "Exit — confirmed weekly trend breakdown"
                )


            # --------------------------------------------------
            # 3. Normal score-based classification
            # --------------------------------------------------

            elif (
                    score >= config.PORTFOLIO_STRONG_HOLD_SCORE
            ):

                signal = "🟢 STRONG HOLD"

                action = (
                    "Trend healthy — continue holding"
                )


            elif (
                    score >= config.PORTFOLIO_HOLD_SCORE
            ):

                signal = "🟢 HOLD"

                action = "Hold position"


            elif (
                    score >= config.PORTFOLIO_WATCH_SCORE
            ):

                signal = "🟡 WATCH"

                action = (
                    "Momentum weakening — "
                    "review next cycle"
                )


            elif (
                    score >= config.PORTFOLIO_REDUCE_SCORE
            ):

                signal = "🟠 REDUCE"

                action = (
                    "Multiple warning signs — "
                    "consider reducing position"
                )


            else:

                signal = "🔴 EXIT"

                action = (
                    "Trend and momentum "
                    "significantly weakened"
                )

            # --------------------------------------------------
            # First W50 break warning
            # --------------------------------------------------

            if (
                below_w50_count == 1
                and "EXIT" not in signal
            ):

                action = (
                    "First weekly close below W50 — "
                    "monitor for confirmation"
                )

            # --------------------------------------------------
            # Formatting
            # --------------------------------------------------

            if pnl is None:

                pnl_str = "—"

            else:

                pnl_str = (
                    f"{'+' if pnl >= 0 else ''}"
                    f"{pnl:.1f}%"
                )

            w50_str = (
                f"{'+' if w50_buffer >= 0 else ''}"
                f"{w50_buffer:.1f}%"
            )

            results.append({

                "Symbol": symbol,

                "Entry Date":
                    entry_date_raw,

                "Entry Price":
                    (
                        f"₹{entry_price:,.2f}"
                        if entry_price is not None
                        else "—"
                    ),

                "Current Price":
                    round(current, 2),

                # Phase 2B daily rotation metrics
                "50 EMA":
                    round(daily_ema50, 2)
                    if daily_ema50 is not None else "—",
                "100 EMA":
                    round(daily_ema100, 2)
                    if daily_ema100 is not None else "—",
                "200 EMA":
                    round(daily_ema200, 2)
                    if daily_ema200 is not None else "—",
                "vs 50 EMA":
                    (
                        f"{'+' if current >= daily_ema50 else ''}"
                        f"{((current - daily_ema50) / daily_ema50 * 100):.1f}%"
                    )
                    if daily_ema50 is not None else "—",
                "vs 100 EMA":
                    (
                        f"{'+' if current >= daily_ema100 else ''}"
                        f"{((current - daily_ema100) / daily_ema100 * 100):.1f}%"
                    )
                    if daily_ema100 is not None else "—",
                "vs 200 EMA":
                    (
                        f"{'+' if current >= daily_ema200 else ''}"
                        f"{((current - daily_ema200) / daily_ema200 * 100):.1f}%"
                    )
                    if daily_ema200 is not None else "—",
                "RSI (14)":
                    round(daily_rsi, 1)
                    if daily_rsi is not None else "—",
                "Avg Vol (20d)":
                    (
                        f"{daily_avg_vol / 1e6:.2f}M"
                        if daily_avg_vol is not None else "—"
                    ),
                "52W High (₹)":
                    (
                        round(daily_high_52w, 2)
                        if daily_high_52w is not None else "—"
                    ),
                "% from 52W High":
                    (
                        f"-{daily_pct_from_high}%"
                        if daily_pct_from_high is not None else "—"
                    ),

                "Weekly 20 EMA":
                    round(weekly_ema20, 2),

                "Weekly 50 EMA":
                    round(weekly_ema50, 2),

                "vs W50 EMA":
                    w50_str,

                "Weekly RSI":
                    (
                        round(
                            weekly_rsi,
                            1,
                        )
                        if weekly_rsi is not None
                        else "—"
                    ),

                "W50 Below Weeks":
                    below_w50_count,

                "Peak Drawdown":
                    f"-{peak_drawdown:.1f}%",

                "Trend Score":
                    trend_score,

                "Momentum Score":
                    momentum_score,

                "Drawdown Score":
                    drawdown_score,

                "P&L Score":
                    pnl_score,

                "Confirmation Score":
                    confirmation_score,

                "Exit Score":
                    score,

                "P&L %":
                    pnl_str,

                "Signal":
                    signal,

                "Action":
                    action,
            })

        except Exception as e:

            results.append({

                "Symbol":
                    symbol,

                "Entry Date":
                    holding.get(
                        "entry_date",
                        "—",
                    ),

                "Entry Price":
                    holding.get(
                        "entry_price",
                        None,
                    ),

                "Current Price":
                    "—",

                "Weekly 20 EMA":
                    "—",

                "Weekly 50 EMA":
                    "—",

                "vs W50 EMA":
                    "—",

                "Weekly RSI":
                    "—",

                "W50 Below Weeks":
                    "—",

                "Peak Drawdown":
                    "—",

                "Trend Score":
                    "—",

                "Momentum Score":
                    "—",

                "Drawdown Score":
                    "—",

                "P&L Score":
                    "—",

                "Confirmation Score":
                    "—",

                "Exit Score":
                    "—",

                "P&L %":
                    "—",

                "Signal":
                    f"✗ Error: {str(e)[:60]}",

                "Action":
                    "Check manually",
            })

    # ----------------------------------------------------------
    # Sort by severity
    # ----------------------------------------------------------

    priority = {
        "🔴 EXIT": 0,
        "🟠 REDUCE": 1,
        "🟡 WATCH": 2,
        "🟢 HOLD": 3,
        "🟢 STRONG HOLD": 4,
    }

    results.sort(
        key=lambda x: priority.get(
            x.get("Signal", ""),
            99,
        )
    )

    return results

# ── Download ───────────────────────────────────────────────

def download_data(symbols, progress_callback=None):
    tickers = [s + ".NS" for s in symbols]
    if progress_callback:
        progress_callback(f"Downloading data for {len(tickers)} stocks from Yahoo Finance…")
    raw = yf.download(
        tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    return raw, tickers


# ── Screener ───────────────────────────────────────────────

def run_screener(raw, symbols, tickers, progress_callback=None):
    results  = []
    rejected = []
    skipped  = []
    errors   = []

    for i, symbol in enumerate(symbols):
        ticker = symbol + ".NS"
        if progress_callback and i % 10 == 0:
            progress_callback(f"Screening {i+1}/{len(symbols)}: {symbol}")
        try:
            closes  = extract_series(raw, ticker, "Close",  tickers)
            volumes = extract_series(raw, ticker, "Volume", tickers)

            if not closes or len(closes) < 200:
                skipped.append(f"{symbol} (only {len(closes)} days of data)")
                continue

            current = float(closes[-1])
            ema50   = calc_ema(closes, 50)
            ema100  = calc_ema(closes, 100)
            ema200  = calc_ema(closes, 200)
            ema_ok  = current > ema50 and current > ema100 and current > ema200

            rsi    = calc_rsi(
    closes,
    config.RSI_PERIOD,
)
            rsi_ok = rsi is not None and rsi > config.RSI_MIN

            avg_vol = calc_average_volume(
    volumes,
    config.AVG_VOLUME_PERIOD,
)
            vol_ok  = avg_vol is not None and avg_vol >= config.MIN_AVG_VOLUME

            high_52w, pct_from_high = calc_52week_metrics(closes)
            high_ok = pct_from_high is not None and pct_from_high <= config.NEAR_HIGH_PERCENT

            all_pass = ema_ok and rsi_ok and vol_ok and high_ok

            if all_pass:
                results.append({
                    "Symbol":          symbol,
                    "Price (₹)":       round(current, 2),
                    "50 EMA":          round(ema50,  2),
                    "100 EMA":         round(ema100, 2),
                    "200 EMA":         round(ema200, 2),
                    "vs 50 EMA":       f"{'+' if current >= ema50 else ''}{round((current-ema50)/ema50*100,1)}%",
                    "vs 100 EMA":      f"{'+' if current >= ema100 else ''}{round((current-ema100)/ema100*100,1)}%",
                    "vs 200 EMA":      f"{'+' if current >= ema200 else ''}{round((current-ema200)/ema200*100,1)}%",
                    "RSI (14)":        rsi,
                    "Avg Vol (20d)":   f"{avg_vol/1e6:.2f}M",
                    "52W High (₹)":    high_52w,
                    "% from 52W High": f"-{pct_from_high}%",
                })
            elif ema_ok:
                rsi_str  = f"{rsi:.1f}" if rsi else "—"
                vol_str  = f"{avg_vol/1e6:.1f}M" if avg_vol else "—"
                high_str = f"-{pct_from_high}%" if pct_from_high is not None else "—"
                rejected.append({
                    "symbol": symbol,
                    "reason": f"RSI={rsi_str} Vol={vol_str} 52wH={high_str}",
                })

        except Exception as e:
            errors.append(f"{symbol}: {str(e)[:70]}")

    return results, rejected, skipped, errors


# ── Score & rank ───────────────────────────────────────────

    def pct_rank(series, invert=False):
        ranked = series.rank(pct=True) * 100
        return (100 - ranked) if invert else ranked

    df["_ema200_raw"] = df["vs 200 EMA"].str.replace("+", "", regex=False).str.replace("%", "", regex=False).astype(float)
    df["_rsi_raw"]    = df["RSI (14)"].astype(float)
    df["_52w_raw"]    = df["% from 52W High"].str.replace("-", "", regex=False).str.replace("%", "", regex=False).astype(float)
    df["_vol_raw"]    = df["Avg Vol (20d)"].str.replace("M", "", regex=False).astype(float)

    df["Composite Score"] = (
        pct_rank(df["_ema200_raw"]) * W_EMA200 +
        pct_rank(df["_rsi_raw"])    * W_RSI    +
        pct_rank(df["_52w_raw"], invert=True) * W_52W +
        pct_rank(df["_vol_raw"])    * W_VOLUME
    ).round(1)

    df = df.sort_values("Composite Score", ascending=False)
    df.insert(0, "Rank", range(1, len(df) + 1))
    df = df.drop(columns=[c for c in df.columns if c.startswith("_")])

    if not universe_info.empty:
        df = df.merge(universe_info, on="Symbol", how="left")
        front = ["Rank", "Symbol"] + [c for c in ["Company Name", "Industry"] if c in df.columns]
        rest  = [c for c in df.columns if c not in front]
        df    = df[front + rest]

    return df

# ── Main entry point called by app.py ─────────────────────

def run_full_screen(file_bytes, holdings_bytes=None, progress_callback=None):
    """
    file_bytes     : universe CSV (required) — must have 'Symbol' column
    holdings_bytes : holdings CSV (optional) — columns: Symbol, Entry Date, Entry Price
    Returns dict: { top10, all_passed, stats, exit_signals, excel_bytes, run_date }
    """
    run_date = datetime.today().strftime("%d %b %Y")

    symbols, universe_info = load_symbols_from_bytes(file_bytes)

    if progress_callback:
        progress_callback(f"Loaded {len(symbols)} symbols from CSV")

    raw, tickers = download_data(symbols, progress_callback)
    results, rejected, skipped, errors = run_screener(raw, symbols, tickers, progress_callback)

    if progress_callback:
        progress_callback("Calculating composite scores…")

    df = rank_stocks(
    results,
    universe_info,
)

    # ── Exit signal check ──────────────────────────────────
    exit_signals = []
    if holdings_bytes:
        try:
            holdings = load_holdings_from_bytes(holdings_bytes)
            exit_signals = check_exit_signals(holdings, progress_callback)
        except Exception as e:
            if progress_callback:
                progress_callback(f"Holdings check skipped: {str(e)}")

    all_passed = df.to_dict(orient="records") if not df.empty else []

    # Phase 2B rotation review.
    # Imported lazily so the settled Phase 2A engine remains independently
    # usable and a rotation-module error does not break screening itself.
    rotation_review = {
        "reviews": [],
        "candidate_pool_size": 0,
        "replacement_count": 0,
        "cash_count": 0,
    }

    if exit_signals and all_passed:
        try:
            from rotation import build_rotation_review

            rotation_review = build_rotation_review(
                all_passed,
                exit_signals,
            )

            if progress_callback:
                progress_callback(
                    "Phase 2B rotation review completed."
                )
        except Exception as e:
            if progress_callback:
                progress_callback(
                    f"Phase 2B rotation review skipped: {str(e)[:100]}"
                )

    excel_bytes = build_excel(
        df,
        rejected,
        run_date,
        exit_signals,
    ) if not df.empty else None

    top10      = [r for r in all_passed if r.get("Rank", 99) <= 10]

    exit_count = sum(1 for e in exit_signals if "EXIT" in str(e.get("Signal", "")))

    stats = {
        "run_date":    run_date,
        "scanned":     len(symbols),
        "passed":      len(results),
        "ema_only":    len(rejected),
        "skipped":     len(skipped),
        "errors":      len(errors),
        "holdings":    len(exit_signals),
        "exit_alerts": exit_count,
    }

    return {
        "top10":        top10,
        "all_passed":   all_passed,
        "rejected":     rejected,
        "exit_signals": exit_signals,
        "rotation_review": rotation_review,
        "stats":        stats,
        "excel_bytes":  excel_bytes,
        "run_date":     run_date,
    }

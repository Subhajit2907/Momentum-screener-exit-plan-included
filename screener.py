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
    For each holding, fetch weekly OHLC and check if current price
    is below weekly 50 EMA.
    Returns list of dicts with exit signal status for each holding.
    """
    if not holdings:
        return []

    symbols = [h["symbol"] for h in holdings]
    tickers = [s + ".NS" for s in symbols]

    if progress_callback:
        progress_callback(f"Checking exit signals for {len(symbols)} holdings (weekly data)…")

    try:
        raw = yf.download(
            tickers,
            period="2y",        # 2 years for reliable weekly 50 EMA (needs 50 weeks)
            interval="1wk",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        return [{"symbol": h["symbol"], "error": str(e)} for h in holdings]

    results = []
    for holding in holdings:
        symbol = holding["symbol"]
        ticker = symbol + ".NS"
        try:
            closes = extract_series(raw, ticker, "Close", tickers)

            if not closes or len(closes) < 50:
                results.append({
                    "Symbol":       symbol,
                    "Entry Date":   holding["entry_date"],
                    "Entry Price":  holding["entry_price"],
                    "Current Price": "—",
                    "Weekly 50 EMA": "—",
                    "Signal":       "⚠ Insufficient data",
                    "P&L %":        "—",
                    "Action":       "Check manually",
                })
                continue

            current     = float(closes[-1])
            weekly_ema50 = calc_ema(closes, 50)
            exit_signal  = current < weekly_ema50

            # P&L if entry price available
            pnl_str = "—"
            if holding["entry_price"]:
                pnl = (current - holding["entry_price"]) / holding["entry_price"] * 100
                pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.1f}%"

            buffer = ((current - weekly_ema50) / weekly_ema50) * 100

            results.append({
                "Symbol":         symbol,
                "Entry Date":     holding["entry_date"],
                "Entry Price":    f"₹{holding['entry_price']:,.2f}" if holding["entry_price"] else "—",
                "Current Price":  round(current, 2),
                "Weekly 50 EMA": round(weekly_ema50, 2),
                "vs W50 EMA":    f"{'+' if buffer >= 0 else ''}{buffer:.1f}%",
                "P&L %":         pnl_str,
                "Signal":        "🚨 EXIT" if exit_signal else "✅ HOLD",
                "Action":        "Consider exiting — price below weekly 50 EMA" if exit_signal else "Hold position",
            })

        except Exception as e:
            results.append({
                "Symbol":       symbol,
                "Entry Date":   holding["entry_date"],
                "Entry Price":  holding["entry_price"],
                "Current Price": "—",
                "Weekly 50 EMA": "—",
                "Signal":       f"✗ Error: {str(e)[:50]}",
                "P&L %":        "—",
                "Action":       "Check manually",
            })

    # Sort — exits first, then holds
    results.sort(key=lambda x: 0 if "EXIT" in str(x.get("Signal","")) else 1)
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

            high_52w, pct_from_high = calc_52w_metrics(closes)
            high_ok = pct_from_high is not None and pct_from_high <= config.NEAR_HIGH_PERCENT

            all_pass = ema_ok and rsi_ok and vol_ok and high_ok

            if all_pass:
                results.append({
                    "Symbol":          symbol,
                    "Price (₹)":       round(current, 2),
                    "50 EMA":          round(ema50,  2),
                    "100 EMA":         round(ema100, 2),
                    "200 EMA":         round(ema200, 2),
                    "vs 50 EMA":       f"+{round((current-ema50)/ema50*100,1)}%",
                    "vs 200 EMA":      f"+{round((current-ema200)/ema200*100,1)}%",
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

    excel_bytes = build_excel(df, rejected, run_date, exit_signals) if not df.empty else None

    all_passed = df.to_dict(orient="records") if not df.empty else []
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
        "stats":        stats,
        "excel_bytes":  excel_bytes,
        "run_date":     run_date,
    }

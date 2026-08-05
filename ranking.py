"""
ranking.py

Composite ranking engine for the Momentum Screener.
"""

import pandas as pd

import config


def percentile_rank(series, invert=False):
    """
    Convert a numeric series into percentile ranks (0-100).

    Higher values receive higher scores unless invert=True.
    """

    ranked = series.rank(method="average", pct=True) * 100

    if invert:
        ranked = 100 - ranked

    return ranked


def prepare_numeric_columns(df):
    """
    Extract numeric values from formatted columns.
    """

    work = df.copy()

    work["_ema200"] = (
        work["vs 200 EMA"]
        .str.replace("+", "", regex=False)
        .str.replace("%", "", regex=False)
        .astype(float)
    )

    work["_rsi"] = (
        work["RSI (14)"]
        .astype(float)
    )

    work["_52wh"] = (
        work["% from 52W High"]
        .str.replace("-", "", regex=False)
        .str.replace("%", "", regex=False)
        .astype(float)
    )

    work["_volume"] = (
        work["Avg Vol (20d)"]
        .str.replace("M", "", regex=False)
        .astype(float)
    )

    return work


def calculate_composite_score(df):
    """
    Calculate composite score using configured weights.
    """

    df = prepare_numeric_columns(df)

    score_ema = (
        percentile_rank(df["_ema200"])
        * config.WEIGHT_EMA200
    )

    score_rsi = (
        percentile_rank(df["_rsi"])
        * config.WEIGHT_RSI
    )

    score_high = (
        percentile_rank(
            df["_52wh"],
            invert=True,
        )
        * config.WEIGHT_52W_HIGH
    )

    score_volume = (
        percentile_rank(df["_volume"])
        * config.WEIGHT_VOLUME
    )

    df["Composite Score"] = (
        score_ema
        + score_rsi
        + score_high
        + score_volume
    ).round(1)

    return df


def attach_company_information(df, universe_info):
    """
    Merge company metadata.
    """

    if universe_info.empty:
        return df

    merged = df.merge(
        universe_info,
        on="Symbol",
        how="left",
    )

    front = [
        "Rank",
        "Symbol",
    ]

    if "Company Name" in merged.columns:
        front.append("Company Name")

    if "Industry" in merged.columns:
        front.append("Industry")

    remaining = [
        column
        for column in merged.columns
        if column not in front
    ]

    return merged[front + remaining]


def remove_internal_columns(df):
    """
    Drop helper columns.
    """

    cols = [
        c
        for c in df.columns
        if c.startswith("_")
    ]

    return df.drop(columns=cols)


def rank_stocks(results, universe_info):
    """
    Main ranking function.

    Returns ranked DataFrame.
    """

    df = pd.DataFrame(results)

    if df.empty:
        return df

    df = calculate_composite_score(df)

    df = df.sort_values(
        "Composite Score",
        ascending=False,
    ).reset_index(drop=True)

    df.insert(
        0,
        "Rank",
        range(1, len(df) + 1),
    )

    df = remove_internal_columns(df)

    df = attach_company_information(
        df,
        universe_info,
    )

    return df
"""
Phase 2B portfolio rotation engine.

Phase 2A decides whether a holding is EXIT / REDUCE / WATCH /
HOLD / STRONG HOLD. Phase 2B never overrides an EXIT; it only
decides whether released capital should be replaced or held as cash.
"""

import math
import config


def _num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text or text in {"—", "-", "None", "nan", "NaN"}:
        return None
    text = text.replace("₹", "").replace(",", "").replace("%", "").replace("+", "")
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _rank(row):
    """Read the main screener rank using the supported column aliases."""
    for key in ("Rank", "rank", "#", "Top 10 Rank", "Top10 Rank"):
        value = _num(row.get(key))
        if value is not None:
            return int(value) if value.is_integer() else value
    return None


def _symbol(row):
    return str(row.get("Symbol", row.get("symbol", ""))).strip().upper()


def _signal(row):
    return str(row.get("Signal", "")).upper()


def _pct(row, *keys):
    for key in keys:
        value = _num(row.get(key))
        if value is not None:
            return value
    return None


def _ema_score(row):
    values = []
    for key in ("vs 50 EMA", "vs 100 EMA", "vs 200 EMA"):
        value = _pct(row, key)
        if value is not None:
            values.append(value)

    # Temporary fallback for Phase 2A rows until daily momentum
    # fields are exposed by check_exit_signals().
    if not values:
        value = _pct(row, "vs W50 EMA")
        if value is not None:
            values.append(value)

    if not values:
        return 0.0

    # -10% -> 0, +30% -> 30 points.
    scores = [
        max(0.0, min(100.0, ((v + 10.0) / 40.0) * 100.0))
        for v in values
    ]
    return (sum(scores) / len(scores)) * 0.30


def _rsi_score(row):
    value = _num(row.get("RSI (14)"))
    if value is None:
        value = _num(row.get("Weekly RSI"))
    if value is None:
        return 0.0

    normalized = max(0.0, min(100.0, ((value - 50.0) / 30.0) * 100.0))
    return normalized * 0.25


def _high_score(row):
    distance = _pct(row, "% from 52W High", "52W Distance")
    if distance is None:
        return 0.0

    threshold = float(getattr(config, "NEAR_HIGH_PERCENT", 20))
    if threshold <= 0:
        return 0.0

    normalized = max(0.0, min(1.0, 1.0 - abs(distance) / threshold))
    return normalized * 25.0


def _volume_score(row):
    raw = row.get("Avg Vol (20d)", row.get("Avg Volume"))
    if raw is None:
        return 0.0

    text = str(raw).strip().upper()
    multiplier = 1.0
    if "M" in text:
        multiplier = 1_000_000
        text = text.replace("M", "")
    elif "K" in text:
        multiplier = 1_000
        text = text.replace("K", "")

    volume = _num(text)
    minimum = float(getattr(config, "MIN_AVG_VOLUME", 500_000))
    if volume is None or minimum <= 0:
        return 0.0

    normalized = max(0.0, min(1.0, (volume * multiplier) / (minimum * 3.0)))
    return normalized * 20.0


def momentum_strength_score(row):
    """
    Absolute 0-100 score used only for rotation comparison.

    EMA trend 30 + RSI 25 + 52W-high strength 25 + volume 20.
    """
    score = (
        _ema_score(row)
        + _rsi_score(row)
        + _high_score(row)
        + _volume_score(row)
    )
    return int(round(max(0.0, min(100.0, score))))


def _min_advantage(signal):
    if "EXIT" in signal:
        return int(getattr(config, "ROTATION_EXIT_MIN_ADVANTAGE", 10))
    if "REDUCE" in signal:
        return int(getattr(config, "ROTATION_REDUCE_MIN_ADVANTAGE", 15))
    return int(getattr(config, "ROTATION_WATCH_MIN_ADVANTAGE", 20))


def prepare_candidates(passed_rows, held_symbols):
    """
    Return the Top-N screener picks that are not already held.

    Phase 2B deliberately uses the main screener's Top-N universe rather
    than a second hard momentum-score cutoff. The replacement decision is
    then made using the required momentum advantage against the position
    being replaced.
    """
    held = {str(s).strip().upper() for s in held_symbols if s}
    top_count = int(getattr(config, "TOP_PICK_COUNT", 10))

    candidates = []
    for row in passed_rows or []:
        symbol = _symbol(row)
        if not symbol or symbol in held:
            continue

        # Only the main screener's Top-N picks enter the rotation pool.
        # This keeps candidate selection aligned with the primary strategy.
        rank = _rank(row)
        if rank is None or rank > top_count:
            continue

        item = dict(row)
        item["Momentum Strength"] = momentum_strength_score(row)
        candidates.append(item)

    candidates.sort(
        key=lambda r: (
            _rank(r) if _rank(r) is not None else 999,
            -int(r.get("Momentum Strength", 0)),
            -(_num(r.get("Composite Score")) or 0),
            _symbol(r),
        )
    )
    return candidates


def rank_replacements(holding, candidates):
    """Rank all eligible replacements for one WATCH/REDUCE/EXIT holding."""
    existing_score = momentum_strength_score(holding)
    required = _min_advantage(_signal(holding))

    ranked = []
    for candidate in candidates:
        candidate_score = int(candidate["Momentum Strength"])
        advantage = candidate_score - existing_score

        ranked.append({
            "Candidate": _symbol(candidate),
            "Candidate Rank": _rank(candidate) if _rank(candidate) is not None else "—",
            "Candidate Momentum": candidate_score,
            "Existing Momentum": existing_score,
            "Advantage": advantage,
            "Price (₹)": candidate.get("Price (₹)", "—"),
            "Composite Score": candidate.get("Composite Score", "—"),
            "Meets Advantage": advantage >= required,
        })

    # IMPORTANT: candidate selection follows the MAIN SCREENER RANK.
    # Momentum Advantage is only the qualification gate. Once a candidate
    # clears that gate, the best-ranked (lowest Rank number) candidate wins.
    # This is the agreed portfolio-rotation hierarchy.
    ranked.sort(
        key=lambda r: (
            _num(r["Candidate Rank"]) if _num(r["Candidate Rank"]) is not None else 999,
            -int(r["Candidate Momentum"]),
            -int(r["Advantage"]),
            str(r["Candidate"]),
        )
    )
    return ranked


def build_rotation_review(passed_rows, exit_signals):
    """
    Build Phase 2B decisions.

    EXIT:
        suitable replacement -> REPLACE
        no suitable replacement -> CASH

    REDUCE:
        suitable replacement -> ROTATE
        otherwise -> KEEP

    WATCH:
        suitable replacement -> ROTATION REVIEW
        otherwise -> KEEP

    HOLD / STRONG HOLD are deliberately excluded.
    """
    portfolio = list(exit_signals or [])
    candidates = prepare_candidates(
        passed_rows or [],
        [_symbol(row) for row in portfolio],
    )

    maximum = int(getattr(config, "ROTATION_MAX_CANDIDATES", 5))
    reviews = []

    # A candidate can be allocated to ONLY ONE portfolio position in a
    # single screening run. EXIT positions get first priority, followed
    # by REDUCE and then WATCH. This prevents one stock from being shown
    # as the replacement for multiple positions.
    priority = {"EXIT": 0, "REDUCE": 1, "WATCH": 2}

    actionable = [
        holding
        for holding in portfolio
        if any(x in _signal(holding) for x in ("EXIT", "REDUCE", "WATCH"))
    ]

    actionable.sort(
        key=lambda holding: (
            min(
                (
                    priority[x]
                    for x in ("EXIT", "REDUCE", "WATCH")
                    if x in _signal(holding)
                ),
                default=99,
            ),
            momentum_strength_score(holding),
            _symbol(holding),
        )
    )

    allocated_candidates = set()

    for holding in actionable:
        signal = _signal(holding)

        # Only candidates not already allocated to an earlier holding
        # remain available for this position.
        available_candidates = [
            candidate
            for candidate in candidates
            if _symbol(candidate) not in allocated_candidates
        ]

        ranked = rank_replacements(
            holding,
            available_candidates,
        )

        # Select the strongest qualifying candidate from the same ranked
        # list that is displayed. This keeps recommendation and ranking
        # perfectly synchronized.
        suitable = [
            r
            for r in ranked
            if r["Meets Advantage"]
        ]

        best = suitable[0] if suitable else None

        if best:
            allocated_candidates.add(best["Candidate"])

        if "EXIT" in signal:
            if best:
                outcome = "REPLACE"
                recommendation = (
                    f"Exit {_symbol(holding)} and replace with {best['Candidate']}"
                )
            else:
                outcome = "CASH"
                recommendation = (
                    "Exit position and park released capital in cash equivalents "
                    "until a suitable candidate emerges."
                )
        elif "REDUCE" in signal:
            if best:
                outcome = "ROTATE"
                recommendation = (
                    f"Consider replacing {_symbol(holding)} with {best['Candidate']}"
                )
            else:
                outcome = "KEEP"
                recommendation = (
                    "No sufficiently stronger replacement candidate found."
                )
        else:
            if best:
                outcome = "ROTATION REVIEW"
                recommendation = (
                    f"Consider replacing {_symbol(holding)} with {best['Candidate']}"
                )
            else:
                outcome = "KEEP"
                recommendation = (
                    "No sufficiently stronger replacement candidate found."
                )

        reviews.append({
            "Existing Symbol": _symbol(holding),
            "Phase 2A Signal": holding.get("Signal", "—"),
            "Existing Exit Score": holding.get("Exit Score", "—"),
            "Existing Momentum": momentum_strength_score(holding),
            "Required Advantage": _min_advantage(signal),
            "Outcome": outcome,
            "Recommendation": recommendation,
            "Best Candidate": best["Candidate"] if best else "No candidate",
            "Best Candidate Rank": best["Candidate Rank"] if best else "—",
            "Best Candidate Momentum": (
                best["Candidate Momentum"] if best else "—"
            ),
            "Best Advantage": best["Advantage"] if best else "—",
            "Candidate Allocated": bool(best),
            "Candidates": [best] if best else [],
            "Candidate Alternatives": ranked[1:maximum] if best else ranked[:maximum],
        })

    return {
        "reviews": reviews,
        "candidate_pool_size": len(candidates),
        "replacement_count": sum(
            r["Outcome"] in {"REPLACE", "ROTATE", "ROTATION REVIEW"}
            for r in reviews
        ),
        "cash_count": sum(r["Outcome"] == "CASH" for r in reviews),
    }


def flatten_rotation_rows(review):
    """Flatten nested candidate rankings for HTML/Excel reporting."""
    rows = []

    for item in review.get("reviews", []):
        candidates = item.get("Candidates", [])

        if not candidates:
            rows.append({
                "Existing Symbol": item["Existing Symbol"],
                "Phase 2A Signal": item["Phase 2A Signal"],
                "Outcome": item["Outcome"],
                "Candidate": "No candidate",
                "Candidate Momentum": "—",
                "Existing Momentum": item["Existing Momentum"],
                "Advantage": "—",
                "Required Advantage": item["Required Advantage"],
                "Recommendation": item["Recommendation"],
            })
            continue

        for candidate in candidates:
            rows.append({
                "Existing Symbol": item["Existing Symbol"],
                "Phase 2A Signal": item["Phase 2A Signal"],
                "Outcome": item["Outcome"],
                "Candidate": candidate["Candidate"],
                "Candidate Rank": candidate["Candidate Rank"],
                "Candidate Momentum": candidate["Candidate Momentum"],
                "Existing Momentum": candidate["Existing Momentum"],
                "Advantage": candidate["Advantage"],
                "Required Advantage": item["Required Advantage"],
                "Recommendation": item["Recommendation"],
            })

    return rows

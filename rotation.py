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
    """Return strong, not-already-held candidates ranked by strength."""
    held = {str(s).strip().upper() for s in held_symbols if s}
    minimum = int(getattr(config, "ROTATION_MIN_CANDIDATE_SCORE", 80))

    candidates = []
    for row in passed_rows or []:
        symbol = _symbol(row)
        if not symbol or symbol in held:
            continue

        score = momentum_strength_score(row)
        if score < minimum:
            continue

        item = dict(row)
        item["Momentum Strength"] = score
        candidates.append(item)

    candidates.sort(
        key=lambda r: (
            -int(r.get("Momentum Strength", 0)),
            -(_num(r.get("Composite Score")) or 0),
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
            "Candidate Rank": candidate.get("Rank", "—"),
            "Candidate Momentum": candidate_score,
            "Existing Momentum": existing_score,
            "Advantage": advantage,
            "Price (₹)": candidate.get("Price (₹)", "—"),
            "Composite Score": candidate.get("Composite Score", "—"),
            "Meets Advantage": advantage >= required,
        })

    ranked.sort(
        key=lambda r: (-int(r["Advantage"]), -int(r["Candidate Momentum"]))
    )
    return ranked


def _rotation_priority(signal):
    """Lower value means higher portfolio urgency."""
    signal = str(signal or "").upper()
    if "EXIT" in signal:
        return 0
    if "REDUCE" in signal:
        return 1
    return 2


def _assignment_objective(result):
    """
    Return a hierarchical lexicographic objective for the global assignment.

    Portfolio urgency is deliberately part of the objective. A hard EXIT
    must be optimized before a REDUCE, and a REDUCE before a WATCH. Within
    each urgency tier we then optimize the existing rotation quality metrics.

    Priority within each tier:
      1. Assign as many positions as possible.
      2. Maximise total advantage.
      3. Maximise the weakest assigned advantage.
      4. Maximise total candidate momentum.
      5. Maximise total candidate composite score.
      6. Prefer stronger/stabler candidate ranks as the final tie-break.

    This prevents a lower-urgency holding from taking the best candidate away
    from a hard EXIT merely because the combined raw advantage happens to tie.
    """
    assigned = result.get("assigned", [])
    objective = []

    for priority in (0, 1, 2):
        group = [
            item for item in assigned
            if int(item.get("Rotation Priority", 2)) == priority
        ]

        if group:
            min_advantage = min(item["Advantage"] for item in group)
        else:
            min_advantage = -10**9

        objective.extend((
            len(group),
            sum(item["Advantage"] for item in group),
            min_advantage,
            sum(item["Candidate Momentum"] for item in group),
            sum(_num(item.get("Composite Score")) or 0 for item in group),
            -sum(_num(item.get("Candidate Rank")) or 10**6 for item in group),
        ))

    return tuple(objective)


def _better_assignment(left, right):
    """Return whichever assignment has the stronger global objective."""
    if right is None:
        return left
    if _assignment_objective(left) > _assignment_objective(right):
        return left
    return right


def _solve_one_to_one_assignment(reviews):
    """
    Assign distinct eligible candidates to multiple portfolio positions.

    The solver is exact for the small rotation pools used by the application.
    Candidate/holding pairs must already meet the existing Phase 2B advantage
    threshold, so this function changes allocation behaviour, not eligibility
    or scoring.

    IMPORTANT: assignment is optimized hierarchically by Phase 2A urgency:

        EXIT > REDUCE > WATCH

    Thus a hard EXIT gets first priority for the best available replacement.
    Within each urgency tier, the existing global assignment objective is
    preserved.
    """
    if not reviews:
        return {}

    candidate_symbols = sorted({
        candidate["Candidate"]
        for review in reviews
        for candidate in review.get("Candidates", [])
        if candidate.get("Meets Advantage")
    })

    if not candidate_symbols:
        return {}

    # The rotation pool is normally small. Guard against pathological input
    # while retaining an exact solution for the normal case.
    if len(candidate_symbols) > 20:
        candidate_symbols = candidate_symbols[:20]

    candidate_index = {
        symbol: index
        for index, symbol in enumerate(candidate_symbols)
    }

    edges = []
    for review in reviews:
        by_candidate = {}
        priority = _rotation_priority(review.get("Phase 2A Signal"))
        for candidate in review.get("Candidates", []):
            symbol = candidate["Candidate"]
            if not candidate.get("Meets Advantage"):
                continue
            if symbol in candidate_index:
                item = dict(candidate)
                item["Rotation Priority"] = priority
                by_candidate[symbol] = item
        edges.append(by_candidate)

    memo = {}

    def solve(position_index, used_mask):
        key = (position_index, used_mask)
        if key in memo:
            return memo[key]

        if position_index >= len(reviews):
            result = {"assigned": [], "skipped": []}
            memo[key] = result
            return result

        # Option 1: leave this position unassigned.
        tail = solve(position_index + 1, used_mask)
        best = {
            "assigned": list(tail["assigned"]),
            "skipped": [position_index] + list(tail["skipped"]),
        }

        # Option 2: assign each currently unused suitable candidate.
        for symbol, candidate in edges[position_index].items():
            bit = 1 << candidate_index[symbol]
            if used_mask & bit:
                continue

            tail = solve(position_index + 1, used_mask | bit)
            option = {
                "assigned": [
                    {
                        "Position Index": position_index,
                        **candidate,
                    }
                ] + list(tail["assigned"]),
                "skipped": list(tail["skipped"]),
            }

            best = _better_assignment(option, best)

        memo[key] = best
        return best

    solution = solve(0, 0)

    return {
        item["Position Index"]: item
        for item in solution["assigned"]
    }

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

    When multiple positions require rotation, replacement candidates are
    assigned globally and one-to-one. The same candidate cannot be used for
    two positions in the same rotation batch.
    """
    portfolio = list(exit_signals or [])
    candidates = prepare_candidates(
        passed_rows or [],
        [_symbol(row) for row in portfolio],
    )

    maximum = int(getattr(config, "ROTATION_MAX_CANDIDATES", 5))
    reviews = []

    for holding in portfolio:
        signal = _signal(holding)
        if not any(x in signal for x in ("EXIT", "REDUCE", "WATCH")):
            continue

        ranked = rank_replacements(holding, candidates)

        reviews.append({
            "Existing Symbol": _symbol(holding),
            "Phase 2A Signal": holding.get("Signal", "—"),
            "Existing Exit Score": holding.get("Exit Score", "—"),
            "Existing Momentum": momentum_strength_score(holding),
            "Required Advantage": _min_advantage(signal),
            "Outcome": "PENDING",
            "Recommendation": "Pending global rotation assignment.",
            "Best Candidate": "No candidate",
            "Best Candidate Momentum": "—",
            "Best Advantage": "—",
            "Assigned Candidate": None,
            "Assigned Candidate Momentum": "—",
            "Assigned Advantage": "—",
            "Candidates": ranked[:maximum],
        })

    # The assignment must see the full candidate ranking, not only the
    # display-limited top N candidates. Keep the displayed list capped while
    # retaining the complete ranking for allocation.
    assignment_reviews = []
    for holding_review in reviews:
        signal = _signal(holding_review)
        # Rebuild from the full pool for assignment.
        full_ranked = rank_replacements(
            next(
                row for row in portfolio
                if _symbol(row) == holding_review["Existing Symbol"]
            ),
            candidates,
        )
        assignment_reviews.append({
            **holding_review,
            "Candidates": full_ranked,
        })

    assignments = _solve_one_to_one_assignment(assignment_reviews)

    for index, review in enumerate(reviews):
        # Find the matching full review by position. Existing symbols are
        # expected to be unique in a holdings file.
        assignment = assignments.get(index)

        signal = _signal(
            next(
                row for row in portfolio
                if _symbol(row) == review["Existing Symbol"]
            )
        )

        if assignment:
            candidate = assignment["Candidate"]
            review["Assigned Candidate"] = candidate
            review["Assigned Candidate Momentum"] = assignment["Candidate Momentum"]
            review["Assigned Advantage"] = assignment["Advantage"]
            review["Best Candidate"] = candidate
            review["Best Candidate Momentum"] = assignment["Candidate Momentum"]
            review["Best Advantage"] = assignment["Advantage"]

            if "EXIT" in signal:
                outcome = "REPLACE"
                recommendation = (
                    f"Exit {review['Existing Symbol']} and replace with {candidate}"
                )
            elif "REDUCE" in signal:
                outcome = "ROTATE"
                recommendation = (
                    f"Consider replacing {review['Existing Symbol']} with {candidate}"
                )
            else:
                outcome = "ROTATION REVIEW"
                recommendation = (
                    f"Consider replacing {review['Existing Symbol']} with {candidate}"
                )

            review["Outcome"] = outcome
            review["Recommendation"] = recommendation
        else:
            # No distinct candidate was available for this position after
            # the global assignment. Do not reuse a candidate.
            if "EXIT" in signal:
                review["Outcome"] = "CASH"
                review["Recommendation"] = (
                    "Exit position and park released capital in cash equivalents "
                    "until a suitable distinct candidate emerges."
                )
            else:
                review["Outcome"] = "KEEP"
                review["Recommendation"] = (
                    "No distinct replacement candidate remained after global "
                    "one-to-one allocation."
                )

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
                "Assigned": (
                    item.get("Assigned Candidate") == candidate["Candidate"]
                ),
                "Candidate Rank": candidate["Candidate Rank"],
                "Candidate Momentum": candidate["Candidate Momentum"],
                "Existing Momentum": candidate["Existing Momentum"],
                "Advantage": candidate["Advantage"],
                "Required Advantage": item["Required Advantage"],
                "Recommendation": item["Recommendation"],
            })

    return rows

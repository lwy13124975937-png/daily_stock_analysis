#!/usr/bin/env python3
"""Run a real, no-LLM steady-income shortlist sensitivity audit."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import hashlib
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.build_steady_income_report import PublicMarketSource, SteadyIncomeDatasetBuilder
from src.reports.contracts import write_json_atomic


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_BUDGETS = (30, 60, 120, 240)
_TERMINAL_STATUSES = (
    "evaluated_qualified",
    "evaluated_rejected",
    "insufficient_evidence",
    "unsupported_sector_model",
    "provider_failure",
    "internal_error",
)


def _ranked_qualified_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparable = [
        item
        for item in records
        if item.get("qualified") and isinstance(item.get("ranking_score"), (int, float))
    ]
    comparable.sort(key=lambda item: (-float(item["ranking_score"]), str(item.get("code") or "")))
    return comparable


def _ranked_qualified(records: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("code") or "") for item in _ranked_qualified_records(records)]


def _spearman(left: list[str], right: list[str]) -> float | None:
    """Rank correlation for common codes after reranking the common subset."""

    right_set = set(right)
    common = [code for code in left if code in right_set]
    if len(common) < 2:
        return None
    common_set = set(common)
    left_common = [code for code in left if code in common_set]
    right_common = [code for code in right if code in common_set]
    left_rank = {code: index + 1 for index, code in enumerate(left_common)}
    right_rank = {code: index + 1 for index, code in enumerate(right_common)}
    squared = sum((left_rank[code] - right_rank[code]) ** 2 for code in common)
    count = len(common)
    return round(1.0 - (6.0 * squared) / (count * (count * count - 1)), 6)


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average = ((cursor + 1) + end) / 2.0
        for original_index, _value in ordered[cursor:end]:
            ranks[original_index] = average
        cursor = end
    return ranks


def _numeric_spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = _average_ranks([pair[0] for pair in pairs])
    right = _average_ranks([pair[1] for pair in pairs])
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((value - left_mean) ** 2 for value in left)
    right_var = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_var * right_var)
    return round(numerator / denominator, 6) if denominator else None


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _distribution(values: Iterable[float]) -> dict[str, Any]:
    normalized = sorted(float(value) for value in values)
    if not normalized:
        return {
            "count": 0, "min": None, "p25": None, "median": None,
            "mean": None, "p75": None, "max": None,
        }
    return {
        "count": len(normalized),
        "min": round(normalized[0], 6),
        "p25": round(float(_percentile(normalized, 0.25)), 6),
        "median": round(float(_percentile(normalized, 0.5)), 6),
        "mean": round(statistics.fmean(normalized), 6),
        "p75": round(float(_percentile(normalized, 0.75)), 6),
        "max": round(normalized[-1], 6),
    }


def _binary_auc(records: list[dict[str, Any]]) -> float | None:
    scored: list[tuple[float, bool]] = []
    for item in records:
        score = (item.get("preselection") or {}).get("seed_score")
        if not isinstance(score, (int, float)):
            continue
        if item.get("terminal_status") not in {"evaluated_qualified", "evaluated_rejected"}:
            continue
        scored.append((float(score), bool(item.get("qualified"))))
    positive = [score for score, label in scored if label]
    negative = [score for score, label in scored if not label]
    if not positive or not negative:
        return None
    wins = sum(
        1.0 if left > right else 0.5 if left == right else 0.0
        for left in positive
        for right in negative
    )
    return round(wins / (len(positive) * len(negative)), 6)


def _comparison(shortlist: list[str], reference: list[str]) -> dict[str, Any]:
    shortlist_set = set(shortlist)
    reference_set = set(reference)
    overlap = shortlist_set & reference_set
    union = shortlist_set | reference_set
    reference_rank = {code: index + 1 for index, code in enumerate(reference)}
    shortlist_reference_ranks = {
        code: reference_rank[code] for code in shortlist if code in reference_rank
    }
    worst_shortlist_rank = max(shortlist_reference_ranks.values(), default=0)
    later_above = [
        {"code": code, "reference_rank": reference_rank[code]}
        for code in reference
        if code not in shortlist_set and reference_rank[code] < worst_shortlist_rank
    ]
    top_k: dict[str, Any] = {}
    for requested in (3, 5, 10):
        effective = min(requested, len(shortlist), len(reference))
        top_k[str(requested)] = {
            "requested_n": requested,
            "effective_n": effective,
            "overlap": len(set(shortlist[:effective]) & set(reference[:effective])) if effective else 0,
        }
    return {
        "qualified_set_overlap": len(overlap),
        "jaccard": round(len(overlap) / len(union), 6) if union else None,
        "recall_of_reference_qualified": round(len(overlap) / len(reference_set), 6) if reference_set else None,
        "top_k": top_k,
        "shortlist_qualified_reference_ranks": shortlist_reference_ranks,
        "later_qualified_above_shortlist": later_above,
    }


def _prefilter_analysis(records: list[dict[str, Any]], *, upper_position: int) -> dict[str, Any]:
    complete = [
        item
        for item in records
        if item.get("terminal_status") in {"evaluated_qualified", "evaluated_rejected"}
    ]
    qualified = [item for item in complete if item.get("qualified")]
    rejected = [item for item in complete if not item.get("qualified")]

    def seed_scores(items: list[dict[str, Any]]) -> list[float]:
        return [
            float((item.get("preselection") or {}).get("seed_score"))
            for item in items
            if isinstance((item.get("preselection") or {}).get("seed_score"), (int, float))
        ]

    bucket_bounds = [(1, 30), (31, 60), (61, 90), (91, 120)]
    if upper_position > 120:
        bucket_bounds.append((121, upper_position))
    buckets = []
    for start, end in bucket_bounds:
        items = [
            item
            for item in records
            if start <= int((item.get("preselection") or {}).get("deep_queue_position") or 0) <= end
        ]
        completed = [
            item
            for item in items
            if item.get("terminal_status") in {"evaluated_qualified", "evaluated_rejected"}
        ]
        qualified_count = sum(bool(item.get("qualified")) for item in completed)
        buckets.append(
            {
                "positions": f"{start}-{end}",
                "evaluated_count": len(items),
                "completed_count": len(completed),
                "qualified_count": qualified_count,
                "qualified_rate": round(qualified_count / len(completed), 6) if completed else None,
            }
        )
    score_pairs = [
        (float((item.get("preselection") or {}).get("seed_score")), float(item["ranking_score"]))
        for item in records
        if isinstance((item.get("preselection") or {}).get("seed_score"), (int, float))
        and isinstance(item.get("ranking_score"), (int, float))
    ]
    qualified_distribution = _distribution(seed_scores(qualified))
    rejected_distribution = _distribution(seed_scores(rejected))
    return {
        "qualified_prefilter_score_distribution": qualified_distribution,
        "rejected_prefilter_score_distribution": rejected_distribution,
        "qualified_rate_by_deep_position_bucket": buckets,
        "prefilter_score_vs_final_score_spearman": _numeric_spearman(score_pairs),
        "prefilter_score_vs_final_score_sample_size": len(score_pairs),
        "prefilter_score_qualified_auc": _binary_auc(records),
        "qualified_minus_rejected_median": (
            round(float(qualified_distribution["median"]) - float(rejected_distribution["median"]), 6)
            if qualified_distribution["median"] is not None
            and rejected_distribution["median"] is not None
            else None
        ),
    }


def summarize_selection_sensitivity(
    payload: dict[str, Any],
    *,
    budgets: Iterable[int] = DEFAULT_BUDGETS,
    reference_budget: int | None = None,
    elapsed_seconds: float | None = None,
    runtime_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = sorted({int(value) for value in budgets if int(value) > 0})
    if not normalized:
        raise ValueError("at least one positive deep budget is required")
    all_records = list(payload.get("candidates") or []) + list(payload.get("excluded") or [])
    all_records.sort(
        key=lambda item: int((item.get("preselection") or {}).get("deep_queue_position") or 0)
    )
    positions = [
        int((item.get("preselection") or {}).get("deep_queue_position") or 0)
        for item in all_records
    ]
    if positions != list(range(1, len(all_records) + 1)):
        raise ValueError("deep queue positions must be unique and contiguous")
    maximum = len(all_records)
    normalized = [budget for budget in normalized if budget <= maximum]
    if not normalized or normalized[-1] != maximum:
        normalized.append(maximum)
    reference = reference_budget if reference_budget in normalized else (
        120 if 120 in normalized else maximum
    )
    by_budget = {
        budget: [
            item
            for item in all_records
            if int((item.get("preselection") or {}).get("deep_queue_position") or 0) <= budget
        ]
        for budget in normalized
    }
    queue_codes = {
        budget: [str(item.get("code") or "") for item in records]
        for budget, records in by_budget.items()
    }
    strict_nested = all(
        queue_codes[previous] == queue_codes[current][:len(queue_codes[previous])]
        for previous, current in zip(normalized, normalized[1:])
    )
    if not strict_nested:
        raise ValueError("deep budget queues are not strict prefixes")
    baseline_budget = normalized[0]
    baseline_ranked = _ranked_qualified(by_budget[baseline_budget])
    reference_ranked = _ranked_qualified(by_budget[reference])
    maximum_ranked = _ranked_qualified(by_budget[maximum])
    rows: list[dict[str, Any]] = []
    previous_codes: list[str] = []
    for budget in normalized:
        records = by_budget[budget]
        ranked = _ranked_qualified(records)
        terminal = Counter(str(item.get("terminal_status") or "internal_error") for item in records)
        terminal_total = sum(terminal[status] for status in _TERMINAL_STATUSES)
        if terminal_total != len(records):
            raise ValueError(
                f"terminal status arithmetic mismatch for deep={budget}: "
                f"terminal_total={terminal_total} records={len(records)}"
            )
        completed = terminal["evaluated_qualified"] + terminal["evaluated_rejected"]
        rows.append(
            {
                "deep_budget": budget,
                "deep_evaluated": len(records),
                "queue_codes": queue_codes[budget],
                "queue_fingerprint": hashlib.sha256(
                    "\n".join(queue_codes[budget]).encode("utf-8")
                ).hexdigest(),
                "strict_prefix_of_next": None,
                "strictly_extends_previous": (
                    not previous_codes
                    or previous_codes == queue_codes[budget][:len(previous_codes)]
                ),
                "completed_evaluation_count": completed,
                "qualified_count": len(ranked),
                "evaluated_rejected_count": terminal["evaluated_rejected"],
                "insufficient_evidence_count": terminal["insufficient_evidence"],
                "unsupported_sector_model_count": terminal["unsupported_sector_model"],
                "provider_failure_count": terminal["provider_failure"],
                "internal_error_count": terminal["internal_error"],
                "qualified_rate": round(len(ranked) / completed, 6) if completed else None,
                "versus_reference": _comparison(ranked, reference_ranked),
                "versus_maximum": _comparison(ranked, maximum_ranked),
                "rank_correlation_with_first_budget": _spearman(baseline_ranked, ranked),
                "qualified_added_vs_first_budget": [
                    code for code in ranked if code not in set(baseline_ranked)
                ],
                "terminal_status_distribution": dict(sorted(terminal.items())),
                "provider_failure_rate": (
                    round(terminal["provider_failure"] / len(records), 6) if records else None
                ),
                "data_insufficient_rate": (
                    round(terminal["insufficient_evidence"] / len(records), 6)
                    if records
                    else None
                ),
                "success_rate": round(completed / len(records), 6) if records else None,
            }
        )
        previous_codes = queue_codes[budget]
    for index in range(len(rows) - 1):
        rows[index]["strict_prefix_of_next"] = (
            rows[index]["queue_codes"]
            == rows[index + 1]["queue_codes"][:len(rows[index]["queue_codes"])]
        )

    def qualified_details(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked_records = _ranked_qualified_records(records)
        rank_by_code = {
            str(item.get("code") or ""): index + 1
            for index, item in enumerate(ranked_records)
        }
        details = []
        for item in ranked_records:
            preselection = item.get("preselection") or {}
            details.append(
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "prefilter_position": preselection.get("prefilter_position"),
                    "deep_queue_position": preselection.get("deep_queue_position"),
                    "prefilter_score": preselection.get("seed_score"),
                    "ranking_score": item.get("ranking_score"),
                    "final_rank": rank_by_code.get(str(item.get("code") or "")),
                    "qualified": bool(item.get("qualified")),
                    "terminal_status": item.get("terminal_status"),
                    "evidence_status": item.get("evidence_status"),
                }
            )
        return details

    qualified_detail_reference = qualified_details(by_budget[reference])
    qualified_detail_maximum = qualified_details(by_budget[maximum])
    position_ranges = (
        ("1-30", 1, 30),
        ("31-60", 31, 60),
        ("61-120", 61, 120),
        ("121-240", 121, 240),
    )
    qualified_position_distribution = []
    for label, start, end in position_ranges:
        if start > maximum:
            qualified_position_distribution.append(
                {"positions": label, "evaluated": False, "qualified_count": None}
            )
            continue
        qualified_position_distribution.append(
            {
                "positions": label,
                "evaluated": True,
                "qualified_count": sum(
                    start <= int(item["deep_queue_position"]) <= min(end, maximum)
                    for item in qualified_detail_maximum
                ),
            }
        )
    prefilter_count = int(
        (payload.get("screening_stats") or {}).get("prefilter_eligible_count") or maximum
    )
    if prefilter_count > maximum:
        qualified_position_distribution.append(
            {
                "positions": f"{maximum + 1}-{prefilter_count}",
                "evaluated": False,
                "qualified_count": None,
            }
        )
    diagnostic_operations = Counter(
        str(diagnostic.get("operation") or "unknown")
        for item in all_records
        for diagnostic in item.get("provider_diagnostics") or []
        if isinstance(diagnostic, dict)
    )
    return {
        "schema_version": 2,
        "audit_type": "real_deep_selection_sensitivity",
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "as_of": payload.get("as_of"),
        "source_dataset_versions": {
            key: payload.get(key)
            for key in (
                "schema_version",
                "model_version",
                "ruleset_version",
                "evaluator_version",
                "sector_model_version",
                "evidence_version",
                "price_model_version",
            )
        },
        "universe_count": (payload.get("screening_stats") or {}).get("universe_count"),
        "prefilter_count": (payload.get("screening_stats") or {}).get(
            "prefilter_eligible_count"
        ),
        "max_deep_evaluated": maximum,
        "reference_budget": reference,
        "queue_verification": {
            "positions_contiguous": True,
            "strictly_nested": strict_nested,
            "deterministic_order_contract": (
                "industry group size desc, industry text asc; "
                "within group seed_score desc then code asc"
            ),
        },
        "elapsed_seconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
        "provider_runtime_metrics": runtime_metrics or {},
        "price_history_logical_calls": maximum,
        "provider_diagnostic_observations": dict(sorted(diagnostic_operations.items())),
        "qualified_detail_reference": qualified_detail_reference,
        "qualified_detail_maximum": qualified_detail_maximum,
        "qualified_position_distribution": qualified_position_distribution,
        "prefilter_analysis": _prefilter_analysis(
            by_budget[reference], upper_position=reference
        ),
        "prefilter_analysis_maximum": _prefilter_analysis(
            by_budget[maximum], upper_position=maximum
        ),
        "budgets": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit steady-income deep selection without LLM calls"
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--budgets", default="30,60,120,240")
    parser.add_argument("--reference-budget", type=int, default=120)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "reports" / "steady_income_selection_audit.json",
    )
    parser.add_argument("--stock-index", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    budgets = tuple(int(value.strip()) for value in args.budgets.split(",") if value.strip())
    if not budgets or min(budgets) <= 0:
        parser.error("--budgets must contain positive integers")
    source = PublicMarketSource(stock_index_path=args.stock_index)
    started = time.perf_counter()
    payload = SteadyIncomeDatasetBuilder(
        market_source=source,
        max_deep_evaluations=max(budgets),
        max_workers=max(1, args.workers),
    ).build(as_of=args.as_of)
    elapsed = time.perf_counter() - started
    audit = summarize_selection_sensitivity(
        payload,
        budgets=budgets,
        reference_budget=args.reference_budget,
        elapsed_seconds=elapsed,
        runtime_metrics=source.runtime_metrics(),
    )
    write_json_atomic(args.output, audit)
    print(
        "steady-income sensitivity audit complete: "
        f"universe={audit['universe_count']} prefilter={audit['prefilter_count']} "
        f"deep={audit['max_deep_evaluated']} elapsed={audit['elapsed_seconds']}s "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

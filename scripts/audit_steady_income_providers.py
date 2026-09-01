#!/usr/bin/env python3
"""Compare real steady-income deep evaluation at fixed worker counts.

The command never calls an LLM. It freezes one normal-corporate seed batch,
then repeats only the deep provider/evaluator stage at each worker count.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.build_steady_income_report import (  # noqa: E402
    PublicMarketSource,
    SteadyIncomeDatasetBuilder,
    _prefilter_market,
)
from src.reports.contracts import write_json_atomic  # noqa: E402


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _run_batch(
    seeds: list[dict[str, Any]], *, source: PublicMarketSource, as_of: date, workers: int
) -> tuple[list[dict[str, Any]], float]:
    builder = SteadyIncomeDatasetBuilder(
        market_source=source,
        max_workers=workers,
        max_deep_evaluations=len(seeds),
    )
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    if workers == 1:
        for seed in seeds:
            results.append(builder._evaluate(dict(seed), as_of))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="steady-provider-audit") as pool:
            futures = {pool.submit(builder._evaluate, dict(seed), as_of): seed for seed in seeds}
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda item: str(item.get("code") or ""))
    return results, time.monotonic() - started


def _summarize(results: list[dict[str, Any]], elapsed: float, workers: int) -> dict[str, Any]:
    terminal = Counter(str(item.get("terminal_status") or "internal_error") for item in results)
    diagnostics = [
        diagnostic
        for item in results
        for diagnostic in item.get("provider_diagnostics") or []
        if isinstance(diagnostic, dict) and diagnostic.get("status_category") != "ok"
    ]
    completed = terminal["evaluated_qualified"] + terminal["evaluated_rejected"]
    evidence_funnel = {
        evidence_type: sum(
            1
            for item in results
            if (item.get("evidence_status") or {}).get(evidence_type) == "complete"
        )
        for evidence_type in ("price", "dividend", "financial", "sector", "history")
    }
    records = []
    for item in results:
        failed = [
            {
                key: value.get(key)
                for key in (
                    "provider",
                    "operation",
                    "status_category",
                    "exception_class",
                    "retry_count",
                    "schema_failure",
                    "network_failure",
                    "evidence_unavailable",
                )
            }
            for value in item.get("provider_diagnostics") or []
            if isinstance(value, dict) and value.get("status_category") != "ok"
        ]
        records.append(
            {
                "code": item.get("code"),
                "sector_model": item.get("sector_model"),
                "terminal_status": item.get("terminal_status"),
                "failure_code": item.get("failure_code"),
                "evidence_issues": item.get("evidence_issues") or [],
                "failed_provider_operations": failed,
            }
        )
    return {
        "workers": workers,
        "deep_requested": len(results),
        "completed_evaluations": completed,
        "qualified": terminal["evaluated_qualified"],
        "rejected": terminal["evaluated_rejected"],
        "insufficient_evidence": terminal["insufficient_evidence"],
        "unsupported_sector_model": terminal["unsupported_sector_model"],
        "provider_failure": terminal["provider_failure"],
        "internal_error": terminal["internal_error"],
        "terminal_status_distribution": dict(sorted(terminal.items())),
        "provider_failure_by_operation": dict(
            sorted(Counter(str(value.get("operation") or "unknown") for value in diagnostics).items())
        ),
        "provider_status_categories": dict(
            sorted(Counter(str(value.get("status_category") or "unknown") for value in diagnostics).items())
        ),
        "evidence_funnel": evidence_funnel,
        "records": records,
        "elapsed_seconds": round(elapsed, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare steady-income real providers at workers 1/2/4/8")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--deep", type=int, default=30)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "reports" / "steady_income_provider_concurrency_audit.json",
    )
    parser.add_argument("--stock-index", type=Path, default=None)
    args = parser.parse_args(argv)
    worker_counts = tuple(sorted({int(value.strip()) for value in args.workers.split(",") if value.strip()}))
    if not worker_counts or min(worker_counts) <= 0 or args.deep <= 0:
        parser.error("--workers and --deep must be positive")

    evaluation_date = args.as_of or datetime.now(SHANGHAI_TZ).date()
    source = PublicMarketSource(stock_index_path=args.stock_index)
    universe, universe_source = source.load_universe()
    plans, periods = source.load_dividend_plan_periods(evaluation_date)
    dividend_history = source.load_dividend_history()
    _initial, stats = _prefilter_market(
        universe,
        plans,
        dividend_history,
        as_of=evaluation_date,
        max_deep_evaluations=args.deep,
        include_eligible_queue=True,
    )
    eligible = stats.pop("_eligible_queue")
    selector = SteadyIncomeDatasetBuilder(
        market_source=source,
        max_workers=1,
        max_deep_evaluations=args.deep,
    )
    seeds, selection_stats = selector._select_supported_seeds(eligible)
    for position, seed in enumerate(seeds, start=1):
        seed["deep_queue_position"] = position
    if len(seeds) != args.deep:
        raise RuntimeError(f"could not freeze {args.deep} normal-corporate seeds; selected={len(seeds)}")

    rows: list[dict[str, Any]] = []
    for workers in worker_counts:
        results, elapsed = _run_batch(seeds, source=source, as_of=evaluation_date, workers=workers)
        row = _summarize(results, elapsed, workers)
        rows.append(row)
        print(
            f"workers={workers} completed={row['completed_evaluations']}/{row['deep_requested']} "
            f"provider_failure={row['provider_failure']} insufficient={row['insufficient_evidence']} "
            f"elapsed={row['elapsed_seconds']}s"
        )

    payload = {
        "schema_version": 1,
        "audit_type": "steady_income_real_provider_concurrency",
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "as_of": evaluation_date.isoformat(),
        "universe_source": universe_source,
        "dividend_plan_periods": periods,
        "universe_count": len(universe),
        "prefilter_count": stats.get("prefilter_eligible_count"),
        "selection": selection_stats,
        "seed_codes": [seed["code"] for seed in seeds],
        "runs": rows,
    }
    write_json_atomic(args.output, payload)
    print(f"audit written: {args.output}")
    print("LLM calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

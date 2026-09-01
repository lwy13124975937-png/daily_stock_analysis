"""Versioned contracts for the rule-based steady-income evaluator."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


STEADY_INCOME_SCHEMA_VERSION = 6
STEADY_INCOME_MODEL_VERSION = "steady-income-risk-v5"
STEADY_INCOME_RULESET_VERSION = "4.0.0"
STEADY_INCOME_EVALUATOR_VERSION = "5.0.0"
STEADY_INCOME_SECTOR_MODEL_VERSION = "1.0.0"
STEADY_INCOME_EVIDENCE_VERSION = "4.0.0"
STEADY_INCOME_PRICE_MODEL_VERSION = "2.0.0"


class SteadyIncomeDataError(RuntimeError):
    """Base class for trustworthy-data failures."""


class SteadyIncomeProviderUnavailable(SteadyIncomeDataError):
    """A required external data provider could not be used."""


class SteadyIncomeSchemaError(SteadyIncomeDataError):
    """A provider response no longer satisfies the required schema."""


class HistoricalEvidenceUnavailable(SteadyIncomeDataError):
    """Point-in-time evidence needed by historical mode is unavailable."""


class SectorModel(str, Enum):
    NORMAL_CORPORATE = "normal_corporate"
    BANK = "bank"
    INSURER = "insurer"
    BROKER = "broker"
    UNSUPPORTED_FINANCIAL = "unsupported_financial"
    UNKNOWN = "unknown"


class SteadyTerminalStatus(str, Enum):
    """Exactly one terminal outcome for each requested deep evaluation."""

    EVALUATED_QUALIFIED = "evaluated_qualified"
    EVALUATED_REJECTED = "evaluated_rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED_SECTOR_MODEL = "unsupported_sector_model"
    PROVIDER_FAILURE = "provider_failure"
    INTERNAL_ERROR = "internal_error"


class SteadyDataStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SOURCE_SCHEMA_CHANGED = "source_schema_changed"
    VALID_ZERO = "valid_zero"


def summarize_deep_evaluation_counts(
    *,
    prefilter_count: int,
    requested_count: int,
    terminal_distribution: Mapping[str, Any],
) -> dict[str, int]:
    # attempted = reached exactly one terminal status; completed = qualified/rejected only.
    prefilter = int(prefilter_count)
    requested = int(requested_count)
    if prefilter < 0 or requested < 0:
        raise ValueError("steady-income counts cannot be negative")
    if requested > prefilter:
        raise ValueError("deep_requested_count cannot exceed prefilter_count")

    terminal = {
        status.value: int(terminal_distribution.get(status.value) or 0)
        for status in SteadyTerminalStatus
    }
    if any(value < 0 for value in terminal.values()):
        raise ValueError("terminal status counts cannot be negative")

    attempted = sum(terminal.values())
    if attempted > requested:
        raise ValueError("deep_attempted_count cannot exceed deep_requested_count")
    if attempted > prefilter:
        raise ValueError("deep_attempted_count cannot exceed prefilter_count")

    qualified = terminal[SteadyTerminalStatus.EVALUATED_QUALIFIED.value]
    rejected = terminal[SteadyTerminalStatus.EVALUATED_REJECTED.value]
    completed = qualified + rejected
    return {
        "deep_requested_count": requested,
        "deep_attempted_count": attempted,
        "deep_completed_count": completed,
        # Compatibility alias. New consumers must use deep_completed_count.
        "deep_evaluated_count": completed,
        "qualified_count": qualified,
        "rejected_count": rejected,
        "insufficient_evidence_count": terminal[
            SteadyTerminalStatus.INSUFFICIENT_EVIDENCE.value
        ],
        "unsupported_sector_count": terminal[
            SteadyTerminalStatus.UNSUPPORTED_SECTOR_MODEL.value
        ],
        "provider_failure_count": terminal[
            SteadyTerminalStatus.PROVIDER_FAILURE.value
        ],
        "internal_error_count": terminal[SteadyTerminalStatus.INTERNAL_ERROR.value],
        "unevaluated_count": prefilter - attempted,
    }


def normalize_industry(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def resolve_sector_model(industry: Any) -> SectorModel:
    """Resolve only from a provider-supplied canonical industry field.

    Company names are intentionally ignored.  An absent industry is not
    treated as a normal company because doing so could apply an industrial
    cash-flow model to a bank, insurer, or broker.
    """

    text = normalize_industry(industry).lower()
    if not text:
        return SectorModel.UNKNOWN
    if any(token in text for token in ("银行", "bank")):
        return SectorModel.BANK
    if any(token in text for token in ("保险", "insurance")):
        return SectorModel.INSURER
    if any(token in text for token in ("证券", "券商", "broker", "capital markets")):
        return SectorModel.BROKER
    if any(token in text for token in ("多元金融", "金融服务", "financial services", "金融控股")):
        return SectorModel.UNSUPPORTED_FINANCIAL
    return SectorModel.NORMAL_CORPORATE


def public_risk_label(value: Any) -> str:
    labels = {
        "稳健": "规则低风险 A",
        "较稳健": "规则低风险 B",
        "观察": "规则观察",
        "不纳入": "规则排除",
        "数据不足": "数据不足",
    }
    return labels.get(str(value or ""), "数据不足")


VERSION_FINGERPRINT = ":".join(
    (
        str(STEADY_INCOME_SCHEMA_VERSION),
        STEADY_INCOME_MODEL_VERSION,
        STEADY_INCOME_RULESET_VERSION,
        STEADY_INCOME_EVALUATOR_VERSION,
        STEADY_INCOME_SECTOR_MODEL_VERSION,
        STEADY_INCOME_EVIDENCE_VERSION,
        STEADY_INCOME_PRICE_MODEL_VERSION,
    )
)

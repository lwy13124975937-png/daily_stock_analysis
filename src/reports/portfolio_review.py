"""Canonical account-level fund portfolio review result and rule fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.reports.contracts import (
    PORTFOLIO_REVIEW_SCHEMA_VERSION,
    FailureCode,
    failure_code_from_exception,
)


ASSET_TITLES = {"lof": "LOF/ETF 组合复盘", "otc": "场外基金组合复盘"}
HOLDING_TITLES = {"lof": "持有标的", "otc": "持有基金"}
REQUIRED_SECTIONS = {
    "lof": ("组合观察", "配置节奏", "后续观察"),
    "otc": ("组合观察", "风格暴露", "配置节奏", "后续观察"),
}
BANNED_REVIEW_TERMS = ("买入", "卖出", "观望", "评分", "评级", "交易建议")
THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("科技成长", ("科技", "ai", "人工智能", "半导体", "芯片", "算力", "互联网", "软件", "硬科技")),
    ("基建链", ("电网", "基建", "工程", "建筑", "电力设备")),
    ("资源品", ("有色", "黄金", "白银", "稀土", "资源", "矿业", "铜", "铝", "煤炭", "能源")),
    ("红利价值", ("红利", "股息", "价值")),
    ("海外资产", ("纳斯达克", "标普", "美国", "全球", "海外", "qdii", "港股", "港美")),
    ("医药", ("医药", "生物", "创新药")),
    ("新能源", ("新能源", "光伏", "电池")),
    ("宽基指数", ("沪深300", "中证500", "a500", "宽基", "创业板", "上证50", "指数")),
    ("固收债券", ("债", "纯债", "固收")),
)


@dataclass(frozen=True)
class PortfolioReviewResult:
    account: str
    asset_type: str
    status: str
    holdings: tuple[dict[str, str], ...]
    themes: tuple[str, ...]
    sections: Mapping[str, tuple[str, ...]]
    failure_code: str = FailureCode.NONE.value
    generated_by: str = "ai"
    schema_version: int = PORTFOLIO_REVIEW_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account": self.account,
            "asset_type": self.asset_type,
            "status": self.status,
            "holdings": [dict(item) for item in self.holdings],
            "themes": list(self.themes),
            "sections": {key: list(value) for key, value in self.sections.items()},
            "failure_code": self.failure_code,
            "generated_by": self.generated_by,
        }


def infer_fund_themes(name: str, code: str = "") -> list[str]:
    text = f"{name} {code}".lower()
    return [theme for theme, keywords in THEME_RULES if any(keyword in text for keyword in keywords)]


def summarize_themes(holdings: Iterable[Mapping[str, str]]) -> tuple[list[str], str]:
    counts: dict[str, int] = {}
    for item in holdings:
        for theme in infer_fund_themes(str(item.get("name") or ""), str(item.get("code") or "")):
            counts[theme] = counts.get(theme, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    themes = [theme for theme, _ in ordered[:4]]
    concentrated = [theme for theme, count in ordered if count > 1]
    exposure = (
        f"名称规则提示可能集中在{'、'.join(concentrated[:3])}。"
        if concentrated
        else "名称规则未发现重复主题，仍需结合基金实际投向核验。"
    )
    return themes, exposure


def review_looks_truncated(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return True
    suspicious_suffixes = (
        "组合在",
        "基于当前持仓清单做",
        "基于当前持仓清单",
        "呈现出明显的",
        "该组合呈现",
        "当前组合在",
        "当前处于典型的",
        "典型的",
    )
    incomplete_tail_tokens = ("在", "的", "和", "与", "及", "但", "因此", "同时", "主要", "整体", "风格暴露")
    natural_endings = tuple("。；;：:、，,）)】》”’！？?!…")
    for line in [line.strip() for line in clean.splitlines() if line.strip()]:
        current = re.sub(r"^[-*+]\s+", "", line).strip()
        current = re.sub(r"^\d+[.)、]\s+", "", current).strip()
        current = re.sub(r"^#{1,6}\s+", "", current).strip()
        if not current:
            continue
        if any(current.endswith(value) for value in suspicious_suffixes + incomplete_tail_tokens):
            return True
        if current.count("“") > current.count("”"):
            return True
        if len(current) > 40 and not current.endswith(natural_endings):
            return True
    return False


def _clean_line(value: Any) -> str:
    line = re.sub(r"^[-*+]\s+", "", str(value or "").strip())
    return re.sub(r"\s+", " ", line).strip()


def parse_ai_sections(text: str, asset_type: str) -> dict[str, tuple[str, ...]] | None:
    required = REQUIRED_SECTIONS.get(asset_type)
    if not required or review_looks_truncated(text):
        return None
    sections: dict[str, list[str]] = {name: [] for name in required}
    current: str | None = None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip()
            current = title if title in sections else None
            continue
        if current and line:
            cleaned = _clean_line(line)
            if cleaned:
                sections[current].append(cleaned)
    if any(not sections[name] for name in required):
        return None
    flattened = " ".join(item for values in sections.values() for item in values)
    if any(term in flattened for term in BANNED_REVIEW_TERMS):
        return None
    return {name: tuple(sections[name][:2]) for name in required}


def rule_fallback_portfolio_review(
    account: str,
    asset_type: str,
    holdings: Iterable[Mapping[str, str]],
    *,
    failure_code: FailureCode = FailureCode.LLM_FAILED,
) -> PortfolioReviewResult:
    public_holdings = tuple(
        {"name": str(item.get("name") or item.get("code") or "").strip(), "code": str(item.get("code") or "").strip()}
        for item in holdings
        if str(item.get("code") or "").strip()
    )
    themes, exposure = summarize_themes(public_holdings)
    theme_text = "、".join(themes) if themes else "暂无法仅从名称准确归类"
    count = len(public_holdings)
    if asset_type == "lof":
        sections = {
            "组合观察": (
                "AI 组合复盘未完成，以下为规则版组合兜底复盘。",
                f"该账户共有 {count} 只 LOF/ETF；名称规则识别为{theme_text}。",
            ),
            "配置节奏": ("当前仅做组合层面风险观察，不对单只基金作短线判断。",),
            "后续观察": (exposure, "继续核验重复主题和单一方向暴露。"),
        }
    else:
        sections = {
            "组合观察": (
                "AI 组合复盘未完成，以下为规则版组合兜底复盘。",
                f"该账户共有 {count} 只场外基金；名称规则识别为{theme_text}。",
            ),
            "风格暴露": (exposure,),
            "配置节奏": ("当前仅做组合层面风险观察，不对单只基金作短线判断。",),
            "后续观察": ("继续核验单一主题集中度和不同基金实际持仓重叠。",),
        }
    return PortfolioReviewResult(
        account=account,
        asset_type=asset_type,
        status="rule_fallback",
        holdings=public_holdings,
        themes=tuple(themes),
        sections={key: tuple(value) for key, value in sections.items()},
        failure_code=failure_code.value,
        generated_by="rule",
    )


def portfolio_review_from_ai(
    account: str,
    asset_type: str,
    holdings: Iterable[Mapping[str, str]],
    text: str,
) -> PortfolioReviewResult | None:
    sections = parse_ai_sections(text, asset_type)
    if sections is None:
        return None
    public_holdings = tuple(
        {"name": str(item.get("name") or item.get("code") or "").strip(), "code": str(item.get("code") or "").strip()}
        for item in holdings
        if str(item.get("code") or "").strip()
    )
    themes, _ = summarize_themes(public_holdings)
    return PortfolioReviewResult(
        account=account,
        asset_type=asset_type,
        status="ai",
        holdings=public_holdings,
        themes=tuple(themes),
        sections=sections,
        generated_by="ai",
    )


def fallback_for_exception(
    account: str,
    asset_type: str,
    holdings: Iterable[Mapping[str, str]],
    exc: Exception,
) -> PortfolioReviewResult:
    return rule_fallback_portfolio_review(
        account,
        asset_type,
        holdings,
        failure_code=failure_code_from_exception(exc),
    )


def render_portfolio_review_markdown(result: PortfolioReviewResult) -> str:
    lines = [f"### {result.account}", "", f"#### {HOLDING_TITLES[result.asset_type]}", ""]
    lines.extend(f"- {item['name']}（{item['code']}）" for item in result.holdings)
    for title in REQUIRED_SECTIONS[result.asset_type]:
        lines.extend(("", f"#### {title}", ""))
        lines.extend(f"- {line}" for line in result.sections.get(title, ()))
    return "\n".join(lines)

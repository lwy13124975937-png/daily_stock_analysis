from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "00-daily-analysis.yml"


def _workflow() -> dict[str, Any]:
    payload = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def _steps() -> dict[str, dict[str, Any]]:
    jobs = _workflow()["jobs"]
    analyze = jobs["analyze"]
    return {str(step["name"]): step for step in analyze["steps"] if "name" in step}


def _runs_in_mode(step: dict[str, Any], mode: str) -> bool:
    condition = str(step.get("if") or "").strip()
    if not condition:
        return True
    if condition == "always()":
        return True
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("github.event_name", repr("workflow_dispatch"))
    expression = expression.replace("github.event.inputs.mode", repr(mode))
    expression = expression.replace("||", " or ").replace("&&", " and ")
    expression = re.sub(r"(?<![=!])!(?!=)", " not ", expression)
    assert re.fullmatch(r"[\s\w'!=().-]+", expression), expression
    return bool(eval(expression, {"__builtins__": {}}, {}))


def test_workflow_mode_contract() -> None:
    steps = _steps()
    names = {
        "holdings": "生成持仓自选股列表",
        "analysis": "执行股票分析",
        "restore_lookup": "查找上一版已验证构建输入",
        "restore": "恢复上一版已验证构建输入",
        "valid": "检查有效股票日报",
        "coverage": "检查日报 code 覆盖",
        "advice": "更新 AI 建议准确性回测",
        "steady": "生成稳健收益规则评估",
        "pages": "生成静态报告网页",
        "html": "检查静态报告网页",
        "deploy": "Deploy to GitHub Pages",
    }
    expected = {
        "full": {
            "holdings", "analysis", "valid", "coverage", "advice", "steady", "pages", "html", "deploy"
        },
        "pages-only": {"restore_lookup", "restore", "valid", "coverage", "pages", "html", "deploy"},
        "stocks-only": {"holdings", "analysis", "valid", "coverage"},
        "market-only": {"analysis"},
    }
    for mode, expected_keys in expected.items():
        actual = {key for key, step_name in names.items() if _runs_in_mode(steps[step_name], mode)}
        assert actual == expected_keys, (mode, actual)


def test_pages_only_is_validated_artifact_only_and_zero_llm() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = _steps()
    assert not _runs_in_mode(steps["执行股票分析"], "pages-only")
    assert not _runs_in_mode(steps["生成持仓自选股列表"], "pages-only")
    assert not _runs_in_mode(steps["更新 AI 建议准确性回测"], "pages-only")
    assert not _runs_in_mode(steps["生成稳健收益规则评估"], "pages-only")
    assert _runs_in_mode(steps["恢复上一版已验证构建输入"], "pages-only")
    assert _runs_in_mode(steps["检查静态报告网页"], "pages-only")
    assert "core.setFailed('No previous successful validated-build-inputs artifact exists.')" in workflow_text
    assert "name: validated-build-inputs" in workflow_text


def test_non_full_modes_cannot_publish_a_mixed_site() -> None:
    steps = _steps()
    for mode in ("stocks-only", "market-only"):
        assert not _runs_in_mode(steps["生成静态报告网页"], mode)
        assert not _runs_in_mode(steps["Upload Pages artifact"], mode)
        assert not _runs_in_mode(steps["Deploy to GitHub Pages"], mode)
    assert not _runs_in_mode(steps["更新 AI 建议准确性回测"], "market-only")


def test_workflow_shell_does_not_print_holdings_or_credentials() -> None:
    run_blocks = "\n".join(
        str(step.get("run") or "") for step in _steps().values()
    )
    forbidden = (
        r"set\s+-x",
        r"\bprintenv\b",
        r"\bcat\s+[^\n]*(?:holdings|current_stock_list)",
        r"echo\s+[\"']?\$(?:STOCK_LIST|HOLDINGS_DATA_JSON|HOLDINGS_DATA_JSON_B64)",
        r"echo\s+[\"']?\$[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY)",
        r"https?://[^\s]+[?&](?:token|access_token|api_key)=",
    )
    for pattern in forbidden:
        assert re.search(pattern, run_blocks, flags=re.IGNORECASE) is None, pattern

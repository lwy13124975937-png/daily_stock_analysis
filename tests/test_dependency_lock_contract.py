from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirements(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-") or raw_line[:1].isspace():
            continue
        if " @ git+" in line:
            name = line.split(" @ ", 1)[0].lower()
            packages[name] = line
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;.*)?", line)
        assert match is not None, f"unlocked dependency in {path}: {line}"
        packages[match.group(1).lower().replace("_", "-")] = match.group(2)
    return packages


def test_runtime_and_ci_locks_pin_critical_transitive_graph() -> None:
    runtime = _requirements(ROOT / "requirements.lock")
    ci = _requirements(ROOT / ".github" / "requirements-ci.lock")
    docker = _requirements(ROOT / "docker" / "requirements.lock")
    for name in (
        "nh3",
        "pandas",
        "numpy",
        "akshare",
        "requests",
        "httpx",
        "markdown2",
        "exchange-calendars",
        "fastapi",
        "yfinance",
    ):
        assert name in runtime
        assert ci[name] == runtime[name]
        assert name in docker
    assert runtime["alphasift"].endswith("1a0ed8c99b3615c0cb1076e6029827ffc6de2344#egg=alphasift")
    assert "pytest" in ci and "flake8" in ci


def test_ci_and_container_install_from_locks() -> None:
    paths_and_tokens = {
        ".github/workflows/00-daily-analysis.yml": "pip install -r requirements.lock",
        ".github/workflows/ci.yml": "pip install -r .github/requirements-ci.lock",
        ".github/workflows/network-smoke.yml": "pip install -r .github/requirements-ci.lock",
        ".github/workflows/docker-publish.yml": "pip install -r .github/requirements-ci.lock",
        "docker/Dockerfile": "COPY requirements.txt docker/requirements.lock ./",
    }
    for relative, token in paths_and_tokens.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert token in text
        assert "pip install --upgrade" not in text


def test_locked_linux_jobs_pin_their_abi_runner() -> None:
    for relative in (
        ".github/workflows/00-daily-analysis.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/network-smoke.yml",
        ".github/workflows/docker-publish.yml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "ubuntu-latest" not in text
        assert "ubuntu-24.04" in text
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ubuntu:24.04" in dockerfile
    assert "docker/requirements.lock" in dockerfile

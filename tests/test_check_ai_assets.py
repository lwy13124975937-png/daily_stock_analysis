from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_ai_assets


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _configure_paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(check_ai_assets, "ROOT", root)
    monkeypatch.setattr(check_ai_assets, "AGENTS", root / "AGENTS.md")
    monkeypatch.setattr(check_ai_assets, "CLAUDE", root / "CLAUDE.md")


def test_windows_symlink_placeholder_is_accepted_when_index_mode_is_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("AGENTS.md", encoding="utf-8")
    blob = _git(tmp_path, "hash-object", "-w", "CLAUDE.md")
    _git(tmp_path, "update-index", "--add", "--cacheinfo", f"120000,{blob},CLAUDE.md")
    _configure_paths(monkeypatch, tmp_path)

    check_ai_assets.ensure_symlink()


def test_plain_tracked_pointer_file_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("AGENTS.md", encoding="utf-8")
    _git(tmp_path, "add", "AGENTS.md", "CLAUDE.md")
    _configure_paths(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        check_ai_assets.ensure_symlink()

from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from obsidian_vault_guard import (  # noqa: E402
    require_canonical_obsidian_vault,
    require_path_within_vault,
)


def make_vault(path: Path) -> Path:
    (path / ".obsidian").mkdir(parents=True)
    (path / ".obsidian" / "core-plugins.json").write_text(
        '{"sync": true}\n', encoding="utf-8"
    )
    (path / "00 - Home").mkdir()
    (path / "99 - System").mkdir()
    return path


def test_accepts_intact_canonical_vault(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "canonical")

    assert require_canonical_obsidian_vault(vault, canonical=vault) == vault


def test_rejects_existing_alternate_vault(tmp_path: Path) -> None:
    canonical = make_vault(tmp_path / "canonical")
    alternate = make_vault(tmp_path / "OneDrive" / "AI Workspace")

    with pytest.raises(RuntimeError, match="not canonical"):
        require_canonical_obsidian_vault(alternate, canonical=canonical)


def test_rejects_missing_canonical_without_creating_it(tmp_path: Path) -> None:
    canonical = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="does not exist"):
        require_canonical_obsidian_vault(canonical, canonical=canonical)

    assert not canonical.exists()


def test_rejects_canonical_vault_missing_required_anchors(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()

    with pytest.raises(RuntimeError, match="missing required anchors"):
        require_canonical_obsidian_vault(canonical, canonical=canonical)


def test_rejects_path_outside_validated_vault(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "canonical")

    assert require_path_within_vault(vault / "99 - System" / "Inbox", vault) == (
        vault / "99 - System" / "Inbox"
    )
    with pytest.raises(RuntimeError, match="outside the canonical"):
        require_path_within_vault(tmp_path / "outside", vault)

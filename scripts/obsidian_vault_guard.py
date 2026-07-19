#!/usr/bin/env python3
"""Fail-closed validation for Ryan's canonical Obsidian vault."""
from __future__ import annotations

import os
from pathlib import Path


CANONICAL_OBSIDIAN_VAULT = Path(r"C:\Users\ryanr\Obsidian\AI Workspace")
REQUIRED_VAULT_FILES = (Path(".obsidian") / "core-plugins.json",)
REQUIRED_VAULT_DIRS = (Path("00 - Home"), Path("99 - System"))


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise RuntimeError(f"Obsidian vault path must be absolute: {expanded}")
    return Path(os.path.abspath(expanded))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def require_canonical_obsidian_vault(
    configured: str | os.PathLike[str] | None = None,
    *,
    canonical: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the configured vault only when it is the intact canonical vault.

    Path identity is deliberately lexical after absolute normalization. A
    OneDrive alias or junction is not accepted even if it currently targets
    the canonical directory.
    """
    expected = _absolute_path(canonical or CANONICAL_OBSIDIAN_VAULT)
    candidate = _absolute_path(configured or expected)
    if _path_key(candidate) != _path_key(expected):
        raise RuntimeError(
            f"Configured Obsidian vault is not canonical: {candidate}; expected {expected}"
        )
    if not candidate.is_dir():
        raise RuntimeError(f"Canonical Obsidian vault does not exist: {candidate}")

    missing = [
        str(relative)
        for relative in REQUIRED_VAULT_FILES
        if not (candidate / relative).is_file()
    ]
    missing.extend(
        str(relative)
        for relative in REQUIRED_VAULT_DIRS
        if not (candidate / relative).is_dir()
    )
    if missing:
        raise RuntimeError(
            "Canonical Obsidian vault is missing required anchors: " + ", ".join(missing)
        )
    return candidate


def require_path_within_vault(
    path: str | os.PathLike[str], vault: str | os.PathLike[str]
) -> Path:
    """Return an absolute path only when it is inside the validated vault."""
    candidate = _absolute_path(path)
    root = _absolute_path(vault)
    try:
        common = os.path.commonpath((_path_key(candidate), _path_key(root)))
    except ValueError as exc:
        raise RuntimeError(f"Path is outside the canonical Obsidian vault: {candidate}") from exc
    if common != _path_key(root):
        raise RuntimeError(f"Path is outside the canonical Obsidian vault: {candidate}")
    return candidate

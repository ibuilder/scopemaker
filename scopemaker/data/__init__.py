"""Static domain data: MasterFormat divisions and the shipped seed library."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

SEED_DIR = Path(__file__).parent / "seed"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@functools.lru_cache(maxsize=1)
def load_boilerplate() -> dict[str, str]:
    """Prose templates for the non-list sections of an exhibit."""
    data = load_yaml(SEED_DIR / "boilerplate.yaml")
    return {k: v.strip() for k, v in (data.get("boilerplate") or {}).items()}


@functools.lru_cache(maxsize=1)
def load_seed_clauses() -> list[dict[str, Any]]:
    """Every shipped clause, from all ``clauses_*.yaml`` files."""
    clauses: list[dict[str, Any]] = []
    for path in sorted(SEED_DIR.glob("clauses_*.yaml")):
        data = load_yaml(path)
        for entry in data.get("clauses") or []:
            entry = dict(entry)
            entry["_source_file"] = path.name
            clauses.append(entry)
    return clauses


@functools.lru_cache(maxsize=1)
def load_seed_spec_sections() -> list[dict[str, Any]]:
    data = load_yaml(SEED_DIR / "spec_sections.yaml")
    return [dict(entry) for entry in (data.get("spec_sections") or [])]


def clear_caches() -> None:
    """Drop cached seed data (used by tests that rewrite the YAML)."""
    load_boilerplate.cache_clear()
    load_seed_clauses.cache_clear()
    load_seed_spec_sections.cache_clear()

"""Resolve and load the portfolio configuration.

Resolution order is file, then database overlay, then the shipped example.
Streamlit Cloud gives the app a read-only filesystem, so a deployment there
keeps its config as a row rather than a file; the rest of the codebase should
not have to know which of those it is looking at.

This module deliberately does not read the environment. `desk.settings` is the
single place permitted to do that, and CI asserts it — so an override path
arrives here as an argument from the caller, never as a hidden global.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from desk.config.schema import PortfolioConfig

DEFAULT_CONFIG_PATH = Path("config/portfolio.yaml")
EXAMPLE_CONFIG_PATH = Path("config/portfolio.example.yaml")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid.

    Carries the formatted pydantic report, because a config error a user cannot
    read is a config error they will work around by guessing.
    """


def project_root() -> Path:
    """The repo root: the directory holding pyproject.toml, walking up from here."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def resolve_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Find the config file, or None if there isn't one.

    An override comes in as `explicit`; the CLI and the app pass through
    `settings.config_path`. There is no environment lookup here by design.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidate = project_root() / DEFAULT_CONFIG_PATH
    return candidate if candidate.is_file() else None


def _format_errors(exc: ValidationError, source: str) -> str:
    lines = [f"{source} is not valid:"]
    for err in exc.errors():
        location = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  {location}: {err['msg']}")
    return "\n".join(lines)


def parse(raw: Mapping[str, Any], *, source: str = "configuration") -> PortfolioConfig:
    """Validate an already-parsed mapping into a PortfolioConfig."""
    try:
        return PortfolioConfig.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, source)) from exc


def load_file(path: str | os.PathLike[str]) -> PortfolioConfig:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"no configuration file at {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML:\n  {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{p} must contain a YAML mapping at the top level")
    return parse(raw, source=str(p))


def load_example() -> PortfolioConfig:
    """The shipped example. Validated in CI so it cannot rot into a file that
    documents a schema the code no longer accepts."""
    return load_file(project_root() / EXAMPLE_CONFIG_PATH)


def load(
    explicit: str | os.PathLike[str] | None = None,
    *,
    allow_example: bool = False,
) -> PortfolioConfig:
    """Load the active configuration.

    `allow_example` exists for demo mode and for tests. A real run without a
    config should fail loudly and say how to create one, not quietly adopt
    defaults that describe nobody's portfolio.
    """
    path = resolve_path(explicit)
    if path is not None:
        return load_file(path)
    if allow_example:
        return load_example()
    raise ConfigError(
        "no configuration found.\n"
        f"  Expected {DEFAULT_CONFIG_PATH} (override with $DESK_CONFIG_PATH).\n"
        "  Run `desk init` to create one, or `desk demo` to explore with synthetic data."
    )

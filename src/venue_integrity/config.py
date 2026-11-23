"""Configuration loading.

Configuration is plain YAML under ``config/``. Loading is explicit and typed so
that a missing key fails loudly instead of defaulting silently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_MODES = ("replay", "simulation")


def default_config_dir() -> Path:
    env = os.environ.get("VIA_CONFIG_DIR")
    if env:
        return Path(env)
    # repository layout: <repo>/src/venue_integrity/config.py -> <repo>/config
    return Path(__file__).resolve().parents[2] / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


class ModeError(RuntimeError):
    """Raised when the audit is asked to run in a mode it does not support."""


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    config_dir: Path

    @property
    def mode(self) -> str:
        return self.raw["mode"]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        if not isinstance(value, dict):
            raise KeyError(f"settings.yaml is missing section {name!r}")
        return value

    @property
    def ingestion(self) -> dict[str, Any]:
        return self.section("ingestion")

    @property
    def tca(self) -> dict[str, Any]:
        return self.section("tca")

    @property
    def graph(self) -> dict[str, Any]:
        return self.section("graph")

    @property
    def router(self) -> dict[str, Any]:
        return self.section("router")

    @property
    def governed_route_categories(self) -> list[dict[str, Any]]:
        return list(self.raw.get("governed_route_categories") or [])


@dataclass(frozen=True)
class ConfigBundle:
    settings: Settings
    fix_dictionary: dict[str, Any]
    venue_registry: dict[str, Any]
    model_weights: dict[str, Any]
    mic_reference_path: Path
    files: dict[str, Path] = field(default_factory=dict)


def load_config(config_dir: Path | None = None) -> ConfigBundle:
    config_dir = Path(config_dir) if config_dir else default_config_dir()
    files = {
        "settings": config_dir / "settings.yaml",
        "fix_dictionary": config_dir / "fix_dictionary.yaml",
        "venue_registry": config_dir / "venue_registry.yaml",
        "model_weights": config_dir / "model_weights.yaml",
        "mic_reference": config_dir / "mic_reference.csv",
    }
    settings = Settings(load_yaml(files["settings"]), config_dir)
    mode = os.environ.get("VIA_MODE", settings.mode)
    assert_supported_mode(mode)
    return ConfigBundle(
        settings=settings,
        fix_dictionary=load_yaml(files["fix_dictionary"]),
        venue_registry=load_yaml(files["venue_registry"]),
        model_weights=load_yaml(files["model_weights"]),
        mic_reference_path=files["mic_reference"],
        files=files,
    )


def assert_supported_mode(mode: str) -> None:
    if mode not in SUPPORTED_MODES:
        raise ModeError(
            f"mode {mode!r} is not supported. The Venue Integrity Audit runs only in "
            f"{SUPPORTED_MODES} and never connects to live order routing."
        )

"""Pluggable enrichment providers.

Every provider is an adapter with one method, ``find(stage, people)``, returning
``Finding`` records for the stages it supports (``linkedin_url``, ``email``, ``verify``).
The waterfall order lives in ``config/gtm.yaml`` under ``enrichment``; adding a provider
means writing one adapter, registering it with ``@register("<adapter name>")`` and adding
it to the config. Nothing else in the pipeline changes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..enrichment import Finding

STAGES = ("linkedin_url", "email", "verify")


class ProviderError(RuntimeError):
    """A provider call failed. ``retryable`` is False for failures a retry cannot fix
    (no credits, bad credentials, rejected input)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def record_failure(person: dict, provider: str, stage: str, exc: Exception) -> None:
    """How adapters report a per-person failure without stopping the batch."""
    retryable = getattr(exc, "retryable", True)
    person.setdefault("errors", []).append(f"{provider}.{stage}: {exc}")
    person.setdefault("failed", {})[(provider, stage)] = retryable


class Enricher(Protocol):
    name: str
    stages: frozenset[str]

    def find(self, stage: str, people: list[dict]) -> list[Finding]:
        """Return findings. A failure for one person is reported with
        ``record_failure`` and must not stop the others."""
        ...


_ADAPTERS: dict[str, Callable[..., Enricher]] = {}


def register(adapter: str):
    def wrap(cls):
        _ADAPTERS[adapter] = cls
        return cls

    return wrap


def build(name: str, settings: dict, data_dir: Path) -> Enricher:
    adapter = settings.get("adapter")
    if adapter not in _ADAPTERS:
        known = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"enrichment provider {name!r}: unknown adapter {adapter!r} ({known})")
    return _ADAPTERS[adapter](name=name, settings=settings, data_dir=data_dir)


def waterfall_config(rules: dict) -> tuple[dict[str, list[str]], dict[str, dict]]:
    config = rules.get("enrichment") or {}
    waterfall = config.get("waterfall") or {}
    providers = config.get("providers") or {}
    for stage, names in waterfall.items():
        if stage not in STAGES:
            raise ValueError(f"enrichment.waterfall: unknown stage {stage!r}")
        for name in names:
            if name not in providers:
                raise ValueError(f"enrichment.waterfall.{stage}: provider {name!r} not configured")
    return waterfall, providers


# Register built-in adapters.
from . import clay, deepline, manual  # noqa: E402,F401

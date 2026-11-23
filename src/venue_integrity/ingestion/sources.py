"""Capture sources for replay.

The prototype reads captures from JSON-lines files (one :class:`RawCapture` per
line). A streaming adapter (Kafka/Redpanda) is an integration point: it must
produce the same ``RawCapture`` envelopes, preserving the broker's bytes
exactly, and is intentionally not implemented here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from venue_integrity.schemas.raw_event import RawCapture


def write_captures(path: str | Path, captures: Iterable[RawCapture]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for capture in captures:
            handle.write(capture.model_dump_json() + "\n")
            count += 1
    return count


def read_captures(path: str | Path) -> Iterator[RawCapture]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield RawCapture.model_validate(json.loads(line))
            except Exception as exc:  # noqa: BLE001 - surface the line number
                raise ValueError(f"{path}:{line_no}: invalid capture: {exc}") from exc

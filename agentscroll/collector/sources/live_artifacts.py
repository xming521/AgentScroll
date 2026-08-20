"""Persist opt-in live-test stages without affecting normal collection runs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


_write_lock = threading.Lock()
_artifact_dir: Optional[Path] = None


def artifact_dir() -> Optional[Path]:
    return _artifact_dir


def set_artifact_dir(path: str | Path) -> Path:
    global _artifact_dir
    _artifact_dir = Path(path).expanduser().resolve()
    return _artifact_dir


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    with _write_lock:
        temporary.write_text(f"{serialized}\n", encoding="utf-8")
        temporary.replace(path)


def save_stage(
    source: str,
    stage: str,
    topic: str,
    items: Iterable[Dict[str, Any]],
) -> Optional[Path]:
    run_dir = artifact_dir()
    if run_dir is None:
        return None
    materialized = list(items)
    path = run_dir / stage / f"{source}.json"
    _write_json(path, {
        "source": source,
        "topic": topic,
        "stage": stage,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(materialized),
        "items": materialized,
    })
    return path


def save_test_result(
    source: str,
    suite: str,
    topic: str,
    *,
    status: str,
    items: Iterable[Dict[str, Any]],
    error: Optional[str] = None,
) -> Optional[Path]:
    run_dir = artifact_dir()
    if run_dir is None:
        return None
    materialized = list(items)
    path = run_dir / "04_test_result" / f"{suite}_{source}.json"
    _write_json(path, {
        "source": source,
        "suite": suite,
        "topic": topic,
        "status": status,
        "error": error,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(materialized),
        "items": materialized,
    })
    return path

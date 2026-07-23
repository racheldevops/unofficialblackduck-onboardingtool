from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def emit_event(event_type: str, **fields: Any) -> None:
    payload = {**fields, "record_type": event_type}
    print(
        json.dumps(payload, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


def output_directory(
    requested: Path | None,
    organization: str,
    dry_run: bool,
) -> Path:
    if dry_run:
        path = Path(
            tempfile.mkdtemp(
                prefix=f"github-inventory-{organization}-"
            )
        )
        emit_event(
            "dry_run_output",
            output_directory=str(path),
            note="Temporary output is retained for review.",
        )
        return path

    return requested or Path("output") / organization


def projection_accuracy(samples: int) -> str:
    if samples < 10:
        return "low"
    if samples < 50:
        return "medium"
    return "high"


def projected_seconds(
    elapsed_seconds: float,
    completed: int,
    projected_total: float,
) -> float | None:
    if completed <= 0:
        return None
    return elapsed_seconds * max(float(completed), projected_total) / completed

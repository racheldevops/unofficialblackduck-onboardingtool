from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_WORKSPACE = Path(".inventory")
DEFAULT_CONFIG = Path("config/onboarding.toml")


@dataclass(frozen=True)
class Workspace:
    root: Path
    config_path: Path
    inventory_directory: Path
    properties_directory: Path
    workflow_directory: Path
    rulesets_directory: Path

    @classmethod
    def from_root(
        cls,
        root: Path,
        config_path: Path | None = None,
    ) -> "Workspace":
        selected_root = root.expanduser()
        selected_config = (
            config_path.expanduser()
            if config_path is not None
            else DEFAULT_CONFIG
        )
        return cls(
            root=selected_root,
            config_path=selected_config,
            inventory_directory=selected_root / "inventory",
            properties_directory=selected_root / "properties",
            workflow_directory=selected_root / "workflow",
            rulesets_directory=selected_root / "rulesets",
        )

    def create_run_directory(self, stage: str) -> tuple[str, Path]:
        stage_directories = {
            "properties": self.properties_directory,
            "workflow": self.workflow_directory,
            "rulesets": self.rulesets_directory,
        }

        if stage not in stage_directories:
            raise ValueError(f"Unsupported workspace stage: {stage}")

        parent = stage_directories[stage]
        parent.mkdir(parents=True, exist_ok=True)

        for _attempt in range(100):
            run_id = new_run_id()
            run_directory = parent / run_id

            try:
                run_directory.mkdir()
            except FileExistsError:
                continue

            return run_id, run_directory

        raise RuntimeError("Unable to allocate a unique run directory.")


def utc_now_text() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_run_id() -> str:
    timestamp = (
        dt.datetime.now(dt.UTC)
        .strftime("%Y%m%dT%H%M%S%fZ")
    )
    return f"{timestamp}-{secrets.token_hex(3)}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    content = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_text(path, f"{content}\n")


def atomic_write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
) -> None:
    content = b"".join(
        canonical_json_bytes(record) + b"\n"
        for record in records
    )
    atomic_write_bytes(path, content)

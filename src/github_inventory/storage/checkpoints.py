from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..classification import isoformat_utc
from ..errors import InventoryError
from ..settings import (
    ACTIVITY_DAYS,
    CHECKPOINT_SCHEMA_VERSION,
    LANGUAGE_POLICY_VERSION,
    PILOT_SELECTION_METHOD,
)


def failure_record(
    repository: dict[str, Any] | None,
    operation: str,
    error: Exception,
) -> dict[str, Any]:
    repository_id = None
    name_with_owner = None

    if isinstance(repository, dict):
        if isinstance(repository.get("id"), str):
            repository_id = repository["id"]
        if isinstance(repository.get("nameWithOwner"), str):
            name_with_owner = repository["nameWithOwner"]

    return {
        "record_type": "failure",
        "repository_id": repository_id,
        "name_with_owner": name_with_owner,
        "operation": operation,
        "error_category": getattr(
            error,
            "category",
            type(error).__name__,
        ),
        "message": str(error),
        "attempts": int(getattr(error, "attempts", 1)),
    }


def checkpoint_configuration(
    organization: str,
    cutoff: dt.datetime,
    depth: str,
    pilot_limit: int | None = None,
) -> dict[str, Any]:
    return {
        "record_type": "meta",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "organization": organization,
        "activity_days": ACTIVITY_DAYS,
        "cutoff": isoformat_utc(cutoff),
        "language_policy": LANGUAGE_POLICY_VERSION,
        "inspection_depth": depth,
        "pilot_limit": pilot_limit,
        "pilot_selection_method": PILOT_SELECTION_METHOD,
    }


def initialize_checkpoint(
    path: Path,
    configuration: dict[str, Any],
) -> None:
    atomic_write_jsonl(path, [configuration])


def load_checkpoint(
    path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not path.exists():
        raise InventoryError(f"Checkpoint does not exist: {path}")

    metadata: dict[str, Any] | None = None
    records: dict[str, dict[str, Any]] = {}
    repository_ids: set[str] = set()

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise InventoryError(
                    f"Invalid checkpoint JSON on line {line_number}."
                ) from error

            if not isinstance(item, dict):
                raise InventoryError(
                    f"Invalid checkpoint value on line {line_number}."
                )

            if item.get("record_type") == "meta":
                if metadata is not None:
                    raise InventoryError(
                        "Checkpoint contains multiple metadata records."
                    )
                metadata = item
                continue

            if item.get("record_type") != "repository":
                raise InventoryError(
                    f"Invalid checkpoint record on line {line_number}."
                )

            record = item.get("repository")
            if not isinstance(record, dict):
                raise InventoryError(
                    f"Invalid repository checkpoint on line {line_number}."
                )

            name = record.get("name_with_owner")
            if not isinstance(name, str) or not name:
                raise InventoryError(
                    f"Checkpoint repository has no name on line "
                    f"{line_number}."
                )

            if name in records:
                raise InventoryError(
                    f"Checkpoint contains duplicate repository '{name}'."
                )

            repository_id = record.get("repository_id")
            if isinstance(repository_id, str) and repository_id:
                if repository_id in repository_ids:
                    raise InventoryError(
                        "Checkpoint contains a duplicate repository ID."
                    )
                repository_ids.add(repository_id)

            checkpoint_status = record.get("checkpoint_status")
            if checkpoint_status is not None and checkpoint_status not in {
                "successful",
                "excluded",
                "failed",
            }:
                raise InventoryError(
                    f"Checkpoint repository has an invalid status on line "
                    f"{line_number}."
                )

            records[name] = record

    if metadata is None:
        raise InventoryError("Checkpoint metadata is missing.")

    return metadata, records


def validate_checkpoint(
    metadata: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise InventoryError(
                "Checkpoint configuration mismatch for "
                f"'{key}': expected {value!r}, found "
                f"{metadata.get(key)!r}."
            )


class CheckpointWriter:

    def __init__(self, path: Path, flush_every: int = 25) -> None:
        self._handle = path.open("a", encoding="utf-8")
        self._flush_every = max(1, flush_every)
        self._pending = 0

    def append(self, repository: dict[str, Any]) -> None:
        item = {
            "record_type": "repository",
            "repository": repository,
        }
        self._handle.write(
            json.dumps(
                item,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        self._handle.write("\n")
        self._pending += 1

        if self._pending >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._pending = 0

    def close(self) -> None:
        if not self._handle.closed:
            self.flush()
            self._handle.close()

    def __enter__(self) -> "CheckpointWriter":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def atomic_write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        record,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                handle.write("\n")

            handle.flush()
            os.fsync(handle.fileno())

        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_publish_jsonl(
    artifacts: Mapping[
        Path,
        Sequence[dict[str, Any]],
    ],
) -> None:
    if not artifacts:
        return

    parents = {
        path.parent.resolve()
        for path in artifacts
    }
    if len(parents) != 1:
        raise InventoryError(
            "Published artifacts must share one output directory."
        )

    output_directory = next(iter(parents))
    output_directory.mkdir(parents=True, exist_ok=True)
    transaction = Path(
        tempfile.mkdtemp(
            prefix=".publish-",
            dir=output_directory,
        )
    )
    prepared = transaction / "prepared"
    previous = transaction / "previous"
    prepared.mkdir()
    previous.mkdir()
    preserved: list[Path] = []
    installed: list[Path] = []

    try:
        for path, records in artifacts.items():
            atomic_write_jsonl(
                prepared / path.name,
                records,
            )

        try:
            for path in artifacts:
                if path.exists():
                    path.replace(previous / path.name)
                    preserved.append(path)

            for path in artifacts:
                (prepared / path.name).replace(path)
                installed.append(path)
        except Exception:
            for path in installed:
                path.unlink(missing_ok=True)

            for path in preserved:
                saved = previous / path.name
                if saved.exists():
                    saved.replace(path)

            raise
    finally:
        shutil.rmtree(transaction, ignore_errors=True)


def reconcile(
    discovered_names: set[str],
    inventory_records: Sequence[dict[str, Any]],
    failures: Sequence[dict[str, Any]],
    exclusions: Sequence[dict[str, Any]] = (),
    *,
    discovered_count: int | None = None,
) -> bool:
    def record_names(
        records: Sequence[dict[str, Any]],
    ) -> list[str]:
        return [
            value
            for record in records
            if isinstance(
                value := record.get("name_with_owner"),
                str,
            )
            and value
        ]

    successful_list = record_names(inventory_records)
    failed_list = record_names(failures)
    excluded_list = record_names(exclusions)

    if len(successful_list) != len(set(successful_list)):
        return False
    if len(failed_list) != len(set(failed_list)):
        return False
    if len(excluded_list) != len(set(excluded_list)):
        return False

    successful = set(successful_list)
    failed = set(failed_list)
    excluded = set(excluded_list)

    if successful.intersection(failed):
        return False
    if successful.intersection(excluded):
        return False
    if failed.intersection(excluded):
        return False
    if successful | failed | excluded != discovered_names:
        return False

    if discovered_count is not None:
        categorized_count = (
            len(inventory_records)
            + len(failures)
            + len(exclusions)
        )
        if categorized_count != discovered_count:
            return False

    return True

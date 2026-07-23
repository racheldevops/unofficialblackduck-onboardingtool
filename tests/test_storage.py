from __future__ import annotations

import datetime as dt
import json

import pytest

from github_inventory.errors import GitHubError, InventoryError
from github_inventory.storage.checkpoints import (
    CheckpointWriter,
    atomic_write_jsonl,
    checkpoint_configuration,
    failure_record,
    initialize_checkpoint,
    load_checkpoint,
    reconcile,
    validate_checkpoint,
)


def test_checkpoint_can_be_loaded_for_resume(tmp_path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    cutoff = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    configuration = checkpoint_configuration(
        "acme",
        cutoff,
        "root",
    )
    record = {
        "name_with_owner": "acme/repository",
        "repository_id": "R_repository",
    }

    initialize_checkpoint(path, configuration)

    with CheckpointWriter(path, flush_every=1) as writer:
        writer.append(record)

    metadata, records = load_checkpoint(path)

    assert metadata["organization"] == "acme"
    assert metadata["inspection_depth"] == "root"
    assert records == {"acme/repository": record}


def test_checkpoint_configuration_mismatch_is_rejected(tmp_path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    configuration = checkpoint_configuration(
        "acme",
        dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        "root",
    )

    initialize_checkpoint(path, configuration)
    metadata, _records = load_checkpoint(path)

    with pytest.raises(InventoryError, match="mismatch"):
        validate_checkpoint(
            metadata,
            {"inspection_depth": "one"},
        )


def test_invalid_checkpoint_json_is_rejected(tmp_path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(InventoryError, match="Invalid checkpoint JSON"):
        load_checkpoint(path)


def test_atomic_jsonl_write_replaces_existing_content(tmp_path) -> None:
    path = tmp_path / "inventory.jsonl"
    path.write_text("obsolete\n", encoding="utf-8")

    atomic_write_jsonl(
        path,
        [
            {"name": "alpha"},
            {"name": "beta"},
        ],
    )

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert records == [
        {"name": "alpha"},
        {"name": "beta"},
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_reconciliation_accepts_complete_disjoint_results() -> None:
    discovered = {"acme/alpha", "acme/beta"}
    inventory = [{"name_with_owner": "acme/alpha"}]
    failures = [{"name_with_owner": "acme/beta"}]

    assert reconcile(discovered, inventory, failures) is True


def test_reconciliation_rejects_record_and_failure_overlap() -> None:
    discovered = {"acme/alpha"}
    inventory = [{"name_with_owner": "acme/alpha"}]
    failures = [{"name_with_owner": "acme/alpha"}]

    assert reconcile(discovered, inventory, failures) is False


def test_reconciliation_rejects_missing_repository() -> None:
    discovered = {"acme/alpha", "acme/beta"}
    inventory = [{"name_with_owner": "acme/alpha"}]

    assert reconcile(discovered, inventory, []) is False


def test_api_failure_record_is_not_language_classification() -> None:
    repository = {"nameWithOwner": "acme/unavailable"}
    error = GitHubError(
        "repository_unavailable",
        "Repository disappeared.",
        attempts=3,
    )

    record = failure_record(
        repository,
        "manifest inspection",
        error,
    )

    assert record["name_with_owner"] == "acme/unavailable"
    assert record["error_category"] == "repository_unavailable"
    assert record["attempts"] == 3
    assert "detected_languages" not in record
    assert "activity_status" not in record

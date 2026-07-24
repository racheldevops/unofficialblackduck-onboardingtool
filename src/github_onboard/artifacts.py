from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from github_inventory.settings import (
    ACTIVITY_POLICY_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    LANGUAGE_POLICY_VERSION,
)

from .errors import ArtifactError
from .models import InventoryBundle


ARTIFACT_NAMES = (
    "checkpoint.jsonl",
    "inventory.jsonl",
    "failures.jsonl",
    "summary.jsonl",
)


def _read_jsonl(
    path: Path,
    *,
    allow_empty: bool,
) -> tuple[dict[str, Any], ...]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ArtifactError(
            f"Unable to read inventory artifact: {path}"
        ) from error

    records: list[dict[str, Any]] = []

    for line_number, line in enumerate(
        content.splitlines(),
        start=1,
    ):
        if not line.strip():
            continue

        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ArtifactError(
                f"Invalid JSON in {path} on line {line_number}."
            ) from error

        if not isinstance(value, dict):
            raise ArtifactError(
                f"JSON value in {path} on line {line_number} "
                "must be an object."
            )

        records.append(value)

    if not records and not allow_empty:
        raise ArtifactError(
            f"Inventory artifact contains no records: {path}"
        )

    return tuple(records)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(
            f"Inventory field '{field}' must be a nonempty string."
        )

    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ArtifactError(
            f"Inventory field '{field}' must be a nonnegative integer."
        )

    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ArtifactError(
            f"Inventory field '{field}' must be boolean."
        )

    return value


def _repository_name(value: Any, field: str) -> str:
    name = _nonempty_string(value, field)

    if (
        name.count("/") != 1
        or name.startswith("/")
        or name.endswith("/")
    ):
        raise ArtifactError(
            f"Inventory field '{field}' must use owner/name."
        )

    return name


def _validate_languages(
    value: Any,
    repository_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ArtifactError(
            f"Repository '{repository_name}' has no language values."
        )

    languages: list[str] = []

    for language in value:
        if (
            not isinstance(language, str)
            or not language
            or language != language.strip()
            or language != language.casefold()
            or any(character.isspace() for character in language)
        ):
            raise ArtifactError(
                f"Repository '{repository_name}' has an invalid "
                "normalized language."
            )

        languages.append(language)

    if len(languages) != len(set(languages)):
        raise ArtifactError(
            f"Repository '{repository_name}' has duplicate languages."
        )

    if languages != sorted(languages):
        raise ArtifactError(
            f"Repository '{repository_name}' languages are not sorted."
        )

    if "unknown" in languages and languages != ["unknown"]:
        raise ArtifactError(
            f"Repository '{repository_name}' mixes unknown with "
            "known languages."
        )

    return tuple(languages)


def _validate_inventory_records(
    records: tuple[dict[str, Any], ...],
    organization: str,
) -> tuple[set[str], set[str]]:
    repository_ids: set[str] = set()
    normalized_names: set[str] = set()

    for index, record in enumerate(records):
        prefix = f"inventory[{index}]"
        repository_id = _nonempty_string(
            record.get("repository_id"),
            f"{prefix}.repository_id",
        )
        name = _repository_name(
            record.get("name_with_owner"),
            f"{prefix}.name_with_owner",
        )
        owner, _repository = name.split("/", 1)

        if owner.casefold() != organization.casefold():
            raise ArtifactError(
                f"Repository '{name}' is outside organization "
                f"'{organization}'."
            )

        normalized_name = name.casefold()

        if repository_id in repository_ids:
            raise ArtifactError(
                f"Duplicate inventory repository ID: {repository_id}"
            )

        if normalized_name in normalized_names:
            raise ArtifactError(
                f"Duplicate inventory repository name: {name}"
            )

        repository_ids.add(repository_id)
        normalized_names.add(normalized_name)

        archived = _boolean(
            record.get("is_archived"),
            f"{prefix}.is_archived",
        )
        template = _boolean(
            record.get("is_template"),
            f"{prefix}.is_template",
        )
        _boolean(
            record.get("is_fork"),
            f"{prefix}.is_fork",
        )

        if archived or template:
            raise ArtifactError(
                f"Excluded repository '{name}' appears in inventory."
            )

        activity = record.get("activity_status")

        if activity not in {"active", "inactive"}:
            raise ArtifactError(
                f"Repository '{name}' has an invalid activity status."
            )

        _validate_languages(
            record.get("detected_languages"),
            name,
        )

    return repository_ids, normalized_names


def _validate_failure_records(
    records: tuple[dict[str, Any], ...],
    organization: str,
) -> set[str]:
    normalized_names: set[str] = set()

    for index, record in enumerate(records):
        prefix = f"failures[{index}]"
        name = _repository_name(
            record.get("name_with_owner"),
            f"{prefix}.name_with_owner",
        )
        owner, _repository = name.split("/", 1)

        if owner.casefold() != organization.casefold():
            raise ArtifactError(
                f"Failed repository '{name}' is outside organization "
                f"'{organization}'."
            )

        normalized_name = name.casefold()

        if normalized_name in normalized_names:
            raise ArtifactError(
                f"Duplicate failure repository name: {name}"
            )

        normalized_names.add(normalized_name)
        _nonempty_string(
            record.get("operation"),
            f"{prefix}.operation",
        )
        _nonempty_string(
            record.get("error_category"),
            f"{prefix}.error_category",
        )

    return normalized_names


def _load_checkpoint(
    records: tuple[dict[str, Any], ...],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    metadata: dict[str, Any] | None = None
    states: dict[str, dict[str, Any]] = {}
    normalized_names: set[str] = set()
    repository_ids: set[str] = set()

    for index, item in enumerate(records):
        if item.get("record_type") == "meta":
            if metadata is not None:
                raise ArtifactError(
                    "Checkpoint contains multiple metadata records."
                )

            metadata = item
            continue

        if item.get("record_type") != "repository":
            raise ArtifactError(
                f"Invalid checkpoint record at index {index}."
            )

        state = item.get("repository")

        if not isinstance(state, dict):
            raise ArtifactError(
                f"Invalid checkpoint repository at index {index}."
            )

        name = _repository_name(
            state.get("name_with_owner"),
            f"checkpoint[{index}].name_with_owner",
        )
        normalized_name = name.casefold()

        if normalized_name in normalized_names:
            raise ArtifactError(
                f"Duplicate checkpoint repository name: {name}"
            )

        normalized_names.add(normalized_name)

        repository_id = state.get("repository_id")

        if repository_id is not None:
            repository_id = _nonempty_string(
                repository_id,
                f"checkpoint[{index}].repository_id",
            )

            if repository_id in repository_ids:
                raise ArtifactError(
                    "Checkpoint contains duplicate repository IDs."
                )

            repository_ids.add(repository_id)

        status = state.get("checkpoint_status")

        if status not in {"successful", "excluded", "failed"}:
            raise ArtifactError(
                f"Checkpoint repository '{name}' has an invalid status."
            )

        states[name] = state

    if metadata is None:
        raise ArtifactError("Checkpoint metadata is missing.")

    return metadata, states


def _summary_record(
    records: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if len(records) != 1:
        raise ArtifactError(
            "Summary artifact must contain exactly one record."
        )

    summary = records[0]

    if summary.get("record_type") != "summary":
        raise ArtifactError(
            "Summary artifact has an invalid record type."
        )

    return summary


def _artifact_digest(
    directory: Path,
) -> str:
    digest = hashlib.sha256()

    for name in ARTIFACT_NAMES:
        path = directory / name

        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactError(
                f"Unable to digest inventory artifact: {path}"
            ) from error

        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    return digest.hexdigest()


def _reject_secrets(
    directory: Path,
    secrets: Iterable[str],
) -> None:
    selected = tuple(
        secret.encode("utf-8")
        for secret in secrets
        if secret
    )

    if not selected:
        return

    for name in ARTIFACT_NAMES:
        path = directory / name

        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactError(
                f"Unable to inspect inventory artifact: {path}"
            ) from error

        if any(secret in content for secret in selected):
            raise ArtifactError(
                f"Inventory artifact contains an authentication "
                f"secret: {path}"
            )


def load_inventory_bundle(
    directory: Path,
    organization: str,
    *,
    secrets: Iterable[str] = (),
) -> InventoryBundle:
    paths = {
        name: directory / name
        for name in ARTIFACT_NAMES
    }

    missing = [
        str(path)
        for path in paths.values()
        if not path.is_file()
    ]

    if missing:
        raise ArtifactError(
            "Missing inventory artifacts: "
            + ", ".join(sorted(missing))
        )

    _reject_secrets(directory, secrets)

    checkpoint_records = _read_jsonl(
        paths["checkpoint.jsonl"],
        allow_empty=False,
    )
    inventory_records = _read_jsonl(
        paths["inventory.jsonl"],
        allow_empty=True,
    )
    failure_records = _read_jsonl(
        paths["failures.jsonl"],
        allow_empty=True,
    )
    summary = _summary_record(
        _read_jsonl(
            paths["summary.jsonl"],
            allow_empty=False,
        )
    )
    metadata, states = _load_checkpoint(checkpoint_records)

    summary_organization = _nonempty_string(
        summary.get("organization"),
        "summary.organization",
    )

    if summary_organization.casefold() != organization.casefold():
        raise ArtifactError(
            "Inventory summary organization does not match "
            f"'{organization}'."
        )

    checkpoint_organization = _nonempty_string(
        metadata.get("organization"),
        "checkpoint.organization",
    )

    if checkpoint_organization.casefold() != organization.casefold():
        raise ArtifactError(
            "Checkpoint organization does not match "
            f"'{organization}'."
        )

    if summary.get("aborted") is not False:
        raise ArtifactError("Aborted inventory cannot be onboarded.")

    if summary.get("reconciliation_ok") is not True:
        raise ArtifactError(
            "Unreconciled inventory cannot be onboarded."
        )

    if metadata.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ArtifactError(
            "Unsupported checkpoint schema version."
        )

    if metadata.get("language_policy") != LANGUAGE_POLICY_VERSION:
        raise ArtifactError(
            "Unsupported checkpoint language policy."
        )

    if summary.get("activity_policy") != ACTIVITY_POLICY_VERSION:
        raise ArtifactError(
            "Unsupported inventory activity policy."
        )

    successful_count = _nonnegative_integer(
        summary.get("successful_repository_count"),
        "summary.successful_repository_count",
    )
    excluded_count = _nonnegative_integer(
        summary.get("excluded_repository_count"),
        "summary.excluded_repository_count",
    )
    failed_count = _nonnegative_integer(
        summary.get("failed_repository_count"),
        "summary.failed_repository_count",
    )
    selected_count = _nonnegative_integer(
        summary.get("selected_repository_count"),
        "summary.selected_repository_count",
    )

    if successful_count != len(inventory_records):
        raise ArtifactError(
            "Inventory count does not match the summary."
        )

    if failed_count != len(failure_records):
        raise ArtifactError(
            "Failure count does not match the summary."
        )

    if (
        successful_count + excluded_count + failed_count
        != selected_count
    ):
        raise ArtifactError(
            "Summary repository categories do not reconcile."
        )

    _repository_ids, inventory_names = (
        _validate_inventory_records(
            inventory_records,
            organization,
        )
    )
    failure_names = _validate_failure_records(
        failure_records,
        organization,
    )

    if inventory_names.intersection(failure_names):
        raise ArtifactError(
            "A repository appears in inventory and failures."
        )

    successful_states = {
        name.casefold()
        for name, state in states.items()
        if state.get("checkpoint_status") == "successful"
    }
    excluded_states = {
        name.casefold()
        for name, state in states.items()
        if state.get("checkpoint_status") == "excluded"
    }
    failed_states = {
        name.casefold()
        for name, state in states.items()
        if state.get("checkpoint_status") == "failed"
    }

    if successful_states != inventory_names:
        raise ArtifactError(
            "Successful checkpoint states do not match inventory."
        )

    if failed_states != failure_names:
        raise ArtifactError(
            "Failed checkpoint states do not match failures."
        )

    if len(excluded_states) != excluded_count:
        raise ArtifactError(
            "Excluded checkpoint count does not match the summary."
        )

    if len(states) != selected_count:
        raise ArtifactError(
            "Checkpoint state count does not match the summary."
        )

    return InventoryBundle(
        directory=directory,
        inventory=inventory_records,
        failures=failure_records,
        summary=summary,
        checkpoint_metadata=metadata,
        checkpoint_states=states,
        sha256=_artifact_digest(directory),
    )

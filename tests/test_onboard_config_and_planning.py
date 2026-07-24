from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from github_inventory.settings import (
    ACTIVITY_POLICY_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    LANGUAGE_POLICY_VERSION,
)
from github_onboard.artifacts import load_inventory_bundle
from github_onboard.config import (
    initialize_config,
    load_config,
)
from github_onboard.errors import (
    ArtifactError,
    ConfigurationError,
)
from github_onboard.models import (
    InventoryBundle,
    OnboardingConfig,
    RepositoryOverride,
)
from github_onboard.planning import (
    plan_property_definitions,
    plan_repository_assignments,
)
from github_onboard.policy import policy_result
from github_onboard.workspace import Workspace


def make_config(tmp_path: Path) -> OnboardingConfig:
    path = tmp_path / "config" / "onboarding.toml"
    initialize_config(path, "acme")
    return load_config(path)


def inventory_record(
    *,
    name: str = "acme/repository",
    repository_id: str = "R_repository",
    activity: str = "active",
    languages: list[str] | None = None,
    is_fork: bool = False,
) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "name_with_owner": name,
        "url": f"https://github.com/{name}",
        "visibility": "private",
        "default_branch": "main",
        "is_fork": is_fork,
        "is_archived": False,
        "is_template": False,
        "pushed_at": "2026-07-01T00:00:00Z",
        "activity_status": activity,
        "detected_languages": (
            ["python"]
            if languages is None
            else languages
        ),
    }


def inventory_bundle(
    tmp_path: Path,
    *records: dict[str, Any],
) -> InventoryBundle:
    selected = records or (inventory_record(),)

    return InventoryBundle(
        directory=tmp_path,
        inventory=tuple(selected),
        failures=(),
        summary={},
        checkpoint_metadata={},
        checkpoint_states={},
        sha256="inventory-digest",
    )


def write_jsonl(
    path: Path,
    records: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def write_inventory_artifacts(
    directory: Path,
    *,
    summary_note: str | None = None,
) -> None:
    record = inventory_record()
    metadata = {
        "record_type": "meta",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "organization": "acme",
        "language_policy": LANGUAGE_POLICY_VERSION,
    }
    checkpoint = {
        "record_type": "repository",
        "repository": {
            "checkpoint_status": "successful",
            "repository_id": "R_repository",
            "name_with_owner": "acme/repository",
            "inventory": record,
        },
    }
    summary: dict[str, Any] = {
        "record_type": "summary",
        "organization": "acme",
        "aborted": False,
        "reconciliation_ok": True,
        "activity_policy": ACTIVITY_POLICY_VERSION,
        "successful_repository_count": 1,
        "excluded_repository_count": 0,
        "failed_repository_count": 0,
        "selected_repository_count": 1,
    }

    if summary_note is not None:
        summary["note"] = summary_note

    write_jsonl(
        directory / "checkpoint.jsonl",
        [metadata, checkpoint],
    )
    write_jsonl(
        directory / "inventory.jsonl",
        [record],
    )
    write_jsonl(
        directory / "failures.jsonl",
        [],
    )
    write_jsonl(
        directory / "summary.jsonl",
        [summary],
    )


def test_workspace_uses_standard_paths(
    tmp_path: Path,
) -> None:
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )

    assert workspace.config_path == Path(
        "config/onboarding.toml"
    )
    assert workspace.inventory_directory == (
        tmp_path / ".inventory" / "inventory"
    )
    assert workspace.properties_directory == (
        tmp_path / ".inventory" / "properties"
    )


def test_workspace_override_changes_all_default_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom-workspace"
    workspace = Workspace.from_root(root)

    assert workspace.root == root
    assert workspace.config_path == Path(
        "config/onboarding.toml"
    )
    assert workspace.inventory_directory.parent == root
    assert workspace.properties_directory.parent == root
    assert workspace.workflow_directory.parent == root
    assert workspace.rulesets_directory.parent == root


def test_explicit_config_path_is_preserved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    config_path = tmp_path / "configuration" / "custom.toml"
    workspace = Workspace.from_root(
        root,
        config_path,
    )

    assert workspace.root == root
    assert workspace.config_path == config_path


def test_config_initialization_and_loading(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / ".inventory"
        / "config"
        / "onboarding.toml"
    )

    initialize_config(path, "acme")
    config = load_config(
        path,
        environment_organization="ACME",
    )

    assert config.schema_version == 1
    assert config.github.organization == "acme"
    assert config.github.rest_api_url == (
        "https://api.github.com"
    )
    assert config.github.graphql_url == (
        "https://api.github.com/graphql"
    )
    assert config.properties.managed_names == (
        "blackduck_activity",
        "blackduck_languages",
        "blackduck_sca_policy",
    )
    assert config.policy.active_known == "required"
    assert config.policy.inactive_known == "review"
    assert config.policy.unknown == "review"
    assert config.policy.fork == "review"


def test_config_refuses_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "onboarding.toml"
    initialize_config(path, "acme")

    with pytest.raises(
        ConfigurationError,
        match="already exists",
    ):
        initialize_config(path, "acme")


def test_config_rejects_environment_organization_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "onboarding.toml"
    initialize_config(path, "acme")

    with pytest.raises(
        ConfigurationError,
        match="does not match",
    ):
        load_config(
            path,
            environment_organization="other",
        )


def test_config_contains_no_token_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "onboarding.toml"
    initialize_config(path, "acme")
    content = path.read_text(encoding="utf-8").casefold()

    assert "github_token" not in content
    assert "authorization" not in content


def test_valid_inventory_artifacts_load_deterministically(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "inventory"
    write_inventory_artifacts(directory)

    first = load_inventory_bundle(directory, "acme")
    second = load_inventory_bundle(directory, "ACME")

    assert first.inventory == second.inventory
    assert first.sha256 == second.sha256
    assert len(first.inventory) == 1
    assert first.failures == ()


def test_inventory_artifact_containing_token_is_rejected(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "inventory"
    token = "test-secret-token"
    write_inventory_artifacts(
        directory,
        summary_note=token,
    )

    with pytest.raises(
        ArtifactError,
        match="authentication secret",
    ):
        load_inventory_bundle(
            directory,
            "acme",
            secrets=(token,),
        )


def test_missing_inventory_artifact_is_rejected(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "inventory"
    write_inventory_artifacts(directory)
    (directory / "summary.jsonl").unlink()

    with pytest.raises(
        ArtifactError,
        match="Missing inventory artifacts",
    ):
        load_inventory_bundle(directory, "acme")


def test_aborted_inventory_is_rejected(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "inventory"
    write_inventory_artifacts(directory)
    summary_path = directory / "summary.jsonl"
    summary = json.loads(
        summary_path.read_text(
            encoding="utf-8"
        ).strip()
    )
    summary["aborted"] = True
    write_jsonl(summary_path, [summary])

    with pytest.raises(
        ArtifactError,
        match="Aborted inventory",
    ):
        load_inventory_bundle(directory, "acme")


def test_policy_precedence_uses_override_first(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    override = RepositoryOverride(
        repository="acme/repository",
        result="excluded",
        reason="Scanned elsewhere.",
    )
    selected = dataclasses.replace(
        config,
        policy=dataclasses.replace(
            config.policy,
            repository_overrides=(override,),
        ),
    )

    result, reason = policy_result(
        inventory_record(
            is_fork=True,
            languages=["unknown"],
        ),
        selected,
    )

    assert result == "excluded"
    assert reason == "Scanned elsewhere."


@pytest.mark.parametrize(
    ("record", "expected", "reason"),
    [
        (
            inventory_record(is_fork=True),
            "review",
            "fork",
        ),
        (
            inventory_record(languages=["unknown"]),
            "review",
            "unknown_language",
        ),
        (
            inventory_record(activity="inactive"),
            "review",
            "inactive_known",
        ),
        (
            inventory_record(activity="active"),
            "required",
            "active_known",
        ),
    ],
)
def test_policy_mapping(
    tmp_path: Path,
    record: dict[str, Any],
    expected: str,
    reason: str,
) -> None:
    config = make_config(tmp_path)

    assert policy_result(record, config) == (
        expected,
        reason,
    )


def test_property_definition_plan_is_additive(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bundle = inventory_bundle(
        tmp_path,
        inventory_record(
            languages=["java", "python"],
        ),
    )
    existing = [
        {
            "property_name": "blackduck_activity",
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": "Existing activity",
            "allowed_values": [
                "active",
                "inactive",
                "legacy",
            ],
        },
        {
            "property_name": "blackduck_languages",
            "value_type": "multi_select",
            "required": False,
            "default_value": None,
            "description": "Existing languages",
            "allowed_values": ["python", "ruby"],
        },
        {
            "property_name": "blackduck_sca_policy",
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": "Existing policy",
            "allowed_values": [
                "excluded",
                "required",
                "review",
            ],
        },
    ]

    records = plan_property_definitions(
        bundle,
        config,
        existing,
    )
    by_name = {
        record["property_name"]: record
        for record in records
    }

    assert by_name["blackduck_activity"]["action"] == (
        "no_change"
    )
    assert by_name["blackduck_languages"]["action"] == (
        "add_allowed_values"
    )
    assert by_name["blackduck_languages"][
        "missing_allowed_values"
    ] == ["java"]
    assert by_name["blackduck_languages"][
        "desired_definition"
    ]["allowed_values"] == [
        "java",
        "python",
        "ruby",
    ]


def test_property_type_change_is_a_conflict(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bundle = inventory_bundle(tmp_path)
    existing = [
        {
            "property_name": "blackduck_activity",
            "value_type": "string",
            "allowed_values": [],
        }
    ]

    records = plan_property_definitions(
        bundle,
        config,
        existing,
    )
    activity = next(
        record
        for record in records
        if record["property_name"]
        == "blackduck_activity"
    )

    assert activity["action"] == "conflict"
    assert activity["reason"] == "value_type_mismatch"


def test_unrelated_property_does_not_block_initialization(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bundle = inventory_bundle(tmp_path)
    assignments = {
        "acme/repository": {
            "repository_full_name": "acme/repository",
            "properties": [
                {
                    "property_name": "unrelated_property",
                    "value": "manual",
                }
            ],
        }
    }

    records = plan_repository_assignments(
        bundle,
        config,
        assignments,
        refresh_all=False,
    )

    assert records[0]["action"] == "initialize"


def test_existing_managed_value_is_preserved_by_default(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bundle = inventory_bundle(tmp_path)
    assignments = {
        "acme/repository": {
            "properties": [
                {
                    "property_name": "blackduck_activity",
                    "value": "inactive",
                }
            ],
        }
    }

    records = plan_repository_assignments(
        bundle,
        config,
        assignments,
        refresh_all=False,
    )

    assert records[0]["action"] == "skipped_existing"


def test_refresh_all_updates_existing_managed_values(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bundle = inventory_bundle(tmp_path)
    assignments = {
        "acme/repository": {
            "properties": [
                {
                    "property_name": "blackduck_activity",
                    "value": "inactive",
                },
                {
                    "property_name": "blackduck_languages",
                    "value": ["ruby"],
                },
                {
                    "property_name": "blackduck_sca_policy",
                    "value": "review",
                },
            ],
        }
    }

    records = plan_repository_assignments(
        bundle,
        config,
        assignments,
        refresh_all=True,
    )

    assert records[0]["action"] == "update"
    assert records[0]["desired_values"] == {
        "blackduck_activity": "active",
        "blackduck_languages": ["python"],
        "blackduck_sca_policy": "required",
    }

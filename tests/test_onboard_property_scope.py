from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_onboard.cli import (
    build_parser,
    selected_property_repositories,
)
from github_onboard.config import (
    initialize_config,
    load_config,
)
from github_onboard.errors import OnboardError
from github_onboard.inventory import fresh_inventory_arguments
from github_onboard.models import (
    InventoryBundle,
    OnboardingConfig,
)
from github_onboard.properties import run_properties
from github_onboard.workspace import Workspace


def make_config(tmp_path: Path) -> OnboardingConfig:
    path = tmp_path / "config" / "onboarding.toml"
    initialize_config(path, "acme")
    return load_config(path)


def inventory_record(
    name: str,
    repository_id: str,
    *,
    activity: str = "active",
    languages: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "name_with_owner": name,
        "url": f"https://github.com/{name}",
        "visibility": "private",
        "default_branch": "main",
        "is_fork": False,
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
    records: tuple[dict[str, Any], ...],
    *,
    additional_states: dict[str, dict[str, Any]] | None = None,
) -> InventoryBundle:
    states = {
        record["name_with_owner"]: {
            "checkpoint_status": "successful",
            "repository_id": record["repository_id"],
            "name_with_owner": record["name_with_owner"],
            "inventory": record,
        }
        for record in records
    }
    states.update(additional_states or {})

    return InventoryBundle(
        directory=tmp_path / "inventory",
        inventory=records,
        failures=(),
        summary={},
        checkpoint_metadata={},
        checkpoint_states=states,
        sha256="inventory-sha256",
    )


def compatible_definitions() -> list[dict[str, Any]]:
    return [
        {
            "property_name": "blackduck_activity",
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": "Activity",
            "allowed_values": ["active", "inactive"],
        },
        {
            "property_name": "blackduck_languages",
            "value_type": "multi_select",
            "required": False,
            "default_value": None,
            "description": "Languages",
            "allowed_values": ["java", "python"],
        },
        {
            "property_name": "blackduck_sca_policy",
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": "Policy",
            "allowed_values": [
                "excluded",
                "required",
                "review",
            ],
        },
    ]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def read_only_handler(
    methods: list[str],
) -> httpx.MockTransport:
    definitions = compatible_definitions()

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        path = request.url.path

        if path == "/user":
            return httpx.Response(
                200,
                json={"login": "operator"},
            )

        if path == "/orgs/acme":
            return httpx.Response(
                200,
                json={"id": 7, "login": "acme"},
            )

        if path == "/orgs/acme/properties/schema":
            return httpx.Response(
                200,
                json=definitions,
            )

        if path == "/orgs/acme/properties/values":
            return httpx.Response(200, json=[])

        raise AssertionError(
            f"Unexpected request: {request.method} {request.url}"
        )

    return httpx.MockTransport(handler)


def test_parser_accepts_limit_and_repeated_repositories() -> None:
    arguments = build_parser().parse_args(
        [
            "properties",
            "--limit",
            "50",
            "--repository",
            "acme/first",
            "--repository",
            "acme/second",
        ]
    )

    assert arguments.limit == 50
    assert arguments.repository == [
        "acme/first",
        "acme/second",
    ]


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_parser_rejects_invalid_property_limit(
    value: str,
) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(
            [
                "properties",
                "--limit",
                value,
            ]
        )

    assert captured.value.code == 2


def test_cli_repository_validation_rejects_other_org(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "properties",
            "--repository",
            "other/repository",
        ]
    )
    config = make_config(tmp_path)

    with pytest.raises(SystemExit) as captured:
        selected_property_repositories(
            parser,
            arguments,
            config,
        )

    assert captured.value.code == 2


def test_cli_repository_validation_rejects_duplicates(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "properties",
            "--repository",
            "acme/repository",
            "--repository",
            "ACME/REPOSITORY",
        ]
    )
    config = make_config(tmp_path)

    with pytest.raises(SystemExit) as captured:
        selected_property_repositories(
            parser,
            arguments,
            config,
        )

    assert captured.value.code == 2


def test_cli_repository_count_cannot_exceed_limit(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "properties",
            "--limit",
            "1",
            "--repository",
            "acme/first",
            "--repository",
            "acme/second",
        ]
    )
    config = make_config(tmp_path)

    with pytest.raises(SystemExit) as captured:
        selected_property_repositories(
            parser,
            arguments,
            config,
        )

    assert captured.value.code == 2


def test_fresh_inventory_arguments_receive_limit(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)

    arguments = fresh_inventory_arguments(
        config,
        tmp_path / "inventory",
        insecure=True,
        limit=50,
    )

    assert arguments.limit == 50
    assert arguments.insecure is True
    assert arguments.output_dir == tmp_path / "inventory"


def test_limit_is_forwarded_and_recorded_in_dry_run(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    records = (
        inventory_record("acme/first", "R_first"),
        inventory_record(
            "acme/second",
            "R_second",
            languages=["java"],
        ),
    )
    calls: list[dict[str, Any]] = []
    methods: list[str] = []

    def inventory_loader(
        received_config,
        output_directory,
        token,
        *,
        insecure,
        limit,
    ):
        calls.append(
            {
                "config": received_config,
                "output_directory": output_directory,
                "token": token,
                "insecure": insecure,
                "limit": limit,
            }
        )
        return 0, inventory_bundle(
            tmp_path,
            records,
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=False,
        refresh_all=False,
        insecure=False,
        limit=2,
        inventory_loader=inventory_loader,
        transport=read_only_handler(methods),
    )

    assert result == 0
    assert calls == [
        {
            "config": config,
            "output_directory": (
                workspace.inventory_directory
            ),
            "token": "test-token",
            "insecure": False,
            "limit": 2,
        }
    ]
    assert methods
    assert set(methods) == {"GET"}

    plan = read_jsonl(
        output / "property-plan.jsonl"
    )
    summary = read_jsonl(
        output / "property-summary.jsonl"
    )[0]
    metadata = next(
        record
        for record in plan
        if record["record_type"]
        == "property_plan_metadata"
    )
    assignments = [
        record
        for record in plan
        if record["record_type"]
        == "repository_property_plan"
    ]

    assert metadata["scope"] == {
        "mode": "limit",
        "limit": 2,
        "repositories": [],
        "selected_repository_count": 2,
    }
    assert summary["scope"] == metadata["scope"]
    assert {
        record["name_with_owner"]
        for record in assignments
    } == {
        "acme/first",
        "acme/second",
    }


def test_exact_repository_scope_filters_full_inventory(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    records = (
        inventory_record("acme/first", "R_first"),
        inventory_record(
            "acme/second",
            "R_second",
            languages=["java"],
        ),
    )
    calls: list[dict[str, Any]] = []
    methods: list[str] = []

    def inventory_loader(
        received_config,
        output_directory,
        token,
        *,
        insecure,
    ):
        calls.append(
            {
                "config": received_config,
                "output_directory": output_directory,
                "token": token,
                "insecure": insecure,
            }
        )
        return 0, inventory_bundle(
            tmp_path,
            records,
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=False,
        refresh_all=False,
        insecure=False,
        limit=1,
        repositories=("acme/second",),
        inventory_loader=inventory_loader,
        transport=read_only_handler(methods),
    )

    assert result == 0
    assert len(calls) == 1
    assert "limit" not in calls[0]
    assert set(methods) == {"GET"}

    plan = read_jsonl(
        output / "property-plan.jsonl"
    )
    summary = read_jsonl(
        output / "property-summary.jsonl"
    )[0]
    metadata = next(
        record
        for record in plan
        if record["record_type"]
        == "property_plan_metadata"
    )
    assignments = [
        record
        for record in plan
        if record["record_type"]
        == "repository_property_plan"
    ]

    assert metadata["scope"] == {
        "mode": "repositories",
        "limit": 1,
        "repositories": ["acme/second"],
        "selected_repository_count": 1,
    }
    assert summary["scope"] == metadata["scope"]
    assert [
        record["name_with_owner"]
        for record in assignments
    ] == ["acme/second"]


def test_exact_repository_apply_mutates_only_selected_repo(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    records = (
        inventory_record("acme/first", "R_first"),
        inventory_record(
            "acme/second",
            "R_second",
            languages=["java"],
        ),
    )
    current: dict[str, dict[str, Any]] = {}
    patch_bodies: list[dict[str, Any]] = []
    requested_paths: list[str] = []

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, inventory_bundle(
            tmp_path,
            records,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested_paths.append(path)

        if path == "/user":
            return httpx.Response(
                200,
                json={"login": "operator"},
            )

        if path == "/orgs/acme":
            return httpx.Response(
                200,
                json={"id": 7, "login": "acme"},
            )

        if path == "/orgs/acme/properties/schema":
            return httpx.Response(
                200,
                json=compatible_definitions(),
            )

        if (
            request.method == "GET"
            and path == "/orgs/acme/properties/values"
        ):
            return httpx.Response(200, json=[])

        if (
            request.method == "GET"
            and path
            == "/repos/acme/second/properties/values"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "property_name": name,
                        "value": value,
                    }
                    for name, value in sorted(
                        current.get("second", {}).items()
                    )
                ],
            )

        if (
            request.method == "PATCH"
            and path == "/orgs/acme/properties/values"
        ):
            body = json.loads(request.content)
            patch_bodies.append(body)

            for repository in body["repository_names"]:
                current[repository] = {
                    item["property_name"]: item["value"]
                    for item in body["properties"]
                }

            return httpx.Response(204)

        raise AssertionError(
            f"Unexpected request: {request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=False,
        insecure=False,
        repositories=("acme/second",),
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert len(patch_bodies) == 1
    assert patch_bodies[0]["repository_names"] == [
        "second"
    ]
    assert "first" not in current
    assert (
        "/repos/acme/first/properties/values"
        not in requested_paths
    )

    apply_records = read_jsonl(
        output / "property-apply.jsonl"
    )
    repository_apply = [
        record
        for record in apply_records
        if record.get("resource_type")
        == "repository_assignment"
    ]

    assert [
        record["name_with_owner"]
        for record in repository_apply
    ] == ["acme/second"]


def test_requested_failed_repository_blocks_before_rest(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    records = (
        inventory_record("acme/good", "R_good"),
    )
    bundle = inventory_bundle(
        tmp_path,
        records,
        additional_states={
            "acme/failed": {
                "checkpoint_status": "failed",
                "repository_id": "R_failed",
                "name_with_owner": "acme/failed",
            }
        },
    )

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 1, bundle

    def handler(
        _request: httpx.Request,
    ) -> httpx.Response:
        raise AssertionError(
            "REST request occurred for invalid repository scope."
        )

    with pytest.raises(OnboardError) as captured:
        run_properties(
            config,
            workspace,
            "test-token",
            apply=True,
            refresh_all=False,
            insecure=False,
            repositories=("acme/failed",),
            inventory_loader=inventory_loader,
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.category == (
        "repository_scope_error"
    )
    assert "acme/failed (failed)" in str(captured.value)


def test_direct_scope_count_guard_runs_before_inventory(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    inventory_called = False

    def inventory_loader(*_args, **_kwargs):
        nonlocal inventory_called
        inventory_called = True
        raise AssertionError(
            "Inventory ran despite an invalid scope."
        )

    with pytest.raises(OnboardError) as captured:
        run_properties(
            config,
            workspace,
            "test-token",
            apply=True,
            refresh_all=False,
            insecure=False,
            limit=1,
            repositories=(
                "acme/first",
                "acme/second",
            ),
            inventory_loader=inventory_loader,
        )

    assert captured.value.category == (
        "repository_scope_error"
    )
    assert inventory_called is False

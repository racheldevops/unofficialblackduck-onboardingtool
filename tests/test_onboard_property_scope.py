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
from github_onboard.config import initialize_config, load_config
from github_onboard.errors import OnboardError
from github_onboard.inventory import fresh_inventory_arguments
from github_onboard.models import InventoryBundle
from github_onboard.properties import run_properties
from github_onboard.workspace import Workspace


def make_config(tmp_path: Path):
    path = tmp_path / "config/onboarding.toml"
    initialize_config(path, "acme")
    return load_config(path)


def record(
    name: str,
    repository_id: str,
    language: str = "python",
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
        "activity_status": "active",
        "detected_languages": [language],
    }


def bundle(
    tmp_path: Path,
    records: tuple[dict[str, Any], ...],
    extra_states: dict[str, dict[str, Any]] | None = None,
) -> InventoryBundle:
    states = {
        item["name_with_owner"]: {
            "checkpoint_status": "successful",
            "repository_id": item["repository_id"],
            "name_with_owner": item["name_with_owner"],
            "inventory": item,
        }
        for item in records
    }
    states.update(extra_states or {})

    return InventoryBundle(
        directory=tmp_path / "inventory",
        inventory=records,
        failures=(),
        summary={},
        checkpoint_metadata={},
        checkpoint_states=states,
        sha256="inventory-sha256",
    )


def definitions() -> list[dict[str, Any]]:
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


def read_transport(
    methods: list[str],
) -> httpx.MockTransport:
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
                json=definitions(),
            )

        if path == "/orgs/acme/properties/values":
            return httpx.Response(200, json=[])

        raise AssertionError(
            f"Unexpected request: {request.method} {request.url}"
        )

    return httpx.MockTransport(handler)


def test_parser_accepts_limit_and_repeated_repository() -> None:
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


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_parser_rejects_invalid_limit(value: str) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(
            ["properties", "--limit", value]
        )

    assert captured.value.code == 2


@pytest.mark.parametrize(
    "repositories",
    [
        ["other/repository"],
        ["invalid"],
        ["acme/repository", "ACME/REPOSITORY"],
    ],
)
def test_cli_rejects_invalid_repository_scope(
    tmp_path: Path,
    repositories: list[str],
) -> None:
    parser = build_parser()
    arguments: list[str] = ["properties"]

    for repository in repositories:
        arguments.extend(
            ["--repository", repository]
        )

    parsed = parser.parse_args(arguments)

    with pytest.raises(SystemExit) as captured:
        selected_property_repositories(
            parser,
            parsed,
            make_config(tmp_path),
        )

    assert captured.value.code == 2


def test_cli_rejects_repository_count_above_limit(
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

    with pytest.raises(SystemExit) as captured:
        selected_property_repositories(
            parser,
            arguments,
            make_config(tmp_path),
        )

    assert captured.value.code == 2


def test_fresh_inventory_receives_limit(
    tmp_path: Path,
) -> None:
    arguments = fresh_inventory_arguments(
        make_config(tmp_path),
        tmp_path / "inventory",
        insecure=True,
        limit=50,
    )

    assert arguments.limit == 50
    assert arguments.insecure is True


def test_limit_is_forwarded_and_audited(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    calls: list[int | None] = []
    methods: list[str] = []
    inventory = bundle(
        tmp_path,
        (
            record("acme/first", "R_first"),
            record("acme/second", "R_second", "java"),
        ),
    )

    def loader(
        _config,
        _directory,
        _token,
        *,
        insecure,
        limit,
    ):
        assert insecure is False
        calls.append(limit)
        return 0, inventory

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=False,
        refresh_all=False,
        insecure=False,
        limit=2,
        inventory_loader=loader,
        transport=read_transport(methods),
    )

    assert result == 0
    assert calls == [2]
    assert set(methods) == {"GET"}

    plan = read_jsonl(
        output / "property-plan.jsonl"
    )
    metadata = next(
        item
        for item in plan
        if item["record_type"]
        == "property_plan_metadata"
    )

    assert metadata["scope"] == {
        "mode": "limit",
        "limit": 2,
        "repositories": [],
        "selected_repository_count": 2,
    }


def test_exact_repository_filters_full_inventory(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    methods: list[str] = []
    inventory = bundle(
        tmp_path,
        (
            record("acme/first", "R_first"),
            record("acme/second", "R_second", "java"),
        ),
    )

    def loader(
        _config,
        _directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, inventory

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=False,
        refresh_all=False,
        insecure=False,
        limit=1,
        repositories=("acme/second",),
        inventory_loader=loader,
        transport=read_transport(methods),
    )

    assert result == 0
    plan = read_jsonl(
        output / "property-plan.jsonl"
    )
    assignments = [
        item
        for item in plan
        if item["record_type"]
        == "repository_property_plan"
    ]
    metadata = next(
        item
        for item in plan
        if item["record_type"]
        == "property_plan_metadata"
    )

    assert [
        item["name_with_owner"]
        for item in assignments
    ] == ["acme/second"]
    assert metadata["scope"] == {
        "mode": "repositories",
        "limit": 1,
        "repositories": ["acme/second"],
        "selected_repository_count": 1,
    }


def test_exact_repository_apply_mutates_only_target(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    inventory = bundle(
        tmp_path,
        (
            record("acme/first", "R_first"),
            record("acme/second", "R_second", "java"),
        ),
    )
    current: dict[str, dict[str, Any]] = {}
    patch_bodies: list[dict[str, Any]] = []
    requested_paths: list[str] = []

    def loader(
        _config,
        _directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, inventory

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
                json=definitions(),
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

    result, _output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=False,
        insecure=False,
        repositories=("acme/second",),
        inventory_loader=loader,
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


def test_failed_requested_repository_blocks_before_rest(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    inventory = bundle(
        tmp_path,
        (record("acme/good", "R_good"),),
        {
            "acme/failed": {
                "checkpoint_status": "failed",
                "repository_id": "R_failed",
                "name_with_owner": "acme/failed",
            }
        },
    )

    def loader(
        _config,
        _directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 1, inventory

    def handler(
        _request: httpx.Request,
    ) -> httpx.Response:
        raise AssertionError(
            "REST was reached for an invalid scope."
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
            inventory_loader=loader,
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.category == (
        "repository_scope_error"
    )
    assert "acme/failed (failed)" in str(
        captured.value
    )


def test_scope_count_guard_precedes_inventory(
    tmp_path: Path,
) -> None:
    called = False

    def loader(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Inventory should not run.")

    with pytest.raises(OnboardError):
        run_properties(
            make_config(tmp_path),
            Workspace.from_root(
                tmp_path / ".inventory"
            ),
            "test-token",
            apply=True,
            refresh_all=False,
            insecure=False,
            limit=1,
            repositories=(
                "acme/first",
                "acme/second",
            ),
            inventory_loader=loader,
        )

    assert called is False

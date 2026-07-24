from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from github_onboard.config import (
    initialize_config,
    load_config,
)
from github_onboard.models import (
    InventoryBundle,
    OnboardingConfig,
)
from github_onboard.properties import run_properties
from github_onboard.workspace import Workspace


def make_config(tmp_path: Path) -> OnboardingConfig:
    path = tmp_path / "configuration/onboarding.toml"
    initialize_config(path, "acme")
    return load_config(path)


def inventory_record(
    *,
    name: str = "acme/repository",
    repository_id: str = "R_repository",
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


def make_bundle(
    tmp_path: Path,
    *,
    records: tuple[dict[str, Any], ...] | None = None,
    failures: tuple[dict[str, Any], ...] = (),
) -> InventoryBundle:
    selected = records or (inventory_record(),)

    return InventoryBundle(
        directory=tmp_path / "inventory",
        inventory=selected,
        failures=failures,
        summary={},
        checkpoint_metadata={},
        checkpoint_states={},
        sha256="inventory-sha256",
    )


def compatible_definitions(
    languages: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "property_name": "blackduck_activity",
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": "Activity",
            "allowed_values": [
                "active",
                "inactive",
            ],
        },
        {
            "property_name": "blackduck_languages",
            "value_type": "multi_select",
            "required": False,
            "default_value": None,
            "description": "Languages",
            "allowed_values": (
                ["python"]
                if languages is None
                else languages
            ),
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


def test_property_dry_run_runs_inventory_and_never_mutates(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    inventory_calls: list[tuple[Path, bool]] = []
    request_methods: list[str] = []

    def inventory_loader(
        received_config,
        output_directory,
        token,
        *,
        insecure,
    ):
        assert received_config == config
        assert token == "test-token"
        inventory_calls.append(
            (output_directory, insecure)
        )
        return 0, make_bundle(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)

        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={"login": "operator"},
            )

        if request.url.path == "/orgs/acme":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "login": "acme",
                    "plan": {"name": "free"},
                },
            )

        if request.url.path.endswith(
            "/properties/schema"
        ):
            return httpx.Response(200, json=[])

        if request.url.path.endswith(
            "/properties/values"
        ):
            return httpx.Response(200, json=[])

        raise AssertionError(
            f"Unexpected request: "
            f"{request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=False,
        refresh_all=False,
        insecure=False,
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert inventory_calls == [
        (workspace.inventory_directory, False)
    ]
    assert request_methods
    assert set(request_methods) == {"GET"}

    plan = read_jsonl(
        output / "property-plan.jsonl"
    )
    summary = read_jsonl(
        output / "property-summary.jsonl"
    )[0]
    assignments = [
        record
        for record in plan
        if record.get("record_type")
        == "repository_property_plan"
    ]

    assert assignments[0]["action"] == "initialize"
    assert summary["mode"] == "dry_run"
    assert summary["mutation_occurred"] is False
    assert not (
        output / "property-apply.jsonl"
    ).exists()

    latest = json.loads(
        (
            workspace.properties_directory
            / "latest.json"
        ).read_text(encoding="utf-8")
    )

    assert latest["run_id"] == output.name
    assert latest["mutation_occurred"] is False


def test_property_apply_creates_assigns_and_verifies(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    definitions: dict[str, dict[str, Any]] = {}
    repository_values: dict[str, dict[str, Any]] = {}
    request_methods: list[str] = []

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, make_bundle(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        path = request.url.path

        if path == "/user":
            return httpx.Response(
                200,
                json={"login": "operator"},
            )

        if path == "/orgs/acme":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "login": "acme",
                    "plan": {"name": "enterprise"},
                },
            )

        if (
            request.method == "GET"
            and path
            == "/orgs/acme/properties/schema"
        ):
            return httpx.Response(
                200,
                json=list(definitions.values()),
            )

        if (
            request.method == "GET"
            and path
            == "/orgs/acme/properties/values"
        ):
            return httpx.Response(200, json=[])

        if (
            request.method == "PUT"
            and path.startswith(
                "/orgs/acme/properties/schema/"
            )
        ):
            body = json.loads(request.content)
            name = path.rsplit("/", 1)[-1]
            definition = {
                "property_name": name,
                **body,
            }
            definitions[name] = definition
            return httpx.Response(
                200,
                json=definition,
            )

        if (
            request.method == "GET"
            and path.startswith("/repos/acme/")
            and path.endswith("/properties/values")
        ):
            repository = path.split("/")[3]
            values = repository_values.get(
                repository,
                {},
            )
            return httpx.Response(
                200,
                json=[
                    {
                        "property_name": name,
                        "value": value,
                    }
                    for name, value in sorted(
                        values.items()
                    )
                ],
            )

        if (
            request.method == "PATCH"
            and path
            == "/orgs/acme/properties/values"
        ):
            body = json.loads(request.content)
            values = {
                item["property_name"]: item["value"]
                for item in body["properties"]
            }

            for repository in body["repository_names"]:
                repository_values.setdefault(
                    repository,
                    {},
                ).update(values)

            return httpx.Response(204)

        raise AssertionError(
            f"Unexpected request: "
            f"{request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=False,
        insecure=False,
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert set(definitions) == {
        "blackduck_activity",
        "blackduck_languages",
        "blackduck_sca_policy",
    }
    assert repository_values["repository"] == {
        "blackduck_activity": "active",
        "blackduck_languages": ["python"],
        "blackduck_sca_policy": "required",
    }
    assert request_methods.count("PUT") == 3
    assert request_methods.count("PATCH") == 1

    apply_records = read_jsonl(
        output / "property-apply.jsonl"
    )
    verification = read_jsonl(
        output / "property-verification.jsonl"
    )
    rollback = read_jsonl(
        output / "property-rollback-plan.jsonl"
    )
    summary = read_jsonl(
        output / "property-summary.jsonl"
    )[0]

    assert any(
        record.get("resource_type")
        == "repository_assignment"
        and record.get("result") == "applied"
        for record in apply_records
    )
    assert any(
        record.get("name_with_owner")
        == "acme/repository"
        and record.get("result") == "verified"
        for record in verification
    )
    assert any(
        record.get("record_type")
        == "repository_property_rollback"
        and record.get("restore_values")
        == {
            "blackduck_activity": None,
            "blackduck_languages": None,
            "blackduck_sca_policy": None,
        }
        for record in rollback
    )
    assert summary["result"] == 0
    assert summary["mutation_requested"] is True
    assert summary["mutation_occurred"] is True


def test_apply_preserves_existing_manual_values(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    mutation_methods: list[str] = []
    definitions = compatible_definitions()
    existing = [
        {
            "property_name": "blackduck_activity",
            "value": "inactive",
        }
    ]

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, make_bundle(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if request.method != "GET":
            mutation_methods.append(request.method)
            return httpx.Response(204)

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
            return httpx.Response(
                200,
                json=[
                    {
                        "repository_full_name": (
                            "acme/repository"
                        ),
                        "properties": existing,
                    }
                ],
            )

        if path == (
            "/repos/acme/repository/properties/values"
        ):
            return httpx.Response(
                200,
                json=existing,
            )

        raise AssertionError(
            f"Unexpected request: "
            f"{request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=False,
        insecure=False,
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert mutation_methods == []

    verification = read_jsonl(
        output / "property-verification.jsonl"
    )

    assert any(
        record.get("result") == "skipped_existing"
        for record in verification
    )


def test_refresh_all_updates_only_managed_values(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    definitions = compatible_definitions(
        ["python", "ruby"]
    )
    current = {
        "blackduck_activity": "inactive",
        "blackduck_languages": ["ruby"],
        "blackduck_sca_policy": "review",
        "unrelated_property": "manual",
    }
    patch_bodies: list[dict[str, Any]] = []

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, make_bundle(tmp_path)

    def property_list() -> list[dict[str, Any]]:
        return [
            {
                "property_name": name,
                "value": value,
            }
            for name, value in sorted(current.items())
        ]

    def handler(request: httpx.Request) -> httpx.Response:
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

        if (
            request.method == "GET"
            and path == "/orgs/acme/properties/schema"
        ):
            return httpx.Response(
                200,
                json=definitions,
            )

        if (
            request.method == "GET"
            and path == "/orgs/acme/properties/values"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "repository_full_name": (
                            "acme/repository"
                        ),
                        "properties": property_list(),
                    }
                ],
            )

        if (
            request.method == "GET"
            and path
            == "/repos/acme/repository/properties/values"
        ):
            return httpx.Response(
                200,
                json=property_list(),
            )

        if (
            request.method == "PATCH"
            and path == "/orgs/acme/properties/values"
        ):
            body = json.loads(request.content)
            patch_bodies.append(body)

            for item in body["properties"]:
                current[item["property_name"]] = item["value"]

            return httpx.Response(204)

        raise AssertionError(
            f"Unexpected request: "
            f"{request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=True,
        insecure=False,
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert len(patch_bodies) == 1
    assert current == {
        "blackduck_activity": "active",
        "blackduck_languages": ["python"],
        "blackduck_sca_policy": "required",
        "unrelated_property": "manual",
    }

    apply_records = read_jsonl(
        output / "property-apply.jsonl"
    )

    assert any(
        record.get("action") == "update"
        and record.get("result") == "applied"
        for record in apply_records
    )


def test_repository_failure_does_not_block_other_group(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    records = (
        inventory_record(
            name="acme/good",
            repository_id="R_good",
            activity="active",
            languages=["python"],
        ),
        inventory_record(
            name="acme/bad",
            repository_id="R_bad",
            activity="inactive",
            languages=["java"],
        ),
    )
    bundle = make_bundle(
        tmp_path,
        records=records,
    )
    definitions = compatible_definitions(
        ["java", "python"]
    )
    resulting_values: dict[str, dict[str, Any]] = {}

    def inventory_loader(
        _config,
        _output_directory,
        _token,
        *,
        insecure,
    ):
        assert insecure is False
        return 0, bundle

    def handler(request: httpx.Request) -> httpx.Response:
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

        if (
            request.method == "GET"
            and path == "/orgs/acme/properties/schema"
        ):
            return httpx.Response(
                200,
                json=definitions,
            )

        if (
            request.method == "GET"
            and path == "/orgs/acme/properties/values"
        ):
            return httpx.Response(200, json=[])

        if (
            request.method == "GET"
            and path.startswith("/repos/acme/")
            and path.endswith("/properties/values")
        ):
            repository = path.split("/")[3]
            values = resulting_values.get(
                repository,
                {},
            )
            return httpx.Response(
                200,
                json=[
                    {
                        "property_name": name,
                        "value": value,
                    }
                    for name, value in sorted(
                        values.items()
                    )
                ],
            )

        if (
            request.method == "PATCH"
            and path == "/orgs/acme/properties/values"
        ):
            body = json.loads(request.content)
            values = {
                item["property_name"]: item["value"]
                for item in body["properties"]
            }

            if values["blackduck_activity"] == "inactive":
                return httpx.Response(
                    422,
                    json={"message": "Repository rejected."},
                )

            for repository in body["repository_names"]:
                resulting_values[repository] = dict(values)

            return httpx.Response(204)

        raise AssertionError(
            f"Unexpected request: "
            f"{request.method} {request.url}"
        )

    result, output = run_properties(
        config,
        workspace,
        "test-token",
        apply=True,
        refresh_all=False,
        insecure=False,
        inventory_loader=inventory_loader,
        transport=httpx.MockTransport(handler),
    )

    assert result == 1
    assert resulting_values["good"] == {
        "blackduck_activity": "active",
        "blackduck_languages": ["python"],
        "blackduck_sca_policy": "required",
    }
    assert "bad" not in resulting_values

    apply_records = read_jsonl(
        output / "property-apply.jsonl"
    )
    summary = read_jsonl(
        output / "property-summary.jsonl"
    )[0]

    assert any(
        record.get("name_with_owner") == "acme/good"
        and record.get("result") == "applied"
        for record in apply_records
    )
    assert any(
        record.get("name_with_owner") == "acme/bad"
        and record.get("result") == "failed"
        for record in apply_records
    )
    assert summary["repository_failure_count"] == 1
    assert summary["result"] == 1

from __future__ import annotations

import argparse
import datetime as dt
import json
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_inventory.classification import (
    build_excluded_record,
    build_inventory_record,
    classify_languages,
    normalize_language,
    validate_repository_metadata,
)
from github_inventory.errors import (
    GitHubError,
    InventoryError,
    RuntimeBudgetExceeded,
)
from github_inventory.github.client import GitHubClient
from github_inventory.github.repositories import (
    discover_repositories,
    inspect_manifest_paths,
    preflight,
    select_representative_repositories,
)
from github_inventory.models import ClientStats
from github_inventory.settings import (
    CHECKPOINT_SCHEMA_VERSION,
    LANGUAGE_POLICY_VERSION,
    PILOT_SELECTION_METHOD,
)
from github_inventory.storage.checkpoints import (
    CheckpointWriter,
    checkpoint_configuration,
    initialize_checkpoint,
    load_checkpoint,
    reconcile,
    validate_checkpoint,
)


inventory_module = __import__(
    "github_inventory.workflows.inventory",
    fromlist=["run_inventory"],
)
preflight_module = __import__(
    "github_inventory.workflows.preflight",
    fromlist=["run_preflight"],
)


class StubClient:

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "variables": variables,
                "operation": operation,
            }
        )

        if not self.responses:
            raise AssertionError("Unexpected GraphQL call.")

        return self.responses.pop(0)


class WorkflowClient:

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def stats(self) -> ClientStats:
        return ClientStats(
            requests=3,
            graphql_cost=3,
            retries=0,
        )


@pytest.fixture(autouse=True)
def block_network(monkeypatch) -> None:

    def deny_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Tests must not access the network.")

    monkeypatch.setattr(
        socket,
        "create_connection",
        deny_network,
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        deny_network,
    )


def page(
    nodes: list[dict[str, Any]],
    total: int,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "organization": {
            "repositories": {
                "totalCount": total,
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": cursor,
                },
            }
        }
    }


def workflow_arguments(
    output: Path,
    *,
    resume: bool = False,
    limit: int | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        max_hours=2.0,
        output_dir=output,
        dry_run=False,
        discard_checkpoint=False,
        resume=resume,
        timeout=30.0,
        retries=2,
        inspection_depth="root",
        limit=limit,
        insecure=False,
        workers=2,
    )


def configure_inventory(
    monkeypatch,
    repositories: list[dict[str, Any]],
    *,
    total: int | None = None,
    administrator: bool = True,
    paths_by_name: dict[str, list[str]] | None = None,
) -> None:
    selected_total = len(repositories) if total is None else total
    manifest_paths = paths_by_name or {}

    monkeypatch.setattr(
        inventory_module,
        "GitHubClient",
        WorkflowClient,
    )
    monkeypatch.setattr(
        inventory_module,
        "preflight",
        lambda *_args, **_kwargs: {
            "viewer_can_administer": administrator,
            "visible_repository_count": selected_total,
            "graphql_rate_remaining": 4999,
        },
    )
    monkeypatch.setattr(
        inventory_module,
        "discover_repositories",
        lambda *_args, **_kwargs: (
            list(repositories),
            selected_total,
        ),
    )

    def inspections(
        _client: Any,
        candidates: list[dict[str, Any]],
        *,
        depth: str,
        workers: int,
    ):
        assert depth == "root"
        assert workers == 2

        for repository in candidates:
            name = repository["nameWithOwner"]
            yield repository, manifest_paths.get(name, []), None

    monkeypatch.setattr(
        inventory_module,
        "bounded_inspections",
        inspections,
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_python_packaging_imports_and_entry_points() -> None:
    root = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert sys.version_info[:2] == (3, 12)
    assert "3.12" in configuration["project"]["requires-python"]
    assert configuration["build-system"]["build-backend"]
    assert any(
        requirement.casefold().startswith("httpx")
        for requirement in configuration["project"]["dependencies"]
    )
    assert any(
        requirement.casefold().startswith("pytest")
        for requirement in configuration[
            "project"
        ]["optional-dependencies"]["test"]
    )

    module_result = subprocess.run(
        [sys.executable, "-m", "github_inventory", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    script = Path(sys.executable).with_name("github-inventory")
    script_result = subprocess.run(
        [str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert module_result.returncode == 0
    assert script.exists()
    assert script_result.returncode == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Python", "python"),
        ("Jupyter Notebook", "jupyter-notebook"),
        ("  Visual   Basic  ", "visual-basic"),
        ("C#", "c#"),
        ("C++", "c++"),
    ],
)
def test_language_normalization_policy(
    value: str,
    expected: str,
) -> None:
    assert normalize_language(value) == expected
    assert " " not in expected


def test_disk_usage_never_adds_unknown_to_known_language() -> None:
    edges = [
        {
            "size": 100,
            "node": {"name": "Python"},
        }
    ]

    assert classify_languages(
        edges,
        100,
        999_999,
    ) == ["python"]


def test_unknown_is_exact_when_no_language_qualifies() -> None:
    assert classify_languages([], 0, 999_999) == ["unknown"]


def test_manifest_does_not_fabricate_absent_language() -> None:
    edges = [
        {
            "size": 100,
            "node": {"name": "Python"},
        }
    ]

    assert classify_languages(
        edges,
        100,
        0,
        ["pom.xml"],
    ) == ["python"]


def test_truncated_language_connection_is_a_failure(
    repository_factory,
) -> None:
    repository = repository_factory()
    repository["languages"]["pageInfo"]["hasNextPage"] = True

    with pytest.raises(InventoryError, match="truncated"):
        build_inventory_record(
            repository,
            dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("nameWithOwner", "invalid"),
        ("url", "http://github.com/acme/repository"),
        ("visibility", "SECRET"),
        ("isArchived", 1),
        ("isFork", "false"),
        ("isTemplate", None),
        ("pushedAt", "not-a-timestamp"),
        ("diskUsage", -1),
        ("defaultBranchRef", {"name": ""}),
    ],
)
def test_malformed_repository_metadata_is_rejected(
    repository_factory,
    field: str,
    value: Any,
) -> None:
    repository = repository_factory(**{field: value})

    with pytest.raises(InventoryError):
        validate_repository_metadata(repository)


@pytest.mark.parametrize(
    ("archived", "template", "reason"),
    [
        (True, False, "archived"),
        (False, True, "template"),
        (True, True, "archived_and_template"),
    ],
)
def test_excluded_checkpoint_record(
    repository_factory,
    archived: bool,
    template: bool,
    reason: str,
) -> None:
    repository = repository_factory(
        isArchived=archived,
        isTemplate=template,
    )

    assert build_excluded_record(repository) == {
        "checkpoint_status": "excluded",
        "repository_id": repository["id"],
        "name_with_owner": repository["nameWithOwner"],
        "exclusion_reason": reason,
    }


def test_preflight_reports_administration_capability() -> None:
    client = StubClient(
        [
            {
                "viewer": {"login": "operator"},
                "organization": {
                    "login": "acme",
                    "viewerCanAdminister": False,
                    "repositories": {"totalCount": 4},
                },
                "rateLimit": {
                    "limit": 5000,
                    "remaining": 4999,
                    "resetAt": "2099-01-01T00:00:00Z",
                },
            }
        ]
    )

    result = preflight(client, "acme")

    assert result["viewer_can_administer"] is False
    assert result["visible_repository_count"] == 4


def test_preflight_rejects_malformed_administration_capability() -> None:
    client = StubClient(
        [
            {
                "viewer": {"login": "operator"},
                "organization": {
                    "login": "acme",
                    "viewerCanAdminister": "yes",
                    "repositories": {"totalCount": 4},
                },
                "rateLimit": {
                    "limit": 5000,
                    "remaining": 4999,
                    "resetAt": "2099-01-01T00:00:00Z",
                },
            }
        ]
    )

    with pytest.raises(GitHubError) as captured:
        preflight(client, "acme")

    assert captured.value.category == "invalid_response"


def test_discovery_rejects_duplicate_repository_id(
    repository_factory,
) -> None:
    first = repository_factory(name="first")
    second = repository_factory(name="second", id=first["id"])
    client = StubClient([page([first, second], 2)])

    with pytest.raises(GitHubError, match="duplicate repository ID"):
        discover_repositories(client, "acme")


def test_discovery_rejects_duplicate_repository_name(
    repository_factory,
) -> None:
    first = repository_factory(name="first")
    second = repository_factory(
        name="second",
        id="R_second",
        nameWithOwner=first["nameWithOwner"],
    )
    client = StubClient([page([first, second], 2)])

    with pytest.raises(GitHubError, match="duplicate repository name"):
        discover_repositories(client, "acme")


def test_discovery_rejects_empty_continued_page() -> None:
    client = StubClient(
        [
            page(
                [],
                1,
                has_next=True,
                cursor="cursor-1",
            )
        ]
    )

    with pytest.raises(GitHubError, match="empty page"):
        discover_repositories(client, "acme")


def test_discovery_rejects_changing_total(
    repository_factory,
) -> None:
    client = StubClient(
        [
            page(
                [repository_factory(name="first")],
                2,
                has_next=True,
                cursor="cursor-1",
            ),
            page(
                [repository_factory(name="second")],
                3,
            ),
        ]
    )

    with pytest.raises(GitHubError, match="total changed"):
        discover_repositories(client, "acme")


def test_discovery_rejects_final_count_mismatch(
    repository_factory,
) -> None:
    client = StubClient(
        [
            page(
                [repository_factory(name="first")],
                2,
            )
        ]
    )

    with pytest.raises(GitHubError, match="count does not match"):
        discover_repositories(client, "acme")


def test_discovery_rejects_any_repeated_cursor(
    repository_factory,
) -> None:
    client = StubClient(
        [
            page(
                [repository_factory(name="first")],
                4,
                has_next=True,
                cursor="cursor-1",
            ),
            page(
                [repository_factory(name="second")],
                4,
                has_next=True,
                cursor="cursor-2",
            ),
            page(
                [repository_factory(name="third")],
                4,
                has_next=True,
                cursor="cursor-1",
            ),
        ]
    )

    with pytest.raises(GitHubError, match="repeated pagination cursor"):
        discover_repositories(client, "acme")


def test_pilot_discovers_every_repository_before_sampling(
    repository_factory,
) -> None:
    repositories = [
        repository_factory(name="alpha"),
        repository_factory(name="beta"),
        repository_factory(name="gamma"),
    ]
    client = StubClient(
        [
            page(
                repositories[:2],
                3,
                has_next=True,
                cursor="cursor-1",
            ),
            page(repositories[2:], 3),
        ]
    )

    selected, total = discover_repositories(
        client,
        "acme",
        limit=2,
    )

    assert total == 3
    assert len(client.calls) == 2
    assert selected == select_representative_repositories(
        repositories,
        2,
    )
    assert selected == select_representative_repositories(
        repositories,
        2,
    )


def test_tree_object_type_is_validated(
    repository_factory,
) -> None:
    client = StubClient(
        [
            {
                "repository": {
                    "object": {
                        "__typename": "Blob",
                    }
                }
            }
        ]
    )

    with pytest.raises(GitHubError, match="non-tree"):
        inspect_manifest_paths(
            client,
            repository_factory(),
            "root",
        )


def test_runtime_deadline_is_checked_before_request() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"data": {"ok": True}},
        )

    client = GitHubClient(
        "test-token",
        deadline=time.monotonic() - 1.0,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(RuntimeBudgetExceeded):
            client.graphql(
                "query Test { viewer { login } }",
                {},
                operation="deadline test",
            )
    finally:
        client.close()

    assert requests == 0


def test_required_wait_cannot_reach_or_exceed_deadline() -> None:
    client = GitHubClient(
        "test-token",
        deadline=time.monotonic() + 0.01,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"data": {"ok": True}},
            )
        ),
    )

    try:
        with pytest.raises(RuntimeBudgetExceeded):
            client._sleep(1.0)
    finally:
        client.close()


def test_secondary_rate_limit_is_retried() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            return httpx.Response(
                403,
                text="You have exceeded a secondary rate limit.",
            )

        return httpx.Response(
            200,
            json={"data": {"ok": True}},
        )

    client = GitHubClient(
        "test-token",
        max_attempts=2,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
        jitter=lambda _minimum, _maximum: 0.0,
    )

    try:
        result = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="secondary limit test",
        )
    finally:
        client.close()

    assert result["ok"] is True
    assert attempts == 2
    assert sleeps == [1.0]


def test_duplicate_checkpoint_repository_is_rejected(
    tmp_path,
) -> None:
    path = tmp_path / "checkpoint.jsonl"
    configuration = checkpoint_configuration(
        "acme",
        dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        "root",
    )
    record = {
        "checkpoint_status": "excluded",
        "repository_id": "R_repository",
        "name_with_owner": "acme/repository",
        "exclusion_reason": "archived",
    }

    initialize_checkpoint(path, configuration)

    with CheckpointWriter(path, flush_every=1) as writer:
        writer.append(record)
        writer.append(record)

    with pytest.raises(InventoryError, match="duplicate repository"):
        load_checkpoint(path)


def test_obsolete_language_checkpoint_is_rejected() -> None:
    metadata = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "language_policy": (
            "primary-plus-manifest-secondary-"
            "unknown-disk-majority-v1"
        ),
    }

    with pytest.raises(InventoryError, match="mismatch"):
        validate_checkpoint(
            metadata,
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "language_policy": LANGUAGE_POLICY_VERSION,
            },
        )


def test_reconciliation_supports_disjoint_exclusions() -> None:
    assert reconcile(
        {"acme/good", "acme/archived", "acme/failed"},
        [{"name_with_owner": "acme/good"}],
        [{"name_with_owner": "acme/failed"}],
        [
            {
                "name_with_owner": "acme/archived",
                "exclusion_reason": "archived",
            }
        ],
        discovered_count=3,
    )


def test_standalone_preflight_fails_without_administration(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        preflight_module,
        "GitHubClient",
        WorkflowClient,
    )
    monkeypatch.setattr(
        preflight_module,
        "preflight",
        lambda *_args, **_kwargs: {
            "record_type": "preflight",
            "organization": "acme",
            "viewer": "operator",
            "viewer_can_administer": False,
            "visible_repository_count": 4,
            "graphql_rate_limit": 5000,
            "graphql_rate_remaining": 4999,
            "graphql_rate_reset_at": "2099-01-01T00:00:00Z",
        },
    )
    arguments = argparse.Namespace(
        max_hours=2.0,
        timeout=30.0,
        retries=2,
        insecure=False,
    )

    result = preflight_module.run_preflight(
        arguments,
        "acme",
        "test-token",
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    event = json.loads(captured.err)

    assert result == 2
    assert output["viewer_can_administer"] is False
    assert event["record_type"] == "fatal_error"
    assert event["category"] == "administration_required"


def test_inventory_omits_exclusions_and_reports_summary(
    monkeypatch,
    tmp_path,
    repository_factory,
) -> None:
    known = repository_factory(
        name="known",
        isFork=True,
        disk_usage_kb=999_999,
    )
    unknown = repository_factory(
        name="unknown",
        pushed_at=None,
        languages=(),
    )
    multiple = repository_factory(
        name="multiple",
        languages=(
            ("JavaScript", 700),
            ("TypeScript", 300),
        ),
    )
    archived = repository_factory(
        name="archived",
        isArchived=True,
    )
    template = repository_factory(
        name="template",
        isTemplate=True,
    )
    repositories = [
        known,
        unknown,
        multiple,
        archived,
        template,
    ]
    output = tmp_path / "inventory"

    configure_inventory(
        monkeypatch,
        repositories,
        paths_by_name={
            "acme/multiple": ["package.json"],
        },
    )

    result = inventory_module.run_inventory(
        workflow_arguments(output),
        "acme",
        "test-token",
    )
    inventory = read_jsonl(output / "inventory.jsonl")
    failures = read_jsonl(output / "failures.jsonl")
    summary = read_jsonl(output / "summary.jsonl")[0]
    metadata, states = load_checkpoint(
        output / "checkpoint.jsonl"
    )

    assert result == 0
    assert failures == []
    assert {
        record["name_with_owner"]
        for record in inventory
    } == {
        "acme/known",
        "acme/unknown",
        "acme/multiple",
    }
    assert all(
        "exclusion_reason" not in record
        for record in inventory
    )
    assert next(
        record
        for record in inventory
        if record["name_with_owner"] == "acme/known"
    )["detected_languages"] == ["python"]
    assert next(
        record
        for record in inventory
        if record["name_with_owner"] == "acme/known"
    )["is_fork"] is True
    assert next(
        record
        for record in inventory
        if record["name_with_owner"] == "acme/unknown"
    )["detected_languages"] == ["unknown"]
    assert next(
        record
        for record in inventory
        if record["name_with_owner"] == "acme/multiple"
    )["detected_languages"] == [
        "javascript",
        "typescript",
    ]
    assert summary["run_mode"] == "full"
    assert summary["discovered_repository_count"] == 5
    assert summary["selected_repository_count"] == 5
    assert summary["successful_repository_count"] == 3
    assert summary["excluded_repository_count"] == 2
    assert summary["failed_repository_count"] == 0
    assert summary["unknown_language_count"] == 1
    assert summary["multilanguage_repository_count"] == 1
    assert summary["reconciliation_ok"] is True
    assert summary["aborted"] is False
    assert (
        summary["projected_full_organization_seconds"]
        >= summary["elapsed_seconds"]
    )
    assert metadata["pilot_selection_method"] == PILOT_SELECTION_METHOD
    assert states["acme/archived"]["checkpoint_status"] == "excluded"
    assert states["acme/template"]["checkpoint_status"] == "excluded"


def test_repository_failure_preserves_successful_output(
    monkeypatch,
    tmp_path,
    repository_factory,
) -> None:
    good = repository_factory(name="good")
    bad = repository_factory(
        name="bad",
        pushed_at="not-a-timestamp",
    )
    output = tmp_path / "inventory"

    configure_inventory(
        monkeypatch,
        [good, bad],
    )

    result = inventory_module.run_inventory(
        workflow_arguments(output),
        "acme",
        "test-token",
    )
    inventory = read_jsonl(output / "inventory.jsonl")
    failures = read_jsonl(output / "failures.jsonl")
    summary = read_jsonl(output / "summary.jsonl")[0]

    assert result == 1
    assert [
        record["name_with_owner"]
        for record in inventory
    ] == ["acme/good"]
    assert len(failures) == 1
    assert failures[0]["name_with_owner"] == "acme/bad"
    assert "detected_languages" not in failures[0]
    assert "activity_status" not in failures[0]
    assert "test-token" not in json.dumps(failures)
    assert summary["successful_repository_count"] == 1
    assert summary["failed_repository_count"] == 1
    assert summary["reconciliation_ok"] is True
    assert summary["aborted"] is False


@pytest.mark.parametrize("preexisting", [False, True])
def test_fatal_discovery_preserves_final_artifacts(
    monkeypatch,
    tmp_path,
    preexisting: bool,
) -> None:
    output = tmp_path / "inventory"
    original = {
        "inventory.jsonl": '{"name_with_owner":"acme/existing"}\n',
        "failures.jsonl": "",
        "summary.jsonl": '{"record_type":"summary"}\n',
    }

    if preexisting:
        output.mkdir()

        for name, content in original.items():
            (output / name).write_text(
                content,
                encoding="utf-8",
            )

    monkeypatch.setattr(
        inventory_module,
        "GitHubClient",
        WorkflowClient,
    )
    monkeypatch.setattr(
        inventory_module,
        "preflight",
        lambda *_args, **_kwargs: {
            "viewer_can_administer": True,
            "visible_repository_count": 1,
            "graphql_rate_remaining": 4999,
        },
    )

    def failed_discovery(*_args, **_kwargs):
        raise GitHubError(
            "pagination_error",
            "Discovery failed.",
            attempts=1,
        )

    monkeypatch.setattr(
        inventory_module,
        "discover_repositories",
        failed_discovery,
    )

    result = inventory_module.run_inventory(
        workflow_arguments(output),
        "acme",
        "test-token",
    )

    assert result == 2

    for name, content in original.items():
        path = output / name

        if preexisting:
            assert path.read_text(encoding="utf-8") == content
        else:
            assert not path.exists()


def test_checkpoint_resume_preserves_cutoff_and_exclusions(
    monkeypatch,
    tmp_path,
    repository_factory,
) -> None:
    good = repository_factory(name="good")
    archived = repository_factory(
        name="archived",
        isArchived=True,
    )
    repositories = [good, archived]
    output = tmp_path / "inventory"

    configure_inventory(monkeypatch, repositories)

    first_result = inventory_module.run_inventory(
        workflow_arguments(output),
        "acme",
        "test-token",
    )
    first_summary = read_jsonl(
        output / "summary.jsonl"
    )[0]

    second_result = inventory_module.run_inventory(
        workflow_arguments(output, resume=True),
        "acme",
        "test-token",
    )
    second_summary = read_jsonl(
        output / "summary.jsonl"
    )[0]
    inventory = read_jsonl(output / "inventory.jsonl")

    assert first_result == 0
    assert second_result == 0
    assert (
        second_summary["activity_cutoff"]
        == first_summary["activity_cutoff"]
    )
    assert second_summary["excluded_repository_count"] == 1
    assert [
        record["name_with_owner"]
        for record in inventory
    ] == ["acme/good"]


def test_pilot_summary_distinguishes_projection(
    monkeypatch,
    tmp_path,
    repository_factory,
) -> None:
    selected = [
        repository_factory(name="alpha"),
        repository_factory(name="beta"),
    ]
    output = tmp_path / "inventory"

    configure_inventory(
        monkeypatch,
        selected,
        total=10,
    )

    result = inventory_module.run_inventory(
        workflow_arguments(output, limit=2),
        "acme",
        "test-token",
    )
    summary = read_jsonl(output / "summary.jsonl")[0]

    assert result == 0
    assert summary["run_mode"] == "pilot"
    assert summary["discovered_repository_count"] == 10
    assert summary["selected_repository_count"] == 2
    assert summary["pilot_limit"] == 2
    assert (
        summary["pilot_selection_method"]
        == PILOT_SELECTION_METHOD
    )
    assert (
        summary["projected_full_organization_seconds"]
        >= summary["elapsed_seconds"]
    )

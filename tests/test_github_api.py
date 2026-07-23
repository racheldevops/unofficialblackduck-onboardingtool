from __future__ import annotations

import importlib
import time
from typing import Any

import httpx
import pytest

from github_inventory.errors import GitHubError
from github_inventory.github.client import GitHubClient
from github_inventory.github.queries import (
    DISCOVERY_QUERY,
    ONE_LEVEL_TREE_QUERY,
    PREFLIGHT_QUERY,
    ROOT_TREE_QUERY,
)
from github_inventory.github.repositories import (
    bounded_inspections,
    discover_repositories,
    inspect_manifest_paths,
    preflight,
)


repositories_module = importlib.import_module(
    "github_inventory.github.repositories"
)


class StubClient:
    """Return predefined GraphQL data without making network requests."""

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


def successful_payload(**values: Any) -> dict[str, Any]:
    return {
        "data": {
            **values,
            "rateLimit": {
                "cost": 1,
                "remaining": 4999,
                "resetAt": "2099-01-01T00:00:00Z",
            },
        }
    }


def test_all_graphql_operations_are_read_only() -> None:
    for query in (
        DISCOVERY_QUERY,
        PREFLIGHT_QUERY,
        ROOT_TREE_QUERY,
        ONE_LEVEL_TREE_QUERY,
    ):
        assert "mutation" not in query.casefold()


def test_preflight_returns_access_and_rate_limit_information() -> None:
    client = StubClient(
        [
            {
                "viewer": {"login": "operator"},
                "organization": {
                    "login": "acme",
                    "viewerCanAdminister": True,
                    "repositories": {"totalCount": 125},
                },
                "rateLimit": {
                    "cost": 1,
                    "limit": 5000,
                    "remaining": 4999,
                    "resetAt": "2099-01-01T00:00:00Z",
                },
            }
        ]
    )

    result = preflight(client, "acme")

    assert result["organization"] == "acme"
    assert result["viewer"] == "operator"
    assert result["visible_repository_count"] == 125
    assert result["graphql_rate_remaining"] == 4999


def test_repository_discovery_paginates_and_reports_progress(
    repository_factory,
) -> None:
    first = repository_factory(name="alpha")
    second = repository_factory(name="beta")
    third = repository_factory(name="gamma")

    client = StubClient(
        [
            {
                "organization": {
                    "repositories": {
                        "totalCount": 3,
                        "nodes": [first, second],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor-1",
                        },
                    }
                }
            },
            {
                "organization": {
                    "repositories": {
                        "totalCount": 3,
                        "nodes": [third],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    }
                }
            },
        ]
    )
    progress: list[tuple[int, int]] = []

    repositories, total = discover_repositories(
        client,
        "acme",
        progress=lambda discovered, expected: progress.append(
            (discovered, expected)
        ),
    )

    assert [item["nameWithOwner"] for item in repositories] == [
        "acme/alpha",
        "acme/beta",
        "acme/gamma",
    ]
    assert total == 3
    assert progress == [(2, 3), (3, 3)]
    assert client.calls[1]["variables"]["cursor"] == "cursor-1"


def test_repository_discovery_rejects_missing_next_cursor(
    repository_factory,
) -> None:
    client = StubClient(
        [
            {
                "organization": {
                    "repositories": {
                        "totalCount": 2,
                        "nodes": [repository_factory(name="alpha")],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": None,
                        },
                    }
                }
            }
        ]
    )

    with pytest.raises(GitHubError) as captured:
        discover_repositories(client, "acme")

    assert captured.value.category == "pagination_error"


def test_one_level_manifest_inspection_collects_nested_files(
    repository_factory,
) -> None:
    repository = repository_factory(name="tree")
    tree = {
        "__typename": "Tree",
        "entries": [
            {
                "name": "README.md",
                "type": "blob",
            },
            {
                "name": "service",
                "type": "tree",
                "object": {
                    "__typename": "Tree",
                    "entries": [
                        {
                            "name": "pyproject.toml",
                            "type": "blob",
                        },
                        {
                            "name": "src",
                            "type": "tree",
                        },
                    ],
                },
            },
        ],
    }
    client = StubClient(
        [
            {
                "repository": {"object": tree},
                "rateLimit": {
                    "cost": 1,
                    "remaining": 4999,
                    "resetAt": "2099-01-01T00:00:00Z",
                },
            }
        ]
    )

    paths = inspect_manifest_paths(client, repository, "one")

    assert "README.md" in paths
    assert "service/pyproject.toml" in paths


def test_root_inspection_does_not_collect_nested_files(
    repository_factory,
) -> None:
    repository = repository_factory(name="tree")
    tree = {
        "__typename": "Tree",
        "entries": [
            {
                "name": "service",
                "type": "tree",
                "object": {
                    "__typename": "Tree",
                    "entries": [
                        {
                            "name": "pyproject.toml",
                            "type": "blob",
                        }
                    ],
                },
            }
        ],
    }
    client = StubClient(
        [
            {
                "repository": {"object": tree},
                "rateLimit": {},
            }
        ]
    )

    paths = inspect_manifest_paths(client, repository, "root")

    assert "service/pyproject.toml" not in paths


def test_bounded_inspections_return_failures_separately(
    monkeypatch,
    repository_factory,
) -> None:
    good = repository_factory(name="good")
    bad = repository_factory(name="bad")

    def fake_inspection(
        _client: object,
        repository: dict[str, Any],
        _depth: str,
    ) -> list[str]:
        if repository["nameWithOwner"].endswith("/bad"):
            raise GitHubError(
                "repository_unavailable",
                "Repository disappeared.",
                attempts=2,
            )

        return ["pyproject.toml"]

    monkeypatch.setattr(
        repositories_module,
        "inspect_manifest_paths",
        fake_inspection,
    )

    results = list(
        bounded_inspections(
            object(),
            [good, bad],
            depth="root",
            workers=2,
        )
    )
    by_name = {
        repository["nameWithOwner"]: (paths, error)
        for repository, paths, error in results
    }

    assert by_name["acme/good"] == (["pyproject.toml"], None)
    assert by_name["acme/bad"][0] is None
    assert isinstance(by_name["acme/bad"][1], GitHubError)


def test_graphql_client_collects_request_and_cost_statistics() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=successful_payload(ok=True),
        )

    client = GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
    )

    try:
        data = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="test operation",
        )
        statistics = client.stats()
    finally:
        client.close()

    assert data["ok"] is True
    assert statistics.requests == 1
    assert statistics.graphql_cost == 1
    assert statistics.retries == 0


def test_graphql_client_retries_server_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            return httpx.Response(503, text="Unavailable")

        return httpx.Response(
            200,
            json=successful_payload(ok=True),
        )

    client = GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        max_attempts=2,
        sleeper=sleeps.append,
        jitter=lambda _minimum, _maximum: 0.0,
    )

    try:
        result = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="retry test",
        )
        statistics = client.stats()
    finally:
        client.close()

    assert result["ok"] is True
    assert attempts == 2
    assert sleeps == [1.0]
    assert statistics.retries == 1


def test_graphql_client_retries_network_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            raise httpx.ConnectError(
                "Connection failed.",
                request=request,
            )

        return httpx.Response(
            200,
            json=successful_payload(ok=True),
        )

    client = GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        max_attempts=2,
        sleeper=sleeps.append,
        jitter=lambda _minimum, _maximum: 0.0,
    )

    try:
        result = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="network retry test",
        )
    finally:
        client.close()

    assert result["ok"] is True
    assert attempts == 2
    assert sleeps == [1.0]


def test_authentication_failure_is_not_retried() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    sleeps: list[float] = []
    client = GitHubClient(
        "invalid-token",
        transport=httpx.MockTransport(handler),
        max_attempts=4,
        sleeper=sleeps.append,
    )

    try:
        with pytest.raises(GitHubError) as captured:
            client.graphql(
                "query Test { viewer { login } }",
                {},
                operation="authentication test",
            )

        statistics = client.stats()
    finally:
        client.close()

    assert captured.value.category == "authentication_failed"
    assert captured.value.attempts == 1
    assert statistics.requests == 1
    assert statistics.retries == 0
    assert sleeps == []


def test_retry_after_header_controls_rate_limit_wait() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            return httpx.Response(
                429,
                text="Rate limited",
                headers={"Retry-After": "0.25"},
            )

        return httpx.Response(
            200,
            json=successful_payload(ok=True),
        )

    client = GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        max_attempts=2,
        sleeper=sleeps.append,
        jitter=lambda _minimum, _maximum: 0.0,
    )

    try:
        result = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="rate-limit test",
        )
    finally:
        client.close()

    assert result["ok"] is True
    assert sleeps == [0.25]


def test_client_waits_before_request_when_primary_limit_is_exhausted() -> None:
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=successful_payload(ok=True),
        )

    client = GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )
    client._update_rate_state(0, time.time() + 0.01)

    try:
        client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="preemptive rate-limit wait",
        )
    finally:
        client.close()

    assert len(sleeps) == 1
    assert sleeps[0] >= 0.9


def test_graphql_error_redacts_token() -> None:
    token = "super-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2099-01-01T00:00:00Z",
                    }
                },
                "errors": [
                    {
                        "type": "FORBIDDEN",
                        "message": f"Rejected credential {token}",
                    }
                ],
            },
        )

    client = GitHubClient(
        token,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(GitHubError) as captured:
            client.graphql(
                "query Test { viewer { login } }",
                {},
                operation="redaction test",
            )
    finally:
        client.close()

    assert token not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)

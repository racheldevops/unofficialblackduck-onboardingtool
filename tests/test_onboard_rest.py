from __future__ import annotations

import json

import httpx
import pytest

from github_onboard.errors import (
    GitHubRestError,
    OnboardError,
)
from github_onboard.github_api import (
    MAX_ASSIGNMENT_BATCH,
    GitHubPropertiesAPI,
)
from github_onboard.rest import GitHubRestClient


def test_rest_client_preserves_configured_base_url() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"login": "operator"},
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://github.example/api/v3",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        response = client.get("/user")

    assert response.json()["login"] == "operator"
    assert requested_urls == [
        "https://github.example/api/v3/user"
    ]


def test_rest_client_blocks_mutation_without_apply() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "Blocked mutation reached the transport."
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            OnboardError,
            match="without --apply",
        ):
            client.mutate(
                "PATCH",
                "/orgs/acme/properties/values",
                body={
                    "repository_names": ["repository"],
                    "properties": [],
                },
            )


def test_rest_client_rejects_unapproved_endpoint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "Unapproved endpoint reached the transport."
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            OnboardError,
            match="not allowlisted",
        ):
            client.mutate(
                "DELETE",
                "/orgs/acme",
                body={},
            )


def test_rest_client_retries_server_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            return httpx.Response(
                503,
                text="Unavailable",
            )

        return httpx.Response(
            200,
            json={"login": "operator"},
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=2,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
        jitter=lambda _minimum, _maximum: 0.0,
    ) as client:
        response = client.get("/user")
        statistics = client.stats()

    assert response.json()["login"] == "operator"
    assert attempts == 2
    assert sleeps == [1.0]
    assert statistics.requests == 2
    assert statistics.retries == 1


def test_rest_network_error_redacts_token() -> None:
    token = "test-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Connection rejected for {token}",
            request=request,
        )

    with GitHubRestClient(
        token,
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(GitHubRestError) as captured:
            client.get("/user")

    assert token not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_property_api_paginates_assignments() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)

        if page == 1:
            values = [
                {
                    "repository_full_name": f"acme/repository-{index}",
                    "properties": [],
                }
                for index in range(100)
            ]
        else:
            values = [
                {
                    "repository_full_name": "acme/repository-final",
                    "properties": [],
                }
            ]

        return httpx.Response(200, json=values)

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubPropertiesAPI(client, "acme")
        assignments = api.list_repository_assignments()

    assert pages == [1, 2]
    assert len(assignments) == 101
    assert "acme/repository-final" in assignments


def test_property_api_uses_allowlisted_writes() -> None:
    received: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            json.loads(request.content)
            if request.content
            else None
        )
        received.append(
            (
                request.method,
                request.url.path,
                body,
            )
        )

        if request.method == "PUT":
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "property_name": name,
                    **body,
                },
            )

        return httpx.Response(204)

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubPropertiesAPI(client, "acme")
        definition = api.put_definition(
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
            }
        )
        api.set_repository_values(
            ["acme/repository"],
            {
                "blackduck_activity": "active",
            },
        )

    assert definition["property_name"] == (
        "blackduck_activity"
    )
    assert received[0][0:2] == (
        "PUT",
        (
            "/orgs/acme/properties/schema/"
            "blackduck_activity"
        ),
    )
    assert received[1][0:2] == (
        "PATCH",
        "/orgs/acme/properties/values",
    )
    assert received[1][2] == {
        "repository_names": ["repository"],
        "properties": [
            {
                "property_name": "blackduck_activity",
                "value": "active",
            }
        ],
    }


def test_property_api_rejects_oversized_batch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "Oversized batch reached the transport."
        )

    repositories = [
        f"acme/repository-{index}"
        for index in range(MAX_ASSIGNMENT_BATCH + 1)
    ]

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubPropertiesAPI(client, "acme")

        with pytest.raises(
            OnboardError,
            match="exceeds",
        ):
            api.set_repository_values(
                repositories,
                {
                    "blackduck_activity": "active",
                },
            )

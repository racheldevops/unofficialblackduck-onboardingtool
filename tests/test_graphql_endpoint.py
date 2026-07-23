from __future__ import annotations

import httpx
import pytest

from github_inventory.github.client import GitHubClient


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.github.com/graphql",
        "https://github.customer.example/api/graphql",
    ],
)
def test_graphql_client_preserves_exact_endpoint(endpoint: str) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "login": "operator",
                    }
                }
            },
        )

    client = GitHubClient(
        "test-token",
        endpoint=endpoint,
        transport=httpx.MockTransport(handler),
    )

    try:
        result = client.graphql(
            "query Test { viewer { login } }",
            {},
            operation="endpoint regression test",
        )
    finally:
        client.close()

    assert result["viewer"]["login"] == "operator"
    assert requested_urls == [endpoint]

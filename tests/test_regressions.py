from __future__ import annotations

import json

import httpx
import pytest

from github_inventory.errors import GitHubError
from github_inventory.github.client import GitHubClient
from github_inventory.reporting import emit_event


def test_emit_event_handles_record_type_field(capsys) -> None:
    emit_event(
        "run_summary",
        record_type="summary",
        aborted=True,
    )

    event = json.loads(capsys.readouterr().err)

    assert event["record_type"] == "run_summary"
    assert event["aborted"] is True


def test_network_error_contains_sanitized_transport_detail() -> None:
    token = "test-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "TLS verification failed",
            request=request,
        )

    client = GitHubClient(
        token,
        transport=httpx.MockTransport(handler),
        max_attempts=1,
    )

    try:
        with pytest.raises(GitHubError) as captured:
            client.graphql(
                "query Test { viewer { login } }",
                {},
                operation="authentication preflight",
            )
    finally:
        client.close()

    message = str(captured.value)

    assert captured.value.category == "network_error"
    assert "ConnectError" in message
    assert "TLS verification failed" in message
    assert token not in message

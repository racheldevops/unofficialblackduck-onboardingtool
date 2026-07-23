from __future__ import annotations

import ast
import importlib
import inspect
import json
from typing import Any

import pytest

from github_inventory.cli import build_parser, main
from github_inventory.github.client import GitHubClient


client_module = importlib.import_module(
    "github_inventory.github.client"
)
cli_module = importlib.import_module(
    "github_inventory.cli"
)


def test_parser_configures_insecure_tls_explicitly() -> None:
    defaults = build_parser().parse_args([])
    insecure = build_parser().parse_args(["--insecure"])

    assert defaults.insecure is False
    assert insecure.insecure is True


@pytest.mark.parametrize(
    ("client_options", "expected"),
    [
        ({}, True),
        ({"verify": False}, False),
    ],
)
def test_client_configures_tls_verification(
    monkeypatch,
    client_options: dict[str, bool],
    expected: bool,
) -> None:
    received: dict[str, Any] = {}

    class StubHttpClient:
        def __init__(self, **options: Any) -> None:
            received.update(options)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        client_module.httpx,
        "Client",
        StubHttpClient,
    )

    client = GitHubClient(
        "test-token",
        **client_options,
    )
    client.close()

    assert received["verify"] is expected


def test_insecure_preflight_emits_warning_without_network_access(
    monkeypatch,
    capsys,
) -> None:
    received: dict[str, Any] = {}

    def fake_run_preflight(
        arguments: Any,
        organization: str,
        token: str,
    ) -> int:
        received["arguments"] = arguments
        received["organization"] = organization
        received["token"] = token
        return 17

    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        cli_module,
        "run_preflight",
        fake_run_preflight,
    )

    result = main(["--preflight", "--insecure"])
    event = json.loads(capsys.readouterr().err)

    assert result == 17
    assert received["organization"] == "acme"
    assert received["token"] == "test-token"
    assert received["arguments"].insecure is True
    assert event == {
        "record_type": "insecure_tls_warning",
        "message": (
            "TLS certificate verification is disabled. GitHub server "
            "identity cannot be verified, and GITHUB_TOKEN could be exposed."
        ),
    }


@pytest.mark.parametrize(
    "module_name",
    [
        "github_inventory.workflows.preflight",
        "github_inventory.workflows.benchmark",
        "github_inventory.workflows.inventory",
    ],
)
def test_workflow_passes_insecure_selection_to_client(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GitHubClient"
    ]

    assert len(calls) == 1

    keywords = {
        keyword.arg: keyword.value
        for keyword in calls[0].keywords
        if keyword.arg is not None
    }
    expected = ast.parse(
        'not getattr(args, "insecure", False)',
        mode="eval",
    ).body

    assert "verify" in keywords
    assert ast.dump(
        keywords["verify"],
        include_attributes=False,
    ) == ast.dump(
        expected,
        include_attributes=False,
    )

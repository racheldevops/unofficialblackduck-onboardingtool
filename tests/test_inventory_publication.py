from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

import pytest

from github_inventory.errors import GitHubError
from github_inventory.models import ClientStats


inventory_module = importlib.import_module(
    "github_inventory.workflows.inventory"
)


@pytest.mark.parametrize("preexisting", [False, True])
def test_fatal_preflight_does_not_publish_final_artifacts(
    monkeypatch,
    capsys,
    tmp_path,
    preexisting: bool,
) -> None:
    class StubClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def close(self) -> None:
            pass

        def stats(self) -> ClientStats:
            return ClientStats(
                requests=1,
                graphql_cost=0,
                retries=0,
            )

    def failed_preflight(*_args, **_kwargs):
        raise GitHubError(
            "not_found",
            "GitHub could not find the resource for authentication preflight.",
            attempts=1,
        )

    monkeypatch.setattr(
        inventory_module,
        "GitHubClient",
        StubClient,
    )
    monkeypatch.setattr(
        inventory_module,
        "preflight",
        failed_preflight,
    )

    output = tmp_path / "full"
    paths = {
        "inventory.jsonl": (
            '{"name_with_owner":"acme/existing",'
            '"repository_id":"existing"}\n'
        ),
        "failures.jsonl": (
            '{"name_with_owner":"acme/failed",'
            '"record_type":"failure"}\n'
        ),
        "summary.jsonl": (
            '{"record_type":"summary",'
            '"reconciliation_ok":true}\n'
        ),
    }

    if preexisting:
        output.mkdir()
        for name, content in paths.items():
            (output / name).write_text(
                content,
                encoding="utf-8",
            )

    arguments = argparse.Namespace(
        max_hours=2.0,
        output_dir=output,
        dry_run=False,
        discard_checkpoint=False,
        resume=False,
        timeout=30.0,
        retries=4,
        inspection_depth="root",
        limit=None,
        insecure=False,
    )

    result = inventory_module.run_inventory(
        arguments,
        "acme",
        "test-token",
    )

    events = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
    ]

    assert result == 2
    assert events[-1]["record_type"] == "run_summary"
    assert events[-1]["aborted"] is True
    assert events[-1]["discovered_repository_count"] == 0
    assert events[-1]["fatal_error"] == (
        "GitHub could not find the resource for "
        "authentication preflight."
    )

    for name, content in paths.items():
        path = output / name

        if preexisting:
            assert path.read_text(encoding="utf-8") == content
        else:
            assert not path.exists()

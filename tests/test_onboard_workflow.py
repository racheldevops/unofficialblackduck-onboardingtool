from __future__ import annotations

import base64
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_onboard.cli import build_parser
from github_onboard.config import (
    initialize_config,
    load_config,
)
from github_onboard.errors import OnboardError
from github_onboard.rest import GitHubRestClient
from github_onboard.workflow import (
    git_blob_sha,
    load_workflow_source,
    run_workflow,
)
from github_onboard.workflow_api import GitHubWorkflowAPI
from github_onboard.workspace import Workspace


PINNED_ACTION = (
    "blackduck-inc/black-duck-security-scan@"
    "152247222aa9cd38124acd5c0cf60f4db71adc3f"
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def enabled_config(tmp_path: Path):
    source_root = Path(__file__).resolve().parents[1]
    configuration_directory = tmp_path / "config"
    workflow_directory = (
        configuration_directory / "workflows"
    )
    workflow_directory.mkdir(parents=True)
    workflow_source = (
        source_root
        / "config"
        / "workflows"
        / "blackduck-required.yml"
    ).read_text(encoding="utf-8")
    workflow_source = workflow_source.replace(
        "BD-Test-Organisation/blackduck-workflows",
        "acme/blackduck-workflows",
    )
    (
        workflow_directory / "blackduck-required.yml"
    ).write_text(
        workflow_source,
        encoding="utf-8",
    )

    configuration_path = (
        configuration_directory / "onboarding.toml"
    )
    initialize_config(configuration_path, "acme")
    text = configuration_path.read_text(
        encoding="utf-8"
    )
    workflow_section = """[workflow]
enabled = true
source_repository = "acme/blackduck-workflows"
local_path = "workflows/blackduck-required.yml"
path = ".github/workflows/blackduck-required.yml"
branch = "main"
url_variable_name = "BLACKDUCK_URL"
secret_name = "BLACKDUCK_API_TOKEN"
runner = "ubuntu-latest"
timeout_minutes = 30
action_repository = "blackduck-inc/black-duck-security-scan"
action_commit_sha = "152247222aa9cd38124acd5c0cf60f4db71adc3f"
"""
    pattern = re.compile(
        r"(?ms)^\[workflow\]\n.*?(?=^\[[^\n]+\]\n|\Z)"
    )
    text, replacements = pattern.subn(
        workflow_section + "\n",
        text,
        count=1,
    )

    if replacements != 1:
        raise AssertionError(
            "Unable to configure the workflow test."
        )

    configuration_path.write_text(
        text,
        encoding="utf-8",
    )
    return load_config(configuration_path)


def repository_response(
    *,
    size: int,
) -> dict[str, Any]:
    return {
        "id": 17,
        "node_id": "R_workflows",
        "full_name": "acme/blackduck-workflows",
        "default_branch": "main",
        "visibility": "public",
        "size": size,
        "permissions": {
            "admin": True,
            "push": True,
            "pull": True,
        },
    }


def file_response(
    content: bytes,
) -> dict[str, Any]:
    return {
        "type": "file",
        "path": (
            ".github/workflows/"
            "blackduck-required.yml"
        ),
        "sha": git_blob_sha(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(
            "ascii"
        ),
        "html_url": (
            "https://github.com/acme/"
            "blackduck-workflows/blob/main/"
            ".github/workflows/"
            "blackduck-required.yml"
        ),
    }


def test_workflow_config_resolves_persistent_source(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    source_path, content = load_workflow_source(config)

    assert source_path == (
        tmp_path
        / "config"
        / "workflows"
        / "blackduck-required.yml"
    )
    assert content
    assert PINNED_ACTION.encode("utf-8") in content
    assert len(git_blob_sha(content)) == 40


def test_workflow_source_uses_only_full_action_shas(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    _source_path, content = load_workflow_source(config)
    text = content.decode("utf-8")
    uses = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("uses:")
    ]

    assert len(uses) == 2

    for line in uses:
        assert re.fullmatch(
            r"uses:\s+[^@\s]+@[0-9a-f]{40}",
            line,
        )

    assert "@main" not in text
    assert "@master" not in text
    assert "@v2" not in text
    assert "@v4" not in text


def test_workflow_source_has_safe_execution_contract(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    _source_path, content = load_workflow_source(config)
    text = content.decode("utf-8")

    assert "permissions:\n  contents: read" in text
    assert "timeout-minutes: 30" in text
    assert "cancel-in-progress: true" in text
    assert (
        "github.event.pull_request.head.repo.full_name "
        "== github.repository"
    ) in text
    assert (
        "github.repository != "
        "'acme/blackduck-workflows'"
    ) in text
    assert (
        "${{ secrets.BLACKDUCK_API_TOKEN }}"
        in text
    )
    assert "BLACKDUCK_API_TOKEN=" not in text


def test_workflow_parser_is_read_only_by_default() -> None:
    dry_run = build_parser().parse_args(["workflow"])
    apply = build_parser().parse_args(
        ["workflow", "--apply"]
    )

    assert dry_run.command == "workflow"
    assert dry_run.apply is False
    assert apply.apply is True


def test_workflow_api_update_sends_expected_sha() -> None:
    received: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["path"] = request.url.path
        received["body"] = json.loads(
            request.content
        )
        content = base64.b64decode(
            received["body"]["content"]
        )
        return httpx.Response(
            200,
            json={
                "content": {
                    "sha": git_blob_sha(content),
                },
                "commit": {
                    "sha": "c" * 40,
                    "html_url": (
                        "https://github.com/acme/"
                        "blackduck-workflows/commit/"
                        + "c" * 40
                    ),
                },
            },
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubWorkflowAPI(
            client,
            "acme/blackduck-workflows",
        )
        result = api.put_file(
            ".github/workflows/blackduck-required.yml",
            branch="main",
            message="Update workflow",
            content=b"name: workflow\n",
            current_sha="a" * 40,
        )

    assert received["method"] == "PUT"
    assert received["path"] == (
        "/repos/acme/blackduck-workflows/contents/"
        ".github/workflows/blackduck-required.yml"
    )
    assert received["body"]["branch"] == "main"
    assert received["body"]["sha"] == "a" * 40
    assert result.commit_sha == "c" * 40


def test_workflow_api_is_blocked_without_apply() -> None:
    def handler(
        _request: httpx.Request,
    ) -> httpx.Response:
        raise AssertionError(
            "Blocked workflow mutation reached transport."
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubWorkflowAPI(
            client,
            "acme/blackduck-workflows",
        )

        with pytest.raises(
            OnboardError,
            match="without --apply",
        ):
            api.put_file(
                ".github/workflows/"
                "blackduck-required.yml",
                branch="main",
                message="Create workflow",
                content=b"name: workflow\n",
                current_sha=None,
            )


def test_workflow_dry_run_plans_empty_repository_create(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)

        if request.url.path == (
            "/repos/acme/blackduck-workflows"
        ):
            return httpx.Response(
                200,
                json=repository_response(size=0),
            )

        if "/contents/" in request.url.path:
            return httpx.Response(
                404,
                json={"message": "Not Found"},
            )

        raise AssertionError(
            f"Unexpected request: {request.method} "
            f"{request.url}"
        )

    result, output = run_workflow(
        config,
        workspace,
        "test-token",
        apply=False,
        insecure=False,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert methods == ["GET", "GET"]
    plan = read_jsonl(
        output / "workflow-plan.jsonl"
    )
    summary = read_jsonl(
        output / "workflow-summary.jsonl"
    )[0]
    file_plan = next(
        record
        for record in plan
        if record.get("record_type")
        == "workflow_file_plan"
    )

    assert file_plan["action"] == "create"
    assert file_plan["current"] is None
    assert summary["mutation_occurred"] is False
    assert not (
        output / "workflow-apply.jsonl"
    ).exists()


def test_workflow_apply_populates_empty_repository(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    _path, desired = load_workflow_source(config)
    remote_content: bytes | None = None
    received_put: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_content
        path = request.url.path

        if path == "/repos/acme/blackduck-workflows":
            return httpx.Response(
                200,
                json=repository_response(
                    size=0 if remote_content is None else 1
                ),
            )

        if (
            request.method == "GET"
            and "/contents/" in path
        ):
            if remote_content is None:
                return httpx.Response(
                    404,
                    json={"message": "Not Found"},
                )

            return httpx.Response(
                200,
                json=file_response(remote_content),
            )

        if (
            request.method == "PUT"
            and "/contents/" in path
        ):
            body = json.loads(request.content)
            received_put.update(body)
            remote_content = base64.b64decode(
                body["content"]
            )
            return httpx.Response(
                201,
                json={
                    "content": {
                        "sha": git_blob_sha(
                            remote_content
                        ),
                    },
                    "commit": {
                        "sha": "d" * 40,
                        "html_url": (
                            "https://github.com/acme/"
                            "blackduck-workflows/commit/"
                            + "d" * 40
                        ),
                    },
                },
            )

        raise AssertionError(
            f"Unexpected request: {request.method} "
            f"{request.url}"
        )

    result, output = run_workflow(
        config,
        workspace,
        "test-token",
        apply=True,
        insecure=False,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert remote_content == desired
    assert "branch" not in received_put
    assert "sha" not in received_put

    apply_records = read_jsonl(
        output / "workflow-apply.jsonl"
    )
    verification = read_jsonl(
        output / "workflow-verification.jsonl"
    )
    rollback = read_jsonl(
        output / "workflow-rollback-plan.jsonl"
    )
    summary = read_jsonl(
        output / "workflow-summary.jsonl"
    )[0]

    assert apply_records[0]["action"] == "create"
    assert apply_records[0]["result"] == "applied"
    assert verification[0]["result"] == "verified"
    assert rollback[1]["original_action"] == "create"
    assert (
        rollback[1]["automatic_delete_supported"]
        is False
    )
    assert summary["mutation_occurred"] is True
    assert summary["verification"] == "verified"


def test_workflow_apply_is_no_change_when_remote_matches(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    _path, desired = load_workflow_source(config)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)

        if request.url.path == (
            "/repos/acme/blackduck-workflows"
        ):
            return httpx.Response(
                200,
                json=repository_response(size=1),
            )

        if "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json=file_response(desired),
            )

        raise AssertionError(
            f"Unexpected request: {request.method} "
            f"{request.url}"
        )

    result, output = run_workflow(
        config,
        workspace,
        "test-token",
        apply=True,
        insecure=False,
        transport=httpx.MockTransport(handler),
    )

    assert result == 0
    assert set(methods) == {"GET"}
    summary = read_jsonl(
        output / "workflow-summary.jsonl"
    )[0]
    verification = read_jsonl(
        output / "workflow-verification.jsonl"
    )

    assert summary["action"] == "no_change"
    assert summary["mutation_occurred"] is False
    assert verification[0]["result"] == "verified"


def test_workflow_apply_stops_on_concurrent_change(
    tmp_path: Path,
) -> None:
    config = enabled_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    initial = b"name: old\n"
    concurrent = b"name: changed\n"
    file_reads = 0
    mutation_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal file_reads
        path = request.url.path

        if request.method != "GET":
            mutation_methods.append(request.method)

        if path == "/repos/acme/blackduck-workflows":
            return httpx.Response(
                200,
                json=repository_response(size=1),
            )

        if (
            request.method == "GET"
            and "/contents/" in path
        ):
            file_reads += 1
            content = (
                initial
                if file_reads == 1
                else concurrent
            )
            return httpx.Response(
                200,
                json=file_response(content),
            )

        raise AssertionError(
            f"Unexpected request: {request.method} "
            f"{request.url}"
        )

    with pytest.raises(
        ValueError,
        match="state changed",
    ):
        run_workflow(
            config,
            workspace,
            "test-token",
            apply=True,
            insecure=False,
            transport=httpx.MockTransport(handler),
        )

    assert mutation_methods == []

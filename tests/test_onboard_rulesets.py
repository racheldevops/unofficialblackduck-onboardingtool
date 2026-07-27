from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

import github_onboard.cli as cli_module
from github_onboard.cli import (
    build_parser,
    run_rulesets_command,
)
from github_onboard.config import initialize_config, load_config
from github_onboard.errors import OnboardError
from github_onboard.rest import GitHubRestClient
from github_onboard.ruleset_api import (
    GitHubRulesetsAPI,
    _validate_desired_ruleset,
)
from github_onboard.rulesets import (
    desired_ruleset,
    run_rulesets,
)
from github_onboard.workflow import (
    git_blob_sha,
    load_workflow_source,
)
from github_onboard.workspace import Workspace


def make_config(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    config_directory = tmp_path / "config"
    workflow_directory = (
        config_directory / "workflows"
    )
    workflow_directory.mkdir(parents=True)
    workflow = (
        root
        / "config/workflows/blackduck-required.yml"
    ).read_text(encoding="utf-8").replace(
        "BD-Test-Organisation/blackduck-workflows",
        "acme/blackduck-workflows",
    )
    (
        workflow_directory / "blackduck-required.yml"
    ).write_text(workflow, encoding="utf-8")
    config_path = (
        config_directory / "onboarding.toml"
    )
    initialize_config(config_path, "acme")
    text = config_path.read_text(encoding="utf-8")
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
    ruleset_section = """[ruleset]
enabled = true
name = "Black Duck SCA Required"
enforcement = "evaluate"
include_policy_value = "required"
"""
    text = re.sub(
        r"(?ms)^\[workflow\]\n.*?(?=^\[[^\n]+\]\n|\Z)",
        workflow_section + "\n",
        text,
        count=1,
    )
    text = re.sub(
        r"(?ms)^\[ruleset\]\n.*?(?=^\[[^\n]+\]\n|\Z)",
        ruleset_section,
        text,
        count=1,
    )
    config_path.write_text(text, encoding="utf-8")
    return load_config(config_path)


class RulesetServer:

    def __init__(
        self,
        config,
        *,
        ruleset: dict[str, Any] | None = None,
        targets: list[str] | None = None,
    ) -> None:
        _path, self.workflow = load_workflow_source(
            config
        )
        self.config = config
        self.ruleset = ruleset
        self.targets = targets or ["acme/target"]
        self.methods: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.property_reads = 0
        self.drift_targets: list[str] | None = None

    def assignments(
        self,
        targets: list[str],
    ) -> list[dict[str, Any]]:
        result = [
            {
                "repository_id": 100 + index,
                "repository_full_name": name,
                "properties": [
                    {
                        "property_name": (
                            "blackduck_sca_policy"
                        ),
                        "value": "required",
                    }
                ],
            }
            for index, name in enumerate(targets)
        ]
        if not any(
            name.casefold()
            == "acme/blackduck-workflows"
            for name in targets
        ):
            result.append(
                {
                    "repository_id": 17,
                    "repository_full_name": (
                        "acme/blackduck-workflows"
                    ),
                    "properties": [
                        {
                            "property_name": (
                                "blackduck_sca_policy"
                            ),
                            "value": "excluded",
                        }
                    ],
                }
            )

        return result

    def handler(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.methods.append(request.method)
        path = request.url.path

        if path == "/repos/acme/blackduck-workflows":
            return httpx.Response(
                200,
                json={
                    "id": 17,
                    "node_id": "R_workflows",
                    "full_name": (
                        "acme/blackduck-workflows"
                    ),
                    "default_branch": "main",
                    "visibility": "public",
                    "size": 1,
                    "permissions": {
                        "admin": True,
                        "push": True,
                        "pull": True,
                    },
                },
            )

        if "/contents/" in path:
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": (
                        ".github/workflows/"
                        "blackduck-required.yml"
                    ),
                    "sha": git_blob_sha(self.workflow),
                    "encoding": "base64",
                    "content": base64.b64encode(
                        self.workflow
                    ).decode("ascii"),
                    "html_url": "https://example/workflow",
                },
            )

        if path == "/orgs/acme/properties/schema":
            return httpx.Response(
                200,
                json=[
                    {
                        "property_name": (
                            "blackduck_sca_policy"
                        ),
                        "value_type": "single_select",
                        "allowed_values": [
                            "excluded",
                            "required",
                            "review",
                        ],
                    }
                ],
            )

        if path == "/orgs/acme/properties/values":
            self.property_reads += 1
            selected = (
                self.drift_targets
                if self.property_reads > 1
                and self.drift_targets is not None
                else self.targets
            )
            return httpx.Response(
                200,
                json=self.assignments(selected),
            )

        if path == "/orgs/acme/rulesets":
            if request.method == "GET":
                values = []

                if self.ruleset is not None:
                    values.append(
                        {
                            "id": self.ruleset["id"],
                            "name": self.ruleset["name"],
                            "target": self.ruleset["target"],
                            "enforcement": self.ruleset[
                                "enforcement"
                            ],
                        }
                    )

                return httpx.Response(200, json=values)

            body = json.loads(request.content)
            self.bodies.append(body)
            self.ruleset = {
                "id": 41,
                **body,
            }
            return httpx.Response(
                201,
                json=self.ruleset,
            )

        if path == "/orgs/acme/rulesets/41":
            if request.method == "GET":
                if self.ruleset is None:
                    raise AssertionError(
                        "Ruleset detail requested before creation."
                    )

                return httpx.Response(
                    200,
                    json=self.ruleset,
                )

            body = json.loads(request.content)
            self.bodies.append(body)
            self.ruleset = {
                "id": 41,
                **body,
            }
            return httpx.Response(
                200,
                json=self.ruleset,
            )

        raise AssertionError(
            f"Unexpected request: {request.method} {request.url}"
        )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def test_ruleset_cli_supports_insecure_and_activation() -> None:
    configure = build_parser().parse_args(
        ["rulesets", "--insecure"]
    )
    activate = build_parser().parse_args(
        [
            "rulesets",
            "activate",
            "--apply",
            "--insecure",
        ]
    )

    assert configure.apply is False
    assert configure.insecure is True
    assert configure.ruleset_operation is None
    assert activate.apply is True
    assert activate.insecure is True
    assert activate.ruleset_operation == "activate"


def test_ruleset_command_forwards_insecure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    config = make_config(tmp_path)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )
    received: dict[str, Any] = {}

    def fake_run_rulesets(
        selected_config,
        selected_workspace,
        token,
        *,
        apply,
        activate,
        insecure,
    ):
        received.update(
            {
                "config": selected_config,
                "workspace": selected_workspace,
                "token": token,
                "apply": apply,
                "activate": activate,
                "insecure": insecure,
            }
        )
        return 0, tmp_path / "output"

    monkeypatch.setattr(
        cli_module,
        "selected_configuration",
        lambda _args: (workspace, config),
    )
    monkeypatch.setattr(
        cli_module,
        "required_token",
        lambda _parser: "test-token",
    )
    monkeypatch.setattr(
        cli_module,
        "run_rulesets",
        fake_run_rulesets,
    )
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "rulesets",
            "activate",
            "--apply",
            "--insecure",
        ]
    )

    result = run_rulesets_command(
        parser,
        arguments,
    )

    assert result == 0
    assert received["apply"] is True
    assert received["activate"] is True
    assert received["insecure"] is True
    assert received["token"] == "test-token"
    assert "insecure_tls_warning" in (
        capsys.readouterr().err
    )


def test_desired_ruleset_has_only_approved_rule(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    desired = desired_ruleset(
        config,
        17,
        enforcement="evaluate",
    )

    assert _validate_desired_ruleset(
        desired
    ) == desired
    assert desired["bypass_actors"] == []
    assert desired["conditions"]["ref_name"] == {
        "include": ["~DEFAULT_BRANCH"],
        "exclude": [],
    }
    assert desired["conditions"][
        "repository_property"
    ] == {
        "include": [
            {
                "name": "blackduck_sca_policy",
                "property_values": ["required"],
            }
        ],
        "exclude": [],
    }
    assert len(desired["rules"]) == 1
    assert desired["rules"][0]["type"] == (
        "workflows"
    )
    assert desired["rules"][0]["parameters"][
        "workflows"
    ] == [
        {
            "path": (
                ".github/workflows/"
                "blackduck-required.yml"
            ),
            "ref": "refs/heads/main",
            "repository_id": 17,
        }
    ]


def test_ruleset_api_rejects_bypass_actors(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    desired = desired_ruleset(
        config,
        17,
        enforcement="evaluate",
    )
    desired["bypass_actors"] = [
        {
            "actor_id": 1,
            "actor_type": "Team",
            "bypass_mode": "always",
        }
    ]

    def handler(
        _request: httpx.Request,
    ) -> httpx.Response:
        raise AssertionError(
            "Invalid ruleset reached transport."
        )

    with GitHubRestClient(
        "test-token",
        base_url="https://api.github.com",
        timeout=30.0,
        max_attempts=1,
        mutation_enabled=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubRulesetsAPI(client, "acme")

        with pytest.raises(
            OnboardError,
            match="bypass",
        ):
            api.create_ruleset(desired)


def test_ruleset_dry_run_previews_required_targets(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(config)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )

    result, output = run_rulesets(
        config,
        workspace,
        "test-token",
        apply=False,
        activate=False,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 0
    assert set(server.methods) == {"GET"}

    plan = read_jsonl(
        output / "ruleset-plan.jsonl"
    )
    ruleset_plan = next(
        item
        for item in plan
        if item["record_type"] == "ruleset_plan"
    )
    preview = next(
        item
        for item in plan
        if item["record_type"]
        == "ruleset_target_preview"
    )

    assert ruleset_plan["action"] == "create"
    assert ruleset_plan["desired"]["enforcement"] == (
        "evaluate"
    )
    assert preview["repository_count"] == 1
    assert [
        item["name_with_owner"]
        for item in preview["repositories"]
    ] == ["acme/target"]
    assert not (
        output / "ruleset-apply.jsonl"
    ).exists()


def test_ruleset_apply_creates_and_verifies_evaluate_mode(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(config)
    workspace = Workspace.from_root(
        tmp_path / ".inventory"
    )

    result, output = run_rulesets(
        config,
        workspace,
        "test-token",
        apply=True,
        activate=False,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 0
    assert server.methods.count("POST") == 1
    assert server.methods.count("PUT") == 0
    assert server.ruleset is not None
    assert server.ruleset["enforcement"] == "evaluate"

    verification = read_jsonl(
        output / "ruleset-verification.jsonl"
    )
    rollback = read_jsonl(
        output / "ruleset-rollback-plan.jsonl"
    )
    summary = read_jsonl(
        output / "ruleset-summary.jsonl"
    )[0]

    assert verification[0]["result"] == "verified"
    assert rollback[1]["previous"] is None
    assert rollback[1][
        "new_ruleset_rollback"
    ] == "disable"
    assert summary["mutation_occurred"] is True


def test_activation_requires_existing_ruleset(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(config)

    result, output = run_rulesets(
        config,
        Workspace.from_root(
            tmp_path / ".inventory"
        ),
        "test-token",
        apply=False,
        activate=True,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 2
    summary = read_jsonl(
        output / "ruleset-summary.jsonl"
    )[0]
    assert summary["action"] == "blocked"
    assert summary["reason"] == (
        "ruleset_must_exist_before_activation"
    )


def test_activation_updates_only_enforcement(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    evaluated = {
        "id": 41,
        **desired_ruleset(
            config,
            17,
            enforcement="evaluate",
        ),
    }
    server = RulesetServer(
        config,
        ruleset=evaluated,
    )

    result, output = run_rulesets(
        config,
        Workspace.from_root(
            tmp_path / ".inventory"
        ),
        "test-token",
        apply=True,
        activate=True,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 0
    assert server.methods.count("POST") == 0
    assert server.methods.count("PUT") == 1
    assert server.ruleset is not None
    assert server.ruleset["enforcement"] == "active"

    updated = server.bodies[0]
    expected = desired_ruleset(
        config,
        17,
        enforcement="active",
    )

    assert updated == expected
    assert read_jsonl(
        output / "ruleset-verification.jsonl"
    )[0]["result"] == "verified"


def test_empty_target_preview_blocks_apply(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(
        config,
        targets=[],
    )
    server.targets = []

    result, output = run_rulesets(
        config,
        Workspace.from_root(
            tmp_path / ".inventory"
        ),
        "test-token",
        apply=True,
        activate=False,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 2
    assert "POST" not in server.methods
    assert "PUT" not in server.methods
    summary = read_jsonl(
        output / "ruleset-summary.jsonl"
    )[0]
    assert "target_preview_empty" in summary["reason"]


def test_workflow_repository_cannot_be_targeted(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(
        config,
        targets=[
            "acme/target",
            "acme/blackduck-workflows",
        ],
    )

    result, output = run_rulesets(
        config,
        Workspace.from_root(
            tmp_path / ".inventory"
        ),
        "test-token",
        apply=False,
        activate=False,
        insecure=False,
        transport=httpx.MockTransport(server.handler),
    )

    assert result == 2
    summary = read_jsonl(
        output / "ruleset-summary.jsonl"
    )[0]
    assert (
        "workflow_repository_is_targeted"
        in summary["reason"]
    )


def test_target_drift_stops_before_mutation(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    server = RulesetServer(config)
    server.drift_targets = ["acme/other"]

    with pytest.raises(
        OnboardError,
        match="state changed",
    ):
        run_rulesets(
            config,
            Workspace.from_root(
                tmp_path / ".inventory"
            ),
            "test-token",
            apply=True,
            activate=False,
            insecure=False,
            transport=httpx.MockTransport(server.handler),
        )

    assert "POST" not in server.methods
    assert "PUT" not in server.methods

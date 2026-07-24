from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path
from typing import Any

from .models import OnboardingConfig
from .rest import GitHubRestClient
from .workflow_api import (
    GitHubWorkflowAPI,
    RepositoryState,
    WorkflowFileState,
)
from .workspace import (
    Workspace,
    atomic_write_json,
    atomic_write_jsonl,
    sha256_bytes,
    utc_now_text,
)


ACTION_REFERENCE = re.compile(
    r"^\s*uses:\s+[^@\s]+@([0-9a-f]{40})\s*$"
)


def git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(
        header + content,
        usedforsecurity=False,
    ).hexdigest()


def load_workflow_source(
    config: OnboardingConfig,
) -> tuple[Path, bytes]:
    if not config.workflow.enabled:
        raise ValueError(
            "Workflow deployment is disabled in configuration."
        )

    path = (
        config.source_path.parent
        / config.workflow.local_path
    )

    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"Unable to read workflow source: {path}"
        ) from error

    if not content:
        raise ValueError("Workflow source is empty.")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "Workflow source must be UTF-8."
        ) from error

    expected_action = (
        f"uses: {config.workflow.action_repository}@"
        f"{config.workflow.action_commit_sha}"
    )
    expected_url = (
        "blackducksca_url: "
        f"${{{{ vars.{config.workflow.url_variable_name} }}}}"
    )
    expected_secret = (
        "blackducksca_token: "
        f"${{{{ secrets.{config.workflow.secret_name} }}}}"
    )

    required = (
        "name: Black Duck SCA",
        "permissions:\n  contents: read",
        f"runs-on: {config.workflow.runner}",
        (
            f"timeout-minutes: "
            f"{config.workflow.timeout_minutes}"
        ),
        expected_action,
        expected_url,
        expected_secret,
        "blackducksca_scan_full: true",
        "blackducksca_waitForScan: true",
        "cancel-in-progress: true",
        (
            "github.event.pull_request.head.repo.full_name "
            "== github.repository"
        ),
        (
            "github.repository != "
            f"'{config.workflow.source_repository}'"
        ),
    )

    for value in required:
        if value not in text:
            raise ValueError(
                f"Workflow source is missing required content: {value}"
            )

    references = [
        match.group(1)
        for line in text.splitlines()
        if (match := ACTION_REFERENCE.fullmatch(line))
        is not None
    ]

    uses_count = sum(
        1
        for line in text.splitlines()
        if line.strip().startswith("uses:")
    )

    if len(references) != uses_count or uses_count < 2:
        raise ValueError(
            "Every workflow action must use a full commit SHA."
        )

    if text.count(expected_secret) != 1:
        raise ValueError(
            "Black Duck token must have exactly one action input."
        )

    if not text.endswith("\n"):
        raise ValueError(
            "Workflow source must end with a newline."
        )

    return path, content


def _file_state(
    value: WorkflowFileState | None,
) -> dict[str, Any] | None:
    if value is None:
        return None

    return {
        "path": value.path,
        "github_sha": value.sha,
        "sha256": sha256_bytes(value.content),
        "git_blob_sha": git_blob_sha(value.content),
        "size": len(value.content),
        "html_url": value.html_url,
    }


def _repository_record(
    state: RepositoryState,
) -> dict[str, Any]:
    return {
        "record_type": "workflow_repository_state",
        "repository_id": state.repository_id,
        "node_id": state.node_id,
        "full_name": state.full_name,
        "default_branch": state.default_branch,
        "visibility": state.visibility,
        "empty": state.empty,
        "can_push": state.can_push,
    }


def _plan_action(
    repository: RepositoryState,
    current: WorkflowFileState | None,
    content: bytes,
    config: OnboardingConfig,
) -> tuple[str, str]:
    if not repository.can_push:
        return "blocked", "workflow_repository_not_writable"

    if (
        repository.default_branch
        != config.workflow.branch
    ):
        return "conflict", "default_branch_mismatch"

    if current is None:
        return "create", "workflow_file_missing"

    if current.content == content:
        return "no_change", "workflow_content_matches"

    return "update", "workflow_content_differs"


def _latest(
    workspace: Workspace,
    *,
    run_id: str,
    run_directory: Path,
    mode: str,
    result: int,
    mutation_occurred: bool,
) -> None:
    atomic_write_json(
        workspace.workflow_directory / "latest.json",
        {
            "run_id": run_id,
            "stage": "workflow",
            "mode": mode,
            "completed_at": utc_now_text(),
            "summary_path": str(
                run_directory / "workflow-summary.jsonl"
            ),
            "result": result,
            "mutation_requested": mode == "apply",
            "mutation_occurred": mutation_occurred,
        },
    )


def _summary(
    *,
    run_id: str,
    run_directory: Path,
    config: OnboardingConfig,
    mode: str,
    action: str,
    result: int,
    mutation_occurred: bool,
    verification: str,
    requests: int,
    retries: int,
    started: float,
) -> dict[str, Any]:
    return {
        "record_type": "workflow_summary",
        "run_id": run_id,
        "organization": config.github.organization,
        "mode": mode,
        "mutation_requested": mode == "apply",
        "mutation_occurred": mutation_occurred,
        "repository": config.workflow.source_repository,
        "branch": config.workflow.branch,
        "path": config.workflow.path,
        "action": action,
        "verification": verification,
        "rest_requests": requests,
        "rest_retries": retries,
        "elapsed_seconds": round(
            time.monotonic() - started,
            3,
        ),
        "output_directory": str(run_directory),
        "result": result,
    }


def run_workflow(
    config: OnboardingConfig,
    workspace: Workspace,
    token: str,
    *,
    apply: bool,
    insecure: bool,
    transport: Any = None,
) -> tuple[int, Path]:
    started = time.monotonic()
    run_id, run_directory = (
        workspace.create_run_directory("workflow")
    )
    source_path, desired_content = (
        load_workflow_source(config)
    )
    desired_sha256 = sha256_bytes(desired_content)
    desired_blob_sha = git_blob_sha(desired_content)
    deadline = (
        started
        + config.inventory.max_hours * 3600.0
    )

    with GitHubRestClient(
        token,
        base_url=config.github.rest_api_url,
        timeout=config.inventory.timeout_seconds,
        max_attempts=config.inventory.retries,
        mutation_enabled=apply,
        deadline=deadline,
        verify=not insecure,
        transport=transport,
    ) as client:
        api = GitHubWorkflowAPI(
            client,
            config.workflow.source_repository,
        )
        repository = api.repository_state()
        current = api.get_file(
            config.workflow.path,
            ref=config.workflow.branch,
        )
        action, reason = _plan_action(
            repository,
            current,
            desired_content,
            config,
        )
        plan = [
            {
                "record_type": "workflow_plan_metadata",
                "run_id": run_id,
                "generated_at": utc_now_text(),
                "organization": config.github.organization,
                "mode": "apply" if apply else "dry_run",
                "config_sha256": config.source_sha256,
                "source_path": str(source_path),
                "source_sha256": desired_sha256,
            },
            _repository_record(repository),
            {
                "record_type": "workflow_file_plan",
                "repository": (
                    config.workflow.source_repository
                ),
                "branch": config.workflow.branch,
                "path": config.workflow.path,
                "action": action,
                "reason": reason,
                "current": _file_state(current),
                "desired": {
                    "sha256": desired_sha256,
                    "git_blob_sha": desired_blob_sha,
                    "size": len(desired_content),
                },
                "commit_message": (
                    "Add Black Duck required workflow"
                    if action == "create"
                    else "Update Black Duck required workflow"
                ),
            },
        ]
        atomic_write_jsonl(
            run_directory / "workflow-plan.jsonl",
            plan,
        )

        if not apply:
            result = (
                2
                if action in {"blocked", "conflict"}
                else 0
            )
            stats = client.stats()
            summary = _summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                mode="dry_run",
                action=action,
                result=result,
                mutation_occurred=False,
                verification="not_requested",
                requests=stats.requests,
                retries=stats.retries,
                started=started,
            )
            atomic_write_jsonl(
                run_directory / "workflow-summary.jsonl",
                [summary],
            )
            _latest(
                workspace,
                run_id=run_id,
                run_directory=run_directory,
                mode="dry_run",
                result=result,
                mutation_occurred=False,
            )
            return result, run_directory

        if action in {"blocked", "conflict"}:
            atomic_write_jsonl(
                run_directory / "workflow-apply.jsonl",
                [],
            )
            atomic_write_jsonl(
                run_directory
                / "workflow-verification.jsonl",
                [],
            )
            atomic_write_jsonl(
                run_directory
                / "workflow-rollback-plan.jsonl",
                [],
            )
            stats = client.stats()
            summary = _summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                mode="apply",
                action=action,
                result=2,
                mutation_occurred=False,
                verification="blocked",
                requests=stats.requests,
                retries=stats.retries,
                started=started,
            )
            atomic_write_jsonl(
                run_directory / "workflow-summary.jsonl",
                [summary],
            )
            _latest(
                workspace,
                run_id=run_id,
                run_directory=run_directory,
                mode="apply",
                result=2,
                mutation_occurred=False,
            )
            return 2, run_directory

        mutation_occurred = False
        apply_records: list[dict[str, Any]] = []
        rollback_records: list[dict[str, Any]] = [
            {
                "record_type": "workflow_rollback_metadata",
                "run_id": run_id,
                "organization": config.github.organization,
                "repository": (
                    config.workflow.source_repository
                ),
                "branch": config.workflow.branch,
                "path": config.workflow.path,
                "created_at": utc_now_text(),
            }
        ]

        if action in {"create", "update"}:
            repository_now = api.repository_state()
            current_now = api.get_file(
                config.workflow.path,
                ref=config.workflow.branch,
            )

            if (
                repository_now.node_id
                != repository.node_id
                or _file_state(current_now)
                != _file_state(current)
            ):
                raise ValueError(
                    "Workflow repository state changed after planning."
                )

            message = (
                "Add Black Duck required workflow"
                if action == "create"
                else "Update Black Duck required workflow"
            )
            write_result = api.put_file(
                config.workflow.path,
                branch=(
                    None
                    if repository_now.empty
                    and current_now is None
                    else config.workflow.branch
                ),
                message=message,
                content=desired_content,
                current_sha=(
                    current_now.sha
                    if current_now is not None
                    else None
                ),
            )
            mutation_occurred = True
            apply_records.append(
                {
                    "record_type": "workflow_apply",
                    "timestamp": utc_now_text(),
                    "repository": (
                        config.workflow.source_repository
                    ),
                    "branch": config.workflow.branch,
                    "path": config.workflow.path,
                    "action": action,
                    "result": "applied",
                    "previous_state_sha256": (
                        sha256_bytes(current.content)
                        if current is not None
                        else None
                    ),
                    "desired_state_sha256": desired_sha256,
                    "content_sha": write_result.content_sha,
                    "commit_sha": write_result.commit_sha,
                    "commit_html_url": (
                        write_result.commit_html_url
                    ),
                }
            )
            rollback_records.append(
                {
                    "record_type": "workflow_file_rollback",
                    "original_action": action,
                    "expected_current_sha256": (
                        desired_sha256
                    ),
                    "expected_current_git_blob_sha": (
                        desired_blob_sha
                    ),
                    "previous_github_sha": (
                        current.sha
                        if current is not None
                        else None
                    ),
                    "previous_content_base64": (
                        base64.b64encode(
                            current.content
                        ).decode("ascii")
                        if current is not None
                        else None
                    ),
                    "automatic_delete_supported": False,
                }
            )

        verified = api.get_file(
            config.workflow.path,
            ref=config.workflow.branch,
        )
        verification_ok = (
            verified is not None
            and verified.content == desired_content
            and verified.sha == desired_blob_sha
        )
        verification_records = [
            {
                "record_type": "workflow_verification",
                "timestamp": utc_now_text(),
                "repository": (
                    config.workflow.source_repository
                ),
                "branch": config.workflow.branch,
                "path": config.workflow.path,
                "result": (
                    "verified"
                    if verification_ok
                    else "mismatch"
                ),
                "desired_sha256": desired_sha256,
                "desired_git_blob_sha": desired_blob_sha,
                "observed": _file_state(verified),
            }
        ]

        atomic_write_jsonl(
            run_directory / "workflow-apply.jsonl",
            apply_records,
        )
        atomic_write_jsonl(
            run_directory / "workflow-verification.jsonl",
            verification_records,
        )
        atomic_write_jsonl(
            run_directory / "workflow-rollback-plan.jsonl",
            rollback_records,
        )

        result = 0 if verification_ok else 4
        stats = client.stats()
        summary = _summary(
            run_id=run_id,
            run_directory=run_directory,
            config=config,
            mode="apply",
            action=action,
            result=result,
            mutation_occurred=mutation_occurred,
            verification=(
                "verified"
                if verification_ok
                else "mismatch"
            ),
            requests=stats.requests,
            retries=stats.retries,
            started=started,
        )
        atomic_write_jsonl(
            run_directory / "workflow-summary.jsonl",
            [summary],
        )
        _latest(
            workspace,
            run_id=run_id,
            run_directory=run_directory,
            mode="apply",
            result=result,
            mutation_occurred=mutation_occurred,
        )
        return result, run_directory

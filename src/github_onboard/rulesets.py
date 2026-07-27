from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import OnboardError
from .github_api import GitHubPropertiesAPI
from .models import OnboardingConfig
from .rest import GitHubRestClient
from .ruleset_api import (
    GitHubRulesetsAPI,
    _validate_desired_ruleset,
    normalize_ruleset,
)
from .workflow import load_workflow_source
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
    sha256_json,
    utc_now_text,
)


def _properties_by_name(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    properties = value.get("properties")

    if not isinstance(properties, list):
        raise OnboardError(
            "GitHub returned invalid repository property values.",
            category="invalid_github_response",
        )

    result: dict[str, Any] = {}

    for item in properties:
        if not isinstance(item, dict):
            raise OnboardError(
                "GitHub returned an invalid repository property.",
                category="invalid_github_response",
            )

        name = item.get("property_name")

        if not isinstance(name, str) or not name:
            raise OnboardError(
                "GitHub returned a repository property without a name.",
                category="invalid_github_response",
            )

        if name in result:
            raise OnboardError(
                f"GitHub returned duplicate repository property "
                f"'{name}'.",
                category="invalid_github_response",
            )

        result[name] = item.get("value")

    return result


def _target_preview(
    assignments: Mapping[str, Mapping[str, Any]],
    config: OnboardingConfig,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []

    for assignment in assignments.values():
        full_name = assignment.get("repository_full_name")

        if not isinstance(full_name, str) or not full_name:
            raise OnboardError(
                "GitHub returned property values without a "
                "repository name.",
                category="invalid_github_response",
            )

        properties = _properties_by_name(assignment)
        policy = properties.get(
            config.properties.policy_name
        )

        if policy != config.ruleset.include_policy_value:
            continue

        repository_id = assignment.get("repository_id")

        if repository_id is not None and (
            type(repository_id) is not int
            or repository_id <= 0
        ):
            raise OnboardError(
                f"GitHub returned an invalid repository ID for "
                f"'{full_name}'.",
                category="invalid_github_response",
            )

        selected.append(
            {
                "repository_id": repository_id,
                "name_with_owner": full_name,
                "policy_property": (
                    config.properties.policy_name
                ),
                "policy_value": policy,
            }
        )

    return sorted(
        selected,
        key=lambda item: item[
            "name_with_owner"
        ].casefold(),
    )


def desired_ruleset(
    config: OnboardingConfig,
    workflow_repository_id: int,
    *,
    enforcement: str,
) -> dict[str, Any]:
    return {
        "name": config.ruleset.name,
        "target": "branch",
        "enforcement": enforcement,
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            },
            "repository_property": {
                "include": [
                    {
                        "name": (
                            config.properties.policy_name
                        ),
                        "property_values": [
                            config.ruleset.include_policy_value
                        ],
                    }
                ],
                "exclude": [],
            },
        },
        "rules": [
            {
                "type": "workflows",
                "parameters": {
                    "do_not_enforce_on_create": True,
                    "workflows": [
                        {
                            "path": config.workflow.path,
                            "ref": (
                                "refs/heads/"
                                f"{config.workflow.branch}"
                            ),
                            "repository_id": (
                                workflow_repository_id
                            ),
                        }
                    ],
                },
            }
        ],
    }


def _workflow_state(
    repository: RepositoryState,
    workflow: WorkflowFileState | None,
) -> dict[str, Any]:
    return {
        "repository_id": repository.repository_id,
        "node_id": repository.node_id,
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "visibility": repository.visibility,
        "can_push": repository.can_push,
        "workflow_file": (
            None
            if workflow is None
            else {
                "path": workflow.path,
                "github_sha": workflow.sha,
                "sha256": sha256_bytes(workflow.content),
                "size": len(workflow.content),
                "html_url": workflow.html_url,
            }
        ),
    }


def _managed_current(
    current: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = normalize_ruleset(current)

    try:
        return _validate_desired_ruleset(normalized)
    except OnboardError as error:
        raise OnboardError(
            "Existing ruleset with the configured name contains "
            "unapproved conditions, rules, or bypass actors.",
            category="ruleset_conflict",
        ) from error


def _without_enforcement(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key != "enforcement"
    }


def _plan_action(
    current_rulesets: Sequence[Mapping[str, Any]],
    desired: Mapping[str, Any],
    staged: Mapping[str, Any],
    *,
    activate: bool,
) -> tuple[str, str, int | None]:
    if len(current_rulesets) > 1:
        return (
            "conflict",
            "duplicate_ruleset_name",
            None,
        )

    if not current_rulesets:
        if activate:
            return (
                "blocked",
                "ruleset_must_exist_before_activation",
                None,
            )

        return "create", "ruleset_missing", None

    current_raw = current_rulesets[0]
    ruleset_id = current_raw.get("id")

    if type(ruleset_id) is not int or ruleset_id <= 0:
        raise OnboardError(
            "Existing ruleset has no valid numeric ID.",
            category="invalid_github_response",
        )

    current = _managed_current(current_raw)

    if current == desired:
        return (
            "no_change",
            "ruleset_matches",
            ruleset_id,
        )

    if activate:
        if (
            _without_enforcement(current)
            != _without_enforcement(staged)
            or current["enforcement"]
            not in {"disabled", "evaluate", "active"}
        ):
            return (
                "conflict",
                "ruleset_changed_before_activation",
                ruleset_id,
            )

        return "update", "activate_ruleset", ruleset_id

    if current["enforcement"] == "active":
        if (
            _without_enforcement(current)
            == _without_enforcement(desired)
        ):
            return (
                "no_change",
                "ruleset_already_active",
                ruleset_id,
            )

        return (
            "conflict",
            "active_ruleset_requires_manual_review",
            ruleset_id,
        )

    return "update", "ruleset_configuration_differs", ruleset_id


def _latest(
    workspace: Workspace,
    *,
    run_id: str,
    run_directory: Path,
    operation: str,
    mode: str,
    result: int,
    mutation_occurred: bool,
) -> None:
    atomic_write_json(
        workspace.rulesets_directory / "latest.json",
        {
            "run_id": run_id,
            "stage": "rulesets",
            "operation": operation,
            "mode": mode,
            "completed_at": utc_now_text(),
            "summary_path": str(
                run_directory / "ruleset-summary.jsonl"
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
    operation: str,
    mode: str,
    action: str,
    result: int,
    mutation_occurred: bool,
    verification: str,
    target_count: int,
    requests: int,
    retries: int,
    started: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "record_type": "ruleset_summary",
        "run_id": run_id,
        "organization": config.github.organization,
        "operation": operation,
        "mode": mode,
        "mutation_requested": mode == "apply",
        "mutation_occurred": mutation_occurred,
        "ruleset_name": config.ruleset.name,
        "action": action,
        "reason": reason,
        "target_repository_count": target_count,
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


def _state_digest(
    *,
    workflow_state: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    current_rulesets: Sequence[Mapping[str, Any]],
) -> str:
    return sha256_json(
        {
            "workflow": dict(workflow_state),
            "targets": list(targets),
            "rulesets": [
                {
                    "id": ruleset.get("id"),
                    "body": normalize_ruleset(ruleset),
                }
                for ruleset in current_rulesets
            ],
        }
    )


def run_rulesets(
    config: OnboardingConfig,
    workspace: Workspace,
    token: str,
    *,
    apply: bool,
    activate: bool,
    insecure: bool,
    transport: Any = None,
) -> tuple[int, Path]:
    if not config.ruleset.enabled:
        raise OnboardError(
            "Ruleset automation is disabled in configuration.",
            category="configuration_error",
        )

    started = time.monotonic()
    run_id, run_directory = (
        workspace.create_run_directory("rulesets")
    )
    operation = "activate" if activate else "configure"
    mode = "apply" if apply else "dry_run"
    deadline = (
        started
        + config.inventory.max_hours * 3600.0
    )
    _source_path, desired_workflow_content = (
        load_workflow_source(config)
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
        workflow_api = GitHubWorkflowAPI(
            client,
            config.workflow.source_repository,
        )
        property_api = GitHubPropertiesAPI(
            client,
            config.github.organization,
        )
        ruleset_api = GitHubRulesetsAPI(
            client,
            config.github.organization,
        )

        repository = workflow_api.repository_state()
        workflow_file = workflow_api.get_file(
            config.workflow.path,
            ref=config.workflow.branch,
        )
        definitions = property_api.list_definitions()
        assignments = (
            property_api.list_repository_assignments()
        )
        current_rulesets = ruleset_api.find_by_name(
            config.ruleset.name
        )
        targets = _target_preview(
            assignments,
            config,
        )
        selected_definition = next(
            (
                definition
                for definition in definitions
                if definition.get("property_name")
                == config.properties.policy_name
            ),
            None,
        )
        blockers: list[str] = []

        if selected_definition is None:
            blockers.append("policy_property_missing")
        else:
            allowed_values = selected_definition.get(
                "allowed_values"
            )

            if (
                not isinstance(allowed_values, list)
                or config.ruleset.include_policy_value
                not in allowed_values
            ):
                blockers.append(
                    "policy_property_value_missing"
                )

        workflow_state = _workflow_state(
            repository,
            workflow_file,
        )

        if workflow_file is None:
            blockers.append("required_workflow_missing")
        elif workflow_file.content != desired_workflow_content:
            blockers.append("required_workflow_out_of_date")

        if (
            repository.default_branch
            != config.workflow.branch
        ):
            blockers.append(
                "workflow_default_branch_mismatch"
            )

        if not targets:
            blockers.append("target_preview_empty")

        if any(
            target["name_with_owner"].casefold()
            == config.workflow.source_repository.casefold()
            for target in targets
        ):
            blockers.append(
                "workflow_repository_is_targeted"
            )

        staged = desired_ruleset(
            config,
            repository.repository_id,
            enforcement=config.ruleset.enforcement,
        )
        desired = desired_ruleset(
            config,
            repository.repository_id,
            enforcement=(
                "active"
                if activate
                else config.ruleset.enforcement
            ),
        )

        if blockers:
            action = "blocked"
            reason = ",".join(sorted(blockers))
            ruleset_id = None
        else:
            action, reason, ruleset_id = _plan_action(
                current_rulesets,
                desired,
                staged,
                activate=activate,
            )

        initial_digest = _state_digest(
            workflow_state=workflow_state,
            targets=targets,
            current_rulesets=current_rulesets,
        )
        plan = [
            {
                "record_type": "ruleset_plan_metadata",
                "run_id": run_id,
                "generated_at": utc_now_text(),
                "organization": config.github.organization,
                "operation": operation,
                "mode": mode,
                "config_sha256": config.source_sha256,
                "state_sha256": initial_digest,
            },
            {
                "record_type": "ruleset_target_preview",
                "property_name": (
                    config.properties.policy_name
                ),
                "property_value": (
                    config.ruleset.include_policy_value
                ),
                "repository_count": len(targets),
                "repositories": targets,
            },
            {
                "record_type": "ruleset_plan",
                "ruleset_id": ruleset_id,
                "name": config.ruleset.name,
                "action": action,
                "reason": reason,
                "current": (
                    None
                    if not current_rulesets
                    else {
                        "id": current_rulesets[0].get("id"),
                        "body": normalize_ruleset(
                            current_rulesets[0]
                        ),
                    }
                ),
                "desired": desired,
                "workflow": workflow_state,
            },
        ]
        atomic_write_jsonl(
            run_directory / "ruleset-plan.jsonl",
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
                operation=operation,
                mode=mode,
                action=action,
                result=result,
                mutation_occurred=False,
                verification="not_requested",
                target_count=len(targets),
                requests=stats.requests,
                retries=stats.retries,
                started=started,
                reason=reason,
            )
            atomic_write_jsonl(
                run_directory / "ruleset-summary.jsonl",
                [summary],
            )
            _latest(
                workspace,
                run_id=run_id,
                run_directory=run_directory,
                operation=operation,
                mode=mode,
                result=result,
                mutation_occurred=False,
            )
            return result, run_directory

        if action in {"blocked", "conflict"}:
            atomic_write_jsonl(
                run_directory / "ruleset-apply.jsonl",
                [],
            )
            atomic_write_jsonl(
                run_directory
                / "ruleset-verification.jsonl",
                [],
            )
            atomic_write_jsonl(
                run_directory
                / "ruleset-rollback-plan.jsonl",
                [],
            )
            stats = client.stats()
            summary = _summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                operation=operation,
                mode=mode,
                action=action,
                result=2,
                mutation_occurred=False,
                verification="blocked",
                target_count=len(targets),
                requests=stats.requests,
                retries=stats.retries,
                started=started,
                reason=reason,
            )
            atomic_write_jsonl(
                run_directory / "ruleset-summary.jsonl",
                [summary],
            )
            _latest(
                workspace,
                run_id=run_id,
                run_directory=run_directory,
                operation=operation,
                mode=mode,
                result=2,
                mutation_occurred=False,
            )
            return 2, run_directory

        repository_now = workflow_api.repository_state()
        workflow_now = workflow_api.get_file(
            config.workflow.path,
            ref=config.workflow.branch,
        )
        assignments_now = (
            property_api.list_repository_assignments()
        )
        targets_now = _target_preview(
            assignments_now,
            config,
        )
        current_now = ruleset_api.find_by_name(
            config.ruleset.name
        )
        current_digest = _state_digest(
            workflow_state=_workflow_state(
                repository_now,
                workflow_now,
            ),
            targets=targets_now,
            current_rulesets=current_now,
        )

        if current_digest != initial_digest:
            raise OnboardError(
                "Ruleset, workflow, or target state changed "
                "after planning.",
                category="ruleset_conflict",
            )

        previous = (
            None
            if not current_rulesets
            else normalize_ruleset(current_rulesets[0])
        )
        mutation_occurred = False
        apply_records: list[dict[str, Any]] = []

        if action == "create":
            resulting = ruleset_api.create_ruleset(
                desired
            )
            ruleset_id = resulting["id"]
            mutation_occurred = True
        elif action == "update":
            if ruleset_id is None:
                raise OnboardError(
                    "Ruleset update has no ruleset ID.",
                    category="ruleset_conflict",
                )

            resulting = ruleset_api.update_ruleset(
                ruleset_id,
                desired,
            )
            mutation_occurred = True
        else:
            resulting = current_rulesets[0]

        if mutation_occurred:
            apply_records.append(
                {
                    "record_type": "ruleset_apply",
                    "timestamp": utc_now_text(),
                    "ruleset_id": ruleset_id,
                    "name": config.ruleset.name,
                    "operation": operation,
                    "action": action,
                    "result": "applied",
                    "previous_state_sha256": (
                        None
                        if previous is None
                        else sha256_json(previous)
                    ),
                    "desired_state_sha256": (
                        sha256_json(desired)
                    ),
                }
            )

        if ruleset_id is None:
            ruleset_id = resulting.get("id")

        verified_raw = ruleset_api.get_ruleset(
            ruleset_id
        )
        verified = normalize_ruleset(verified_raw)
        verification_ok = verified == desired
        verification_records = [
            {
                "record_type": "ruleset_verification",
                "timestamp": utc_now_text(),
                "ruleset_id": ruleset_id,
                "name": config.ruleset.name,
                "result": (
                    "verified"
                    if verification_ok
                    else "mismatch"
                ),
                "desired_state_sha256": (
                    sha256_json(desired)
                ),
                "observed_state_sha256": (
                    sha256_json(verified)
                ),
            }
        ]
        rollback_records = [
            {
                "record_type": "ruleset_rollback_metadata",
                "run_id": run_id,
                "organization": config.github.organization,
                "created_at": utc_now_text(),
            },
            {
                "record_type": "ruleset_rollback",
                "ruleset_id": ruleset_id,
                "original_action": action,
                "expected_current": desired,
                "previous": previous,
                "new_ruleset_rollback": (
                    "disable"
                    if previous is None
                    else "restore"
                ),
                "automatic_delete_supported": False,
            },
        ]
        atomic_write_jsonl(
            run_directory / "ruleset-apply.jsonl",
            apply_records,
        )
        atomic_write_jsonl(
            run_directory / "ruleset-verification.jsonl",
            verification_records,
        )
        atomic_write_jsonl(
            run_directory / "ruleset-rollback-plan.jsonl",
            rollback_records,
        )
        result = 0 if verification_ok else 4
        stats = client.stats()
        summary = _summary(
            run_id=run_id,
            run_directory=run_directory,
            config=config,
            operation=operation,
            mode=mode,
            action=action,
            result=result,
            mutation_occurred=mutation_occurred,
            verification=(
                "verified"
                if verification_ok
                else "mismatch"
            ),
            target_count=len(targets),
            requests=stats.requests,
            retries=stats.retries,
            started=started,
            reason=reason,
        )
        atomic_write_jsonl(
            run_directory / "ruleset-summary.jsonl",
            [summary],
        )
        _latest(
            workspace,
            run_id=run_id,
            run_directory=run_directory,
            operation=operation,
            mode=mode,
            result=result,
            mutation_occurred=mutation_occurred,
        )
        return result, run_directory

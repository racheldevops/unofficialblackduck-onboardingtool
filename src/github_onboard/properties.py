from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import replace
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import OnboardError
from .github_api import (
    MAX_ASSIGNMENT_BATCH,
    GitHubPropertiesAPI,
)
from .inventory import run_fresh_inventory
from .models import InventoryBundle, OnboardingConfig
from .planning import (
    build_property_plan,
    plan_property_definitions,
)
from .rest import GitHubRestClient
from .workspace import (
    Workspace,
    atomic_write_json,
    atomic_write_jsonl,
    canonical_json_bytes,
    sha256_json,
    utc_now_text,
)


InventoryLoader = Callable[..., tuple[int, InventoryBundle]]


def _normalize_repository_scope(
    repositories: Sequence[str],
    organization: str,
) -> tuple[str, ...]:
    selected: list[str] = []
    normalized: set[str] = set()

    for repository in repositories:
        if not isinstance(repository, str):
            raise OnboardError(
                "Repository scope values must be strings.",
                category="repository_scope_error",
            )

        name = repository.strip()

        if (
            name.count("/") != 1
            or name.startswith("/")
            or name.endswith("/")
        ):
            raise OnboardError(
                f"Repository scope must use owner/name: {name!r}.",
                category="repository_scope_error",
            )

        owner, repository_name = name.split("/", 1)

        if (
            not owner
            or not repository_name
            or owner.casefold() != organization.casefold()
        ):
            raise OnboardError(
                f"Repository scope is outside organization "
                f"'{organization}': {name!r}.",
                category="repository_scope_error",
            )

        key = name.casefold()

        if key in normalized:
            raise OnboardError(
                f"Duplicate repository scope: {name}",
                category="repository_scope_error",
            )

        normalized.add(key)
        selected.append(name)

    return tuple(selected)


def _scope_inventory_bundle(
    bundle: InventoryBundle,
    repositories: Sequence[str],
) -> InventoryBundle:
    successful = {
        record["name_with_owner"].casefold(): record
        for record in bundle.inventory
    }
    checkpoint = {
        name.casefold(): state
        for name, state in bundle.checkpoint_states.items()
    }
    unavailable: list[str] = []

    for repository in repositories:
        key = repository.casefold()

        if key in successful:
            continue

        state = checkpoint.get(key)
        status = (
            state.get("checkpoint_status")
            if isinstance(state, dict)
            else "not_found"
        )
        unavailable.append(
            f"{repository} ({status})"
        )

    if unavailable:
        raise OnboardError(
            "Requested repositories are not successful inventory "
            "records: " + ", ".join(unavailable),
            category="repository_scope_error",
        )

    selected = tuple(
        successful[repository.casefold()]
        for repository in repositories
    )

    return replace(
        bundle,
        inventory=selected,
        failures=(),
    )


def _scope_record(
    *,
    limit: int | None,
    repositories: Sequence[str],
    selected_count: int,
) -> dict[str, Any]:
    if repositories:
        mode = "repositories"
    elif limit is not None:
        mode = "limit"
    else:
        mode = "all"

    return {
        "mode": mode,
        "limit": limit,
        "repositories": list(repositories),
        "selected_repository_count": selected_count,
    }


def _redact(value: str, token: str) -> str:
    if not token:
        return value

    return value.replace(token, "[REDACTED]")


def _error_fields(
    error: Exception,
    token: str,
) -> dict[str, Any]:
    return {
        "error_category": getattr(
            error,
            "category",
            type(error).__name__,
        ),
        "message": _redact(str(error), token),
        "attempts": int(getattr(error, "attempts", 1)),
        "http_status": getattr(error, "status_code", None),
    }


def _properties_by_name(
    properties: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for item in properties:
        name = item.get("property_name")

        if not isinstance(name, str) or not name:
            raise OnboardError(
                "GitHub returned a repository property without a name.",
                category="invalid_github_response",
            )

        if name in result:
            raise OnboardError(
                f"GitHub returned duplicate repository property '{name}'.",
                category="invalid_github_response",
            )

        result[name] = item.get("value")

    return result


def _managed_values(
    properties: Sequence[Mapping[str, Any]],
    managed_names: Sequence[str],
) -> dict[str, Any]:
    values = _properties_by_name(properties)

    return {
        name: values.get(name)
        for name in managed_names
    }


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _normalized(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(value)

    return value


def _equal(
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> bool:
    return all(
        _normalized(current.get(name))
        == _normalized(value)
        for name, value in desired.items()
    )


def _recheck_action(
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    refresh_all: bool,
) -> tuple[str, str]:
    if _equal(current, desired):
        return "no_change", "managed_values_match"

    if not any(
        not _empty(value)
        for value in current.values()
    ):
        return "initialize", "managed_values_absent"

    if refresh_all:
        return "update", "refresh_all"

    return "skipped_existing", "managed_values_preserved"


def _chunks(
    values: Sequence[dict[str, Any]],
    size: int,
) -> list[list[dict[str, Any]]]:
    return [
        list(values[offset : offset + size])
        for offset in range(0, len(values), size)
    ]


def _action_counts(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)

    for record in records:
        if record.get("record_type") != record_type:
            continue

        action = record.get("action")

        if isinstance(action, str):
            counts[action] += 1

    return dict(sorted(counts.items()))


def _publish_latest(
    workspace: Workspace,
    *,
    run_id: str,
    run_directory: Path,
    mode: str,
    result: int,
    mutation_requested: bool,
    mutation_occurred: bool,
) -> None:
    atomic_write_json(
        workspace.properties_directory / "latest.json",
        {
            "run_id": run_id,
            "stage": "properties",
            "mode": mode,
            "completed_at": utc_now_text(),
            "summary_path": str(
                run_directory / "property-summary.jsonl"
            ),
            "result": result,
            "mutation_requested": mutation_requested,
            "mutation_occurred": mutation_occurred,
        },
    )


def _write_summary(
    workspace: Workspace,
    run_directory: Path,
    summary: dict[str, Any],
) -> None:
    atomic_write_jsonl(
        run_directory / "property-summary.jsonl",
        [summary],
    )
    _publish_latest(
        workspace,
        run_id=summary["run_id"],
        run_directory=run_directory,
        mode=summary["mode"],
        result=summary["result"],
        mutation_requested=summary["mutation_requested"],
        mutation_occurred=summary["mutation_occurred"],
    )


def _base_summary(
    *,
    run_id: str,
    run_directory: Path,
    config: OnboardingConfig,
    bundle: InventoryBundle,
    plan_records: Sequence[Mapping[str, Any]],
    apply: bool,
    refresh_all: bool,
    started: float,
    result: int,
    mutation_occurred: bool,
    rest_requests: int,
    rest_retries: int,
    repository_failures: int,
    verification_mismatches: int,
    fatal_error: str | None = None,
) -> dict[str, Any]:
    return {
        "record_type": "property_summary",
        "run_id": run_id,
        "organization": config.github.organization,
        "mode": "apply" if apply else "dry_run",
        "mutation_requested": apply,
        "mutation_occurred": mutation_occurred,
        "refresh_all": refresh_all,
        "scope": next(
            (
                record.get("scope")
                for record in plan_records
                if record.get("record_type")
                == "property_plan_metadata"
            ),
            None,
        ),
        "inventory_sha256": bundle.sha256,
        "config_sha256": config.source_sha256,
        "inventory_repository_count": len(bundle.inventory),
        "inventory_failure_count": len(bundle.failures),
        "definition_action_counts": _action_counts(
            plan_records,
            "property_definition_plan",
        ),
        "assignment_action_counts": _action_counts(
            plan_records,
            "repository_property_plan",
        ),
        "repository_failure_count": repository_failures,
        "verification_mismatch_count": verification_mismatches,
        "rest_requests": rest_requests,
        "rest_retries": rest_retries,
        "elapsed_seconds": round(
            time.monotonic() - started,
            3,
        ),
        "output_directory": str(run_directory),
        "fatal_error": fatal_error,
        "result": result,
    }


def _plan_records(
    records: Sequence[dict[str, Any]],
    *,
    run_id: str,
    apply: bool,
    preflight: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = [dict(record) for record in records]
    result[0] = {
        **result[0],
        "run_id": run_id,
        "generated_at": utc_now_text(),
        "mode": "apply" if apply else "dry_run",
    }
    result.insert(
        1,
        {
            **dict(preflight),
            "run_id": run_id,
            "timestamp": utc_now_text(),
        },
    )
    return result


def _empty_apply_artifacts(
    run_directory: Path,
) -> None:
    atomic_write_jsonl(
        run_directory / "property-apply.jsonl",
        [],
    )
    atomic_write_jsonl(
        run_directory / "property-verification.jsonl",
        [],
    )
    atomic_write_jsonl(
        run_directory / "property-rollback-plan.jsonl",
        [],
    )


def _definition_apply(
    api: GitHubPropertiesAPI,
    definition_records: Sequence[dict[str, Any]],
    token: str,
) -> tuple[
    bool,
    bool,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    apply_records: list[dict[str, Any]] = []
    rollback_records: list[dict[str, Any]] = []
    mutation_occurred = False

    for record in definition_records:
        action = record["action"]

        if action == "no_change":
            continue

        if action not in {"create", "add_allowed_values"}:
            return (
                False,
                mutation_occurred,
                apply_records,
                rollback_records,
            )

        name = record["property_name"]
        desired = record["desired_definition"]
        previous = record.get("existing_definition")
        timestamp = utc_now_text()

        try:
            resulting = api.put_definition(desired)
        except Exception as error:
            apply_records.append(
                {
                    "record_type": "property_apply",
                    "timestamp": timestamp,
                    "resource_type": "property_definition",
                    "resource_identifier": name,
                    "action": action,
                    "result": "failed",
                    "previous_state_sha256": sha256_json(
                        previous
                    ),
                    "desired_state_sha256": sha256_json(
                        desired
                    ),
                    **_error_fields(error, token),
                }
            )
            return (
                False,
                mutation_occurred,
                apply_records,
                rollback_records,
            )

        mutation_occurred = True
        apply_records.append(
            {
                "record_type": "property_apply",
                "timestamp": timestamp,
                "resource_type": "property_definition",
                "resource_identifier": name,
                "action": action,
                "result": "applied",
                "previous_state_sha256": sha256_json(
                    previous
                ),
                "desired_state_sha256": sha256_json(
                    desired
                ),
                "resulting_state_sha256": sha256_json(
                    resulting
                ),
                "attempts": 1,
            }
        )
        rollback_records.append(
            {
                "record_type": "property_definition_rollback",
                "property_name": name,
                "original_action": action,
                "expected_current_definition": desired,
                "previous_definition": previous,
                "automatic_apply_supported": False,
            }
        )

    return (
        True,
        mutation_occurred,
        apply_records,
        rollback_records,
    )


def _verify_definitions(
    api: GitHubPropertiesAPI,
    bundle: InventoryBundle,
    config: OnboardingConfig,
) -> tuple[bool, list[dict[str, Any]]]:
    resulting = api.list_definitions()
    verification_plan = plan_property_definitions(
        bundle,
        config,
        resulting,
    )
    verification_records: list[dict[str, Any]] = []
    valid = True

    for record in verification_plan:
        verified = record["action"] == "no_change"

        if not verified:
            valid = False

        verification_records.append(
            {
                "record_type": "property_verification",
                "timestamp": utc_now_text(),
                "resource_type": "property_definition",
                "resource_identifier": record["property_name"],
                "result": (
                    "verified"
                    if verified
                    else "mismatch"
                ),
                "observed_action": record["action"],
                "reason": record["reason"],
            }
        )

    return valid, verification_records


def _prepare_assignment_candidates(
    api: GitHubPropertiesAPI,
    assignment_records: Sequence[dict[str, Any]],
    config: OnboardingConfig,
    *,
    refresh_all: bool,
    token: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
]:
    ready: list[dict[str, Any]] = []
    verification_records: list[dict[str, Any]] = []
    repository_failures = 0
    managed_names = config.properties.managed_names

    for record in assignment_records:
        planned_action = record["action"]

        if planned_action == "no_change":
            verification_records.append(
                {
                    "record_type": "property_verification",
                    "timestamp": utc_now_text(),
                    "resource_type": "repository_assignment",
                    "repository_id": record["repository_id"],
                    "name_with_owner": record["name_with_owner"],
                    "result": "no_change",
                }
            )
            continue

        if planned_action == "skipped_existing":
            verification_records.append(
                {
                    "record_type": "property_verification",
                    "timestamp": utc_now_text(),
                    "resource_type": "repository_assignment",
                    "repository_id": record["repository_id"],
                    "name_with_owner": record["name_with_owner"],
                    "result": "skipped_existing",
                }
            )
            continue

        name = record["name_with_owner"]

        try:
            current_properties = api.get_repository_values(
                name
            )
            current = _managed_values(
                current_properties,
                managed_names,
            )
        except Exception as error:
            repository_failures += 1
            verification_records.append(
                {
                    "record_type": "property_verification",
                    "timestamp": utc_now_text(),
                    "resource_type": "repository_assignment",
                    "repository_id": record["repository_id"],
                    "name_with_owner": name,
                    "result": "read_failed",
                    **_error_fields(error, token),
                }
            )
            continue

        action, reason = _recheck_action(
            current,
            record["desired_values"],
            refresh_all=refresh_all,
        )

        if action in {"no_change", "skipped_existing"}:
            verification_records.append(
                {
                    "record_type": "property_verification",
                    "timestamp": utc_now_text(),
                    "resource_type": "repository_assignment",
                    "repository_id": record["repository_id"],
                    "name_with_owner": name,
                    "result": action,
                    "reason": reason,
                }
            )
            continue

        ready.append(
            {
                **record,
                "action": action,
                "previous_values": current,
            }
        )

    return ready, verification_records, repository_failures


def _apply_assignment_batches(
    api: GitHubPropertiesAPI,
    candidates: Sequence[dict[str, Any]],
    token: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
    int,
]:
    apply_records: list[dict[str, Any]] = []
    rollback_records: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    repository_failures = 0
    mutation_occurred = False
    grouped: dict[bytes, list[dict[str, Any]]] = defaultdict(
        list
    )

    for record in candidates:
        grouped[
            canonical_json_bytes(record["desired_values"])
        ].append(record)

    for key in sorted(grouped):
        group = grouped[key]

        for batch in _chunks(
            group,
            MAX_ASSIGNMENT_BATCH,
        ):
            names = [
                record["name_with_owner"]
                for record in batch
            ]
            desired = batch[0]["desired_values"]
            timestamp = utc_now_text()

            try:
                api.set_repository_values(
                    names,
                    desired,
                )
            except Exception as error:
                for record in batch:
                    repository_failures += 1
                    apply_records.append(
                        {
                            "record_type": "property_apply",
                            "timestamp": timestamp,
                            "resource_type": (
                                "repository_assignment"
                            ),
                            "repository_id": (
                                record["repository_id"]
                            ),
                            "name_with_owner": (
                                record["name_with_owner"]
                            ),
                            "action": record["action"],
                            "result": "failed",
                            "previous_state_sha256": (
                                sha256_json(
                                    record["previous_values"]
                                )
                            ),
                            "desired_state_sha256": (
                                sha256_json(desired)
                            ),
                            **_error_fields(error, token),
                        }
                    )

                continue

            mutation_occurred = True

            for record in batch:
                successful.append(record)
                apply_records.append(
                    {
                        "record_type": "property_apply",
                        "timestamp": timestamp,
                        "resource_type": (
                            "repository_assignment"
                        ),
                        "repository_id": record["repository_id"],
                        "name_with_owner": (
                            record["name_with_owner"]
                        ),
                        "action": record["action"],
                        "result": "applied",
                        "previous_state_sha256": sha256_json(
                            record["previous_values"]
                        ),
                        "desired_state_sha256": sha256_json(
                            record["desired_values"]
                        ),
                        "http_status": 204,
                        "attempts": 1,
                    }
                )
                rollback_records.append(
                    {
                        "record_type": (
                            "repository_property_rollback"
                        ),
                        "repository_id": record["repository_id"],
                        "name_with_owner": (
                            record["name_with_owner"]
                        ),
                        "expected_current_values": (
                            record["desired_values"]
                        ),
                        "restore_values": (
                            record["previous_values"]
                        ),
                    }
                )

    return (
        successful,
        apply_records,
        rollback_records,
        mutation_occurred,
        repository_failures,
    )


def _verify_assignments(
    api: GitHubPropertiesAPI,
    successful: Sequence[dict[str, Any]],
    config: OnboardingConfig,
    token: str,
) -> tuple[list[dict[str, Any]], int, int]:
    verification_records: list[dict[str, Any]] = []
    repository_failures = 0
    verification_mismatches = 0
    managed_names = config.properties.managed_names

    for record in successful:
        name = record["name_with_owner"]

        try:
            resulting_properties = api.get_repository_values(
                name
            )
            resulting = _managed_values(
                resulting_properties,
                managed_names,
            )
        except Exception as error:
            repository_failures += 1
            verification_mismatches += 1
            verification_records.append(
                {
                    "record_type": "property_verification",
                    "timestamp": utc_now_text(),
                    "resource_type": "repository_assignment",
                    "repository_id": record["repository_id"],
                    "name_with_owner": name,
                    "result": "read_failed",
                    **_error_fields(error, token),
                }
            )
            continue

        verified = _equal(
            resulting,
            record["desired_values"],
        )

        if not verified:
            repository_failures += 1
            verification_mismatches += 1

        verification_records.append(
            {
                "record_type": "property_verification",
                "timestamp": utc_now_text(),
                "resource_type": "repository_assignment",
                "repository_id": record["repository_id"],
                "name_with_owner": name,
                "result": (
                    "verified"
                    if verified
                    else "mismatch"
                ),
                "desired_state_sha256": sha256_json(
                    record["desired_values"]
                ),
                "resulting_state_sha256": sha256_json(
                    resulting
                ),
            }
        )

    return (
        verification_records,
        repository_failures,
        verification_mismatches,
    )


def run_properties(
    config: OnboardingConfig,
    workspace: Workspace,
    token: str,
    *,
    apply: bool,
    refresh_all: bool,
    insecure: bool,
    limit: int | None = None,
    repositories: Sequence[str] = (),
    inventory_loader: InventoryLoader = run_fresh_inventory,
    transport: Any = None,
) -> tuple[int, Path]:
    if limit is not None and (
        type(limit) is not int or limit <= 0
    ):
        raise OnboardError(
            "Property scope limit must be a positive integer.",
            category="repository_scope_error",
        )

    repository_scope = _normalize_repository_scope(
        repositories,
        config.github.organization,
    )

    if (
        limit is not None
        and repository_scope
        and len(repository_scope) > limit
    ):
        raise OnboardError(
            "Explicit repository count exceeds --limit.",
            category="repository_scope_error",
        )

    started = time.monotonic()
    run_id, run_directory = (
        workspace.create_run_directory("properties")
    )
    inventory_limit = (
        None
        if repository_scope
        else limit
    )

    if inventory_limit is None:
        inventory_result, bundle = inventory_loader(
            config,
            workspace.inventory_directory,
            token,
            insecure=insecure,
        )
    else:
        inventory_result, bundle = inventory_loader(
            config,
            workspace.inventory_directory,
            token,
            insecure=insecure,
            limit=inventory_limit,
        )

    if repository_scope:
        bundle = _scope_inventory_bundle(
            bundle,
            repository_scope,
        )
        inventory_result = 0

    scope = _scope_record(
        limit=limit,
        repositories=repository_scope,
        selected_count=len(bundle.inventory),
    )
    deadline = (
        started
        + config.inventory.max_hours * 3600.0
    )
    client = GitHubRestClient(
        token,
        base_url=config.github.rest_api_url,
        timeout=config.inventory.timeout_seconds,
        max_attempts=config.inventory.retries,
        mutation_enabled=apply,
        deadline=deadline,
        verify=not insecure,
        transport=transport,
    )

    try:
        api = GitHubPropertiesAPI(
            client,
            config.github.organization,
        )
        preflight = api.preflight()
        definitions = api.list_definitions()
        assignments = api.list_repository_assignments()
        raw_plan = build_property_plan(
            bundle,
            config,
            definitions,
            assignments,
            refresh_all=refresh_all,
        )
        raw_plan = (
            {
                **raw_plan[0],
                "scope": scope,
            },
            *raw_plan[1:],
        )
        plan_records = _plan_records(
            raw_plan,
            run_id=run_id,
            apply=apply,
            preflight=preflight,
        )
        atomic_write_jsonl(
            run_directory / "property-plan.jsonl",
            plan_records,
        )
        shared_blocked = bool(
            raw_plan[0]["shared_schema_blocked"]
        )

        if not apply:
            stats = client.stats()
            result = (
                2
                if shared_blocked
                else 1
                if inventory_result == 1
                else 0
            )
            summary = _base_summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                bundle=bundle,
                plan_records=plan_records,
                apply=False,
                refresh_all=refresh_all,
                started=started,
                result=result,
                mutation_occurred=False,
                rest_requests=stats.requests,
                rest_retries=stats.retries,
                repository_failures=len(bundle.failures),
                verification_mismatches=0,
                fatal_error=(
                    "Property schema conflicts or blocked "
                    "actions remain."
                    if shared_blocked
                    else None
                ),
            )
            _write_summary(
                workspace,
                run_directory,
                summary,
            )
            return result, run_directory

        if shared_blocked:
            _empty_apply_artifacts(run_directory)
            stats = client.stats()
            summary = _base_summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                bundle=bundle,
                plan_records=plan_records,
                apply=True,
                refresh_all=refresh_all,
                started=started,
                result=2,
                mutation_occurred=False,
                rest_requests=stats.requests,
                rest_retries=stats.retries,
                repository_failures=len(bundle.failures),
                verification_mismatches=0,
                fatal_error=(
                    "Property schema conflicts or blocked "
                    "actions remain."
                ),
            )
            _write_summary(
                workspace,
                run_directory,
                summary,
            )
            return 2, run_directory

        definition_records = [
            record
            for record in raw_plan
            if record.get("record_type")
            == "property_definition_plan"
        ]
        assignment_records = [
            record
            for record in raw_plan
            if record.get("record_type")
            == "repository_property_plan"
        ]
        (
            definitions_applied,
            definition_mutation,
            definition_apply_records,
            definition_rollback_records,
        ) = _definition_apply(
            api,
            definition_records,
            token,
        )

        if not definitions_applied:
            atomic_write_jsonl(
                run_directory / "property-apply.jsonl",
                definition_apply_records,
            )
            atomic_write_jsonl(
                run_directory / "property-verification.jsonl",
                [],
            )
            atomic_write_jsonl(
                run_directory
                / "property-rollback-plan.jsonl",
                [
                    {
                        "record_type": (
                            "property_rollback_metadata"
                        ),
                        "run_id": run_id,
                        "organization": (
                            config.github.organization
                        ),
                        "created_at": utc_now_text(),
                        "source_inventory_sha256": bundle.sha256,
                    },
                    *definition_rollback_records,
                ],
            )
            stats = client.stats()
            summary = _base_summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                bundle=bundle,
                plan_records=plan_records,
                apply=True,
                refresh_all=refresh_all,
                started=started,
                result=2,
                mutation_occurred=definition_mutation,
                rest_requests=stats.requests,
                rest_retries=stats.retries,
                repository_failures=len(bundle.failures),
                verification_mismatches=0,
                fatal_error=(
                    "Property definition application failed."
                ),
            )
            _write_summary(
                workspace,
                run_directory,
                summary,
            )
            return 2, run_directory

        (
            definitions_verified,
            definition_verification_records,
        ) = _verify_definitions(
            api,
            bundle,
            config,
        )

        if not definitions_verified:
            atomic_write_jsonl(
                run_directory / "property-apply.jsonl",
                definition_apply_records,
            )
            atomic_write_jsonl(
                run_directory / "property-verification.jsonl",
                definition_verification_records,
            )
            atomic_write_jsonl(
                run_directory
                / "property-rollback-plan.jsonl",
                [
                    {
                        "record_type": (
                            "property_rollback_metadata"
                        ),
                        "run_id": run_id,
                        "organization": (
                            config.github.organization
                        ),
                        "created_at": utc_now_text(),
                        "source_inventory_sha256": bundle.sha256,
                    },
                    *definition_rollback_records,
                ],
            )
            stats = client.stats()
            summary = _base_summary(
                run_id=run_id,
                run_directory=run_directory,
                config=config,
                bundle=bundle,
                plan_records=plan_records,
                apply=True,
                refresh_all=refresh_all,
                started=started,
                result=2,
                mutation_occurred=definition_mutation,
                rest_requests=stats.requests,
                rest_retries=stats.retries,
                repository_failures=len(bundle.failures),
                verification_mismatches=1,
                fatal_error=(
                    "Property definition verification failed."
                ),
            )
            _write_summary(
                workspace,
                run_directory,
                summary,
            )
            return 2, run_directory

        (
            candidates,
            initial_verification_records,
            preparation_failures,
        ) = _prepare_assignment_candidates(
            api,
            assignment_records,
            config,
            refresh_all=refresh_all,
            token=token,
        )
        (
            successful,
            assignment_apply_records,
            assignment_rollback_records,
            assignment_mutation,
            batch_failures,
        ) = _apply_assignment_batches(
            api,
            candidates,
            token,
        )
        (
            final_verification_records,
            final_failures,
            verification_mismatches,
        ) = _verify_assignments(
            api,
            successful,
            config,
            token,
        )
        apply_records = [
            *definition_apply_records,
            *assignment_apply_records,
        ]
        verification_records = [
            *definition_verification_records,
            *initial_verification_records,
            *final_verification_records,
        ]
        rollback_records = [
            {
                "record_type": "property_rollback_metadata",
                "run_id": run_id,
                "organization": config.github.organization,
                "created_at": utc_now_text(),
                "source_inventory_sha256": bundle.sha256,
            },
            *definition_rollback_records,
            *assignment_rollback_records,
        ]
        atomic_write_jsonl(
            run_directory / "property-apply.jsonl",
            apply_records,
        )
        atomic_write_jsonl(
            run_directory / "property-verification.jsonl",
            verification_records,
        )
        atomic_write_jsonl(
            run_directory / "property-rollback-plan.jsonl",
            rollback_records,
        )
        repository_failures = (
            len(bundle.failures)
            + preparation_failures
            + batch_failures
            + final_failures
        )
        result = 1 if repository_failures else 0
        stats = client.stats()
        mutation_occurred = (
            definition_mutation
            or assignment_mutation
        )
        summary = _base_summary(
            run_id=run_id,
            run_directory=run_directory,
            config=config,
            bundle=bundle,
            plan_records=plan_records,
            apply=True,
            refresh_all=refresh_all,
            started=started,
            result=result,
            mutation_occurred=mutation_occurred,
            rest_requests=stats.requests,
            rest_retries=stats.retries,
            repository_failures=repository_failures,
            verification_mismatches=verification_mismatches,
        )
        _write_summary(
            workspace,
            run_directory,
            summary,
        )
        return result, run_directory
    finally:
        client.close()

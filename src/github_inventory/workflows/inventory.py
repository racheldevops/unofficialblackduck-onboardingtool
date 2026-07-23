from __future__ import annotations

import argparse
import datetime as dt
import time
from typing import Any

from ..classification import (
    build_excluded_record,
    build_inventory_record,
    exclusion_reason,
    isoformat_utc,
    needs_manifest_inspection,
    parse_github_timestamp,
    redact,
    utc_now,
    validate_repository_metadata,
)
from ..errors import GitHubError, InventoryError, RuntimeBudgetExceeded
from ..github.client import GitHubClient
from ..github.repositories import (
    bounded_inspections,
    discover_repositories,
    preflight,
)
from ..reporting import (
    emit_event,
    output_directory,
    projected_seconds,
    projection_accuracy,
)
from ..settings import (
    ACTIVITY_DAYS,
    ACTIVITY_POLICY_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    LANGUAGE_POLICY_VERSION,
    PILOT_SELECTION_METHOD,
)
from ..storage.checkpoints import (
    CheckpointWriter,
    atomic_publish_jsonl,
    checkpoint_configuration,
    failure_record,
    initialize_checkpoint,
    load_checkpoint,
    reconcile,
    validate_checkpoint,
)


def _repository_name(
    repository: dict[str, Any],
) -> str | None:
    name = repository.get("nameWithOwner")
    return name if isinstance(name, str) and name else None


def _successful_state(
    record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "checkpoint_status": "successful",
        "repository_id": record["repository_id"],
        "name_with_owner": record["name_with_owner"],
        "inventory": record,
    }


def _failed_state(
    failure: dict[str, Any],
) -> dict[str, Any] | None:
    name = failure.get("name_with_owner")
    if not isinstance(name, str) or not name:
        return None

    return {
        "checkpoint_status": "failed",
        "repository_id": failure.get("repository_id"),
        "name_with_owner": name,
        "failure": failure,
    }


def _restore_checkpoint_states(
    states: dict[str, dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    successful: dict[str, dict[str, Any]] = {}
    excluded: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []

    for name, state in states.items():
        checkpoint_status = state.get("checkpoint_status")

        if checkpoint_status == "successful":
            inventory = state.get("inventory")
            if not isinstance(inventory, dict):
                raise InventoryError(
                    f"Successful checkpoint '{name}' has no inventory."
                )
            successful[name] = inventory
        elif checkpoint_status == "excluded":
            excluded[name] = {
                "checkpoint_status": "excluded",
                "repository_id": state.get("repository_id"),
                "name_with_owner": name,
                "exclusion_reason": state.get("exclusion_reason"),
            }
        elif checkpoint_status == "failed":
            failure = state.get("failure")
            if not isinstance(failure, dict):
                raise InventoryError(
                    f"Failed checkpoint '{name}' has no failure record."
                )
            failures.append(failure)
        else:
            raise InventoryError(
                f"Checkpoint '{name}' has an invalid status."
            )

    return successful, excluded, failures


def run_inventory(
    args: argparse.Namespace,
    organization: str,
    token: str,
) -> int:
    started = time.monotonic()
    maximum_seconds = args.max_hours * 3600.0
    deadline = started + maximum_seconds
    out = output_directory(args.output_dir, organization, args.dry_run)
    out.mkdir(parents=True, exist_ok=True)

    checkpoint_path = out / "checkpoint.jsonl"
    inventory_path = out / "inventory.jsonl"
    failures_path = out / "failures.jsonl"
    summary_path = out / "summary.jsonl"

    if args.resume and args.discard_checkpoint:
        raise InventoryError(
            "--resume and --discard-checkpoint cannot be combined."
        )

    if args.inspection_depth != "root":
        raise InventoryError(
            "Plan 1 inventory requires root-only inspection."
        )

    if args.discard_checkpoint:
        checkpoint_path.unlink(missing_ok=True)

    client = GitHubClient(
        token,
        timeout=args.timeout,
        max_attempts=args.retries,
        deadline=deadline,
        verify=not getattr(args, "insecure", False),
    )

    repositories: list[dict[str, Any]] = []
    organization_repository_count = 0
    successful: dict[str, dict[str, Any]] = {}
    excluded: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    cutoff = utc_now() - dt.timedelta(days=ACTIVITY_DAYS)
    estimated_full_seconds: float | None = None
    discovery_elapsed = 0.0
    discovery_completed = False
    aborted_reason: str | None = None
    fatal_error: str | None = None

    try:
        preflight_result = preflight(client, organization)

        if not preflight_result["viewer_can_administer"]:
            raise GitHubError(
                "administration_required",
                (
                    "GITHUB_TOKEN cannot administer the organization; "
                    "Plan 1 requires organization administration capability."
                ),
                attempts=1,
            )

        emit_event(
            "preflight_complete",
            organization=organization,
            viewer_can_administer=True,
            visible_repository_count=preflight_result[
                "visible_repository_count"
            ],
            graphql_rate_remaining=preflight_result[
                "graphql_rate_remaining"
            ],
        )

        loaded_states: dict[str, dict[str, Any]] = {}

        if args.resume:
            metadata, loaded_states = load_checkpoint(checkpoint_path)
            validate_checkpoint(
                metadata,
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "organization": organization,
                    "activity_days": ACTIVITY_DAYS,
                    "language_policy": LANGUAGE_POLICY_VERSION,
                    "inspection_depth": args.inspection_depth,
                    "pilot_limit": args.limit,
                    "pilot_selection_method": PILOT_SELECTION_METHOD,
                },
            )
            parsed_cutoff = parse_github_timestamp(metadata.get("cutoff"))
            if parsed_cutoff is None:
                raise InventoryError(
                    "Checkpoint has no valid cutoff timestamp."
                )
            cutoff = parsed_cutoff
        else:
            if checkpoint_path.exists():
                raise InventoryError(
                    f"Checkpoint already exists at {checkpoint_path}. "
                    "Use --resume or --discard-checkpoint."
                )

            initialize_checkpoint(
                checkpoint_path,
                checkpoint_configuration(
                    organization,
                    cutoff,
                    args.inspection_depth,
                    args.limit,
                ),
            )

        discovery_started = time.monotonic()

        def discovery_progress(
            discovered: int,
            total: int,
        ) -> None:
            elapsed = time.monotonic() - discovery_started
            estimate = projected_seconds(
                elapsed,
                discovered,
                total,
            )
            emit_event(
                "discovery_progress",
                discovered=discovered,
                organization_total=total,
                elapsed_seconds=round(elapsed, 3),
                estimated_discovery_seconds=(
                    round(estimate, 3)
                    if estimate is not None
                    else None
                ),
                estimate_accuracy=projection_accuracy(discovered),
            )

        repositories, organization_repository_count = (
            discover_repositories(
                client,
                organization,
                limit=args.limit,
                progress=discovery_progress,
            )
        )
        discovery_elapsed = time.monotonic() - discovery_started
        estimated_full_seconds = discovery_elapsed
        discovery_completed = True

        if time.monotonic() >= deadline:
            raise RuntimeBudgetExceeded(
                f"The inventory exceeded {args.max_hours:g} hours."
            )

        selected_by_name = {
            name: repository
            for repository in repositories
            if (name := _repository_name(repository)) is not None
        }

        for name, state in loaded_states.items():
            repository = selected_by_name.get(name)
            if repository is None:
                raise InventoryError(
                    f"Checkpoint repository '{name}' is not in the "
                    "deterministic current selection."
                )

            checkpoint_id = state.get("repository_id")
            current_id = repository.get("id")
            if (
                isinstance(checkpoint_id, str)
                and isinstance(current_id, str)
                and checkpoint_id != current_id
            ):
                raise InventoryError(
                    f"Checkpoint repository ID changed for '{name}'."
                )

        successful, excluded, failures = (
            _restore_checkpoint_states(loaded_states)
        )
        pending_candidates: list[dict[str, Any]] = []
        scale = (
            organization_repository_count / len(repositories)
            if repositories
            and organization_repository_count > len(repositories)
            else 1.0
        )

        with CheckpointWriter(checkpoint_path) as checkpoint:

            def record_failure(
                repository: dict[str, Any],
                operation: str,
                error: Exception,
            ) -> None:
                failure = failure_record(
                    repository,
                    operation,
                    error,
                )
                failure["message"] = redact(
                    str(failure["message"]),
                    [token],
                )
                failures.append(failure)
                state = _failed_state(failure)
                if state is not None:
                    checkpoint.append(state)

            for repository in repositories:
                if time.monotonic() >= deadline:
                    raise RuntimeBudgetExceeded(
                        f"The inventory exceeded {args.max_hours:g} hours."
                    )

                name = _repository_name(repository)
                if name is not None and name in loaded_states:
                    continue

                try:
                    validate_repository_metadata(repository)

                    if exclusion_reason(repository):
                        exclusion = build_excluded_record(repository)
                        excluded[
                            exclusion["name_with_owner"]
                        ] = exclusion
                        checkpoint.append(exclusion)
                        continue

                    if needs_manifest_inspection(repository):
                        pending_candidates.append(repository)
                        continue

                    record = build_inventory_record(
                        repository,
                        cutoff,
                    )
                    successful[
                        record["name_with_owner"]
                    ] = record
                    checkpoint.append(_successful_state(record))
                except InventoryError as error:
                    record_failure(
                        repository,
                        "repository metadata validation",
                        error,
                    )

            projected_candidate_total = (
                len(pending_candidates) * scale
            )
            inspection_started = time.monotonic()
            completed = 0
            progress_interval = max(
                1,
                min(25, len(pending_candidates) // 20 or 1),
            )

            for repository, paths, error in bounded_inspections(
                client,
                pending_candidates,
                depth=args.inspection_depth,
                workers=args.workers,
            ):
                completed += 1

                if error is not None:
                    record_failure(
                        repository,
                        "manifest inspection",
                        error,
                    )
                else:
                    try:
                        record = build_inventory_record(
                            repository,
                            cutoff,
                            paths or (),
                        )
                    except InventoryError as metadata_error:
                        record_failure(
                            repository,
                            "repository metadata validation",
                            metadata_error,
                        )
                    else:
                        successful[
                            record["name_with_owner"]
                        ] = record
                        checkpoint.append(_successful_state(record))

                inspection_elapsed = (
                    time.monotonic() - inspection_started
                )
                projected_inspection = projected_seconds(
                    inspection_elapsed,
                    completed,
                    projected_candidate_total,
                )

                if projected_inspection is not None:
                    estimated_full_seconds = (
                        discovery_elapsed + projected_inspection
                    )

                should_report = (
                    completed in {1, 5, 10}
                    or completed % progress_interval == 0
                    or completed == len(pending_candidates)
                )

                if should_report:
                    elapsed_total = time.monotonic() - started
                    emit_event(
                        "inspection_progress",
                        completed=completed,
                        pending_total=len(pending_candidates),
                        projected_organization_candidates=round(
                            projected_candidate_total,
                            2,
                        ),
                        elapsed_seconds=round(elapsed_total, 3),
                        estimated_full_run_seconds=(
                            round(estimated_full_seconds, 3)
                            if estimated_full_seconds is not None
                            else None
                        ),
                        eta_seconds=(
                            round(
                                max(
                                    0.0,
                                    estimated_full_seconds
                                    - elapsed_total,
                                ),
                                3,
                            )
                            if estimated_full_seconds is not None
                            else None
                        ),
                        estimate_accuracy=projection_accuracy(
                            completed
                        ),
                    )

                minimum_samples = min(
                    10,
                    len(pending_candidates),
                )
                if (
                    completed >= minimum_samples
                    and estimated_full_seconds is not None
                    and estimated_full_seconds > maximum_seconds
                ):
                    raise RuntimeBudgetExceeded(
                        "Projected complete inventory time exceeds "
                        f"{args.max_hours:g} hours."
                    )

                if time.monotonic() >= deadline:
                    raise RuntimeBudgetExceeded(
                        f"The inventory exceeded {args.max_hours:g} hours."
                    )

    except RuntimeBudgetExceeded as error:
        aborted_reason = str(error)
        emit_event(
            "runtime_budget_exceeded",
            message=aborted_reason,
            estimated_full_run_seconds=estimated_full_seconds,
            maximum_seconds=maximum_seconds,
        )
    except Exception as error:
        fatal_error = redact(str(error), [token])
        emit_event(
            "fatal_error",
            category=getattr(
                error,
                "category",
                type(error).__name__,
            ),
            message=fatal_error,
        )
    finally:
        client.close()

    inventory_records = sorted(
        successful.values(),
        key=lambda record: record["name_with_owner"],
    )
    exclusion_records = sorted(
        excluded.values(),
        key=lambda record: record["name_with_owner"],
    )
    failures = sorted(
        failures,
        key=lambda record: (
            str(record.get("name_with_owner")),
            str(record.get("operation")),
        ),
    )
    discovered_names = {
        name
        for repository in repositories
        if (name := _repository_name(repository)) is not None
    }
    selected_count = len(repositories)
    elapsed_seconds = time.monotonic() - started
    pilot = (
        args.limit is not None
        and selected_count < organization_repository_count
    )
    run_mode = "pilot" if pilot else "full"

    if pilot:
        processing_elapsed = max(
            0.0,
            elapsed_seconds - discovery_elapsed,
        )
        projected_full_seconds = max(
            elapsed_seconds,
            discovery_elapsed
            + processing_elapsed
            * (
                organization_repository_count
                / max(1, selected_count)
            ),
            estimated_full_seconds or 0.0,
        )
    else:
        projected_full_seconds = elapsed_seconds

    reconciliation_ok = False

    if discovery_completed and not aborted_reason and not fatal_error:
        reconciliation_ok = reconcile(
            discovered_names,
            inventory_records,
            failures,
            exclusion_records,
            discovered_count=selected_count,
        )
        if not reconciliation_ok:
            fatal_error = (
                "Final categories do not reconcile with the selected "
                "repositories."
            )
            emit_event(
                "fatal_error",
                category="reconciliation_error",
                message=fatal_error,
            )

    stats = client.stats()
    summary = {
        "record_type": "summary",
        "organization": organization,
        "run_mode": run_mode,
        "dry_run": bool(args.dry_run),
        "pilot_limit": args.limit,
        "pilot_selection_method": (
            PILOT_SELECTION_METHOD
            if pilot
            else "all-visible-repositories"
        ),
        "inspection_depth": args.inspection_depth,
        "activity_policy": ACTIVITY_POLICY_VERSION,
        "activity_days": ACTIVITY_DAYS,
        "activity_cutoff": isoformat_utc(cutoff),
        "organization_repository_count": (
            organization_repository_count
        ),
        "discovered_repository_count": (
            organization_repository_count
            if discovery_completed
            else 0
        ),
        "selected_repository_count": selected_count,
        "successful_repository_count": len(inventory_records),
        "excluded_repository_count": len(exclusion_records),
        "failed_repository_count": len(failures),
        "unknown_language_count": sum(
            1
            for record in inventory_records
            if record.get("detected_languages") == ["unknown"]
        ),
        "multilanguage_repository_count": sum(
            1
            for record in inventory_records
            if (
                len(record.get("detected_languages", [])) > 1
                and "unknown"
                not in record.get("detected_languages", [])
            )
        ),
        "graphql_requests": stats.requests,
        "graphql_cost": stats.graphql_cost,
        "retries": stats.retries,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "projected_full_organization_seconds": round(
            projected_full_seconds,
            3,
        ),
        "estimated_full_run_seconds": round(
            projected_full_seconds,
            3,
        ),
        "maximum_seconds": maximum_seconds,
        "reconciliation_ok": reconciliation_ok,
        "aborted": bool(aborted_reason or fatal_error),
        "abort_reason": aborted_reason,
        "fatal_error": fatal_error,
        "output_directory": str(out),
    }

    if (
        discovery_completed
        and not aborted_reason
        and not fatal_error
        and reconciliation_ok
    ):
        try:
            atomic_publish_jsonl(
                {
                    inventory_path: inventory_records,
                    failures_path: failures,
                    summary_path: [summary],
                }
            )
        except Exception as error:
            fatal_error = redact(str(error), [token])
            summary["fatal_error"] = fatal_error
            summary["aborted"] = True
            summary["reconciliation_ok"] = False
            emit_event(
                "fatal_error",
                category=type(error).__name__,
                message=fatal_error,
            )

    emit_event("run_summary", **summary)

    if fatal_error or aborted_reason:
        return 2
    if failures:
        return 1
    return 0

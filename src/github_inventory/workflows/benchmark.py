from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import time
from typing import Any

from ..classification import *
from ..classification import _language_connection
from ..errors import *
from ..github.client import *
from ..github.repositories import *
from ..reporting import *
from ..settings import *
from ..storage.checkpoints import *


def run_depth_benchmark(
    args: argparse.Namespace,
    organization: str,
    token: str,
) -> int:
    started = time.monotonic()
    maximum_seconds = args.max_hours * 3600.0
    out = output_directory(args.output_dir, organization, args.dry_run)
    out.mkdir(parents=True, exist_ok=True)

    benchmark_path = out / "benchmark.jsonl"
    failures_path = out / "failures.jsonl"
    deadline = started + maximum_seconds

    client = GitHubClient(
        token,
        timeout=args.timeout,
        max_attempts=args.retries,
        deadline=deadline,
        verify=not getattr(args, "insecure", False),
    )

    benchmark_records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    try:
        preflight_result = preflight(client, organization)
        emit_event(
            "preflight_complete",
            organization=organization,
            visible_repository_count=preflight_result[
                "visible_repository_count"
            ],
        )

        discovery_started = time.monotonic()
        repositories, total_count = discover_repositories(
            client,
            organization,
            limit=args.limit,
        )
        discovery_elapsed = time.monotonic() - discovery_started
        scale = (
            total_count / len(repositories)
            if repositories and total_count > len(repositories)
            else 1.0
        )
        projected_discovery = discovery_elapsed * scale

        candidates = [
            repository
            for repository in repositories
            if needs_manifest_inspection(repository)
        ]

        benchmark_records.append(
            {
                "record_type": "depth_benchmark",
                "inspection_depth": "linguist",
                "valid_for_confirmed_secondary_policy": False,
                "sample_repository_count": len(repositories),
                "sample_candidate_count": len(candidates),
                "organization_repository_count": total_count,
                "elapsed_seconds": round(discovery_elapsed, 3),
                "projected_full_run_seconds": round(
                    projected_discovery,
                    3,
                ),
                "additional_repositories_with_languages": None,
                "graphql_requests": client.stats().requests,
                "graphql_cost": client.stats().graphql_cost,
            }
        )

        mode_paths: dict[str, dict[str, list[str]]] = {}
        mode_projections: dict[str, float] = {}

        for depth in ("root", "one"):
            before = client.stats()
            mode_started = time.monotonic()
            paths_by_name: dict[str, list[str]] = {}

            for repository, paths, error in bounded_inspections(
                client,
                candidates,
                depth=depth,
                workers=args.workers,
            ):
                name = repository.get("nameWithOwner")

                if error is not None:
                    failures.append(
                        failure_record(
                            repository,
                            f"{depth} benchmark inspection",
                            error,
                        )
                    )
                elif isinstance(name, str):
                    paths_by_name[name] = paths or []

            mode_elapsed = time.monotonic() - mode_started
            after = client.stats()
            projected = projected_discovery + mode_elapsed * scale
            mode_paths[depth] = paths_by_name
            mode_projections[depth] = projected

            benchmark_records.append(
                {
                    "record_type": "depth_benchmark",
                    "inspection_depth": depth,
                    "valid_for_confirmed_secondary_policy": True,
                    "sample_repository_count": len(repositories),
                    "sample_candidate_count": len(candidates),
                    "organization_repository_count": total_count,
                    "elapsed_seconds": round(
                        discovery_elapsed + mode_elapsed,
                        3,
                    ),
                    "inspection_elapsed_seconds": round(
                        mode_elapsed,
                        3,
                    ),
                    "projected_full_run_seconds": round(
                        projected,
                        3,
                    ),
                    "graphql_requests": (
                        after.requests - before.requests
                    ),
                    "graphql_cost": (
                        after.graphql_cost - before.graphql_cost
                    ),
                }
            )

        additional_repositories = 0
        additional_languages: set[str] = set()

        for repository in candidates:
            name = repository.get("nameWithOwner")
            if not isinstance(name, str):
                continue
            if name not in mode_paths["root"] or name not in mode_paths["one"]:
                continue

            edges, total_size = _language_connection(repository)
            root_languages = set(
                classify_languages(
                    edges,
                    total_size,
                    repository.get("diskUsage"),
                    mode_paths["root"][name],
                )
            )
            one_languages = set(
                classify_languages(
                    edges,
                    total_size,
                    repository.get("diskUsage"),
                    mode_paths["one"][name],
                )
            )
            additions = one_languages - root_languages

            if additions:
                additional_repositories += 1
                additional_languages.update(additions)

        recommendation: str | None
        recommendation_reason: str

        if failures:
            recommendation = None
            recommendation_reason = (
                "Benchmark API failures must be resolved first."
            )
        elif (
            additional_repositories == 0
            and mode_projections["root"] <= maximum_seconds
        ):
            recommendation = "root"
            recommendation_reason = (
                "One-level inspection found no additional qualifying "
                "languages in the sample."
            )
        elif (
            additional_repositories > 0
            and mode_projections["one"] <= maximum_seconds
        ):
            recommendation = "one"
            recommendation_reason = (
                "One-level inspection found additional qualifying "
                "languages and remains within the runtime budget."
            )
        elif (
            additional_repositories > 0
            and mode_projections["one"] > maximum_seconds
        ):
            recommendation = None
            recommendation_reason = (
                "One-level inspection found additional evidence but "
                "projects beyond the runtime budget; user direction is "
                "required."
            )
        else:
            recommendation = None
            recommendation_reason = (
                "No policy-compliant mode projects within the runtime "
                "budget."
            )

        benchmark_records.append(
            {
                "record_type": "depth_recommendation",
                "recommended_inspection_depth": recommendation,
                "reason": recommendation_reason,
                "additional_repositories_at_one_level": (
                    additional_repositories
                ),
                "additional_languages_at_one_level": sorted(
                    additional_languages
                ),
                "maximum_seconds": maximum_seconds,
                "output_directory": str(out),
            }
        )

        atomic_write_jsonl(benchmark_path, benchmark_records)
        atomic_write_jsonl(failures_path, failures)

        emit_event(
            "depth_benchmark_complete",
            recommendation=recommendation,
            additional_repositories_at_one_level=additional_repositories,
            output_directory=str(out),
        )

        if failures:
            return 1
        return 0 if recommendation is not None else 2

    except (InventoryError, GitHubError) as error:
        sanitized = redact(str(error), [token])
        atomic_write_jsonl(
            benchmark_path,
            [
                {
                    "record_type": "depth_benchmark_failure",
                    "error_category": getattr(
                        error,
                        "category",
                        type(error).__name__,
                    ),
                    "message": sanitized,
                }
            ],
        )
        emit_event(
            "fatal_error",
            category=getattr(error, "category", type(error).__name__),
            message=sanitized,
        )
        return 2
    finally:
        client.close()

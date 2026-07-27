from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from github_inventory.workflows.inventory import run_inventory

from .artifacts import load_inventory_bundle
from .errors import ArtifactError
from .models import InventoryBundle, OnboardingConfig


InventoryRunner = Callable[
    [argparse.Namespace, str, str],
    int,
]


def fresh_inventory_arguments(
    config: OnboardingConfig,
    output_directory: Path,
    *,
    insecure: bool,
    limit: int | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        max_hours=config.inventory.max_hours,
        output_dir=output_directory,
        dry_run=False,
        discard_checkpoint=True,
        resume=False,
        timeout=config.inventory.timeout_seconds,
        retries=config.inventory.retries,
        inspection_depth=config.inventory.inspection_depth,
        limit=limit,
        insecure=insecure,
        workers=config.inventory.workers,
        graphql_url=config.github.graphql_url,
    )


def run_fresh_inventory(
    config: OnboardingConfig,
    output_directory: Path,
    token: str,
    *,
    insecure: bool,
    limit: int | None = None,
    runner: InventoryRunner = run_inventory,
) -> tuple[int, InventoryBundle]:
    arguments = fresh_inventory_arguments(
        config,
        output_directory,
        insecure=insecure,
        limit=limit,
    )
    result = runner(
        arguments,
        config.github.organization,
        token,
    )

    if result not in {0, 1}:
        raise ArtifactError(
            f"Fresh inventory failed with exit code {result}."
        )

    bundle = load_inventory_bundle(
        output_directory,
        config.github.organization,
        secrets=(token,),
    )

    return result, bundle

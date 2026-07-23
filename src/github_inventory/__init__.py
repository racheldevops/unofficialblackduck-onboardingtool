"""Unofficially BlackDuck https://music.youtube.com/watch?v=IoCXLdMOEfc&list=OLAK5uy_m5Itv-eJ6Zoay9efgWhoF4DActgiCsVKA"""

from __future__ import annotations

from .settings import (
    GITHUB_GRAPHQL_URL,
    ACTIVITY_DAYS,
    LANGUAGE_POLICY_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    DEFAULT_WORKERS,
    DEFAULT_MAX_HOURS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    MANIFEST_LANGUAGE_MAP,
    SUPPORTED_MANIFEST_LANGUAGES,
)

from .errors import (
    InventoryError,
    RuntimeBudgetExceeded,
    GitHubError,
)

from .models import (
    ClientStats,
)

from .classification import (
    utc_now,
    isoformat_utc,
    parse_github_timestamp,
    normalize_language,
    classify_activity,
    manifest_languages,
    _language_totals,
    classify_languages,
    exclusion_reason,
    _language_connection,
    needs_manifest_inspection,
    build_inventory_record,
    redact,
)

from .github.queries import (
    DISCOVERY_QUERY,
    PREFLIGHT_QUERY,
    ROOT_TREE_QUERY,
    ONE_LEVEL_TREE_QUERY,
)

from .github.client import (
    GitHubClient,
)

from .github.repositories import (
    preflight,
    discover_repositories,
    inspect_manifest_paths,
    bounded_inspections,
)

from .storage.checkpoints import (
    failure_record,
    checkpoint_configuration,
    initialize_checkpoint,
    load_checkpoint,
    validate_checkpoint,
    CheckpointWriter,
    atomic_write_jsonl,
    reconcile,
)

from .reporting import (
    emit_event,
    output_directory,
    projection_accuracy,
    projected_seconds,
)

from .workflows.preflight import (
    run_preflight,
)

from .workflows.inventory import (
    run_inventory,
)

from .workflows.benchmark import (
    run_depth_benchmark,
)

from .cli import (
    positive_integer,
    positive_float,
    build_parser,
    main,
)


__all__ = [
    'GITHUB_GRAPHQL_URL',
    'ACTIVITY_DAYS',
    'LANGUAGE_POLICY_VERSION',
    'CHECKPOINT_SCHEMA_VERSION',
    'DEFAULT_WORKERS',
    'DEFAULT_MAX_HOURS',
    'DEFAULT_TIMEOUT_SECONDS',
    'DEFAULT_MAX_ATTEMPTS',
    'MANIFEST_LANGUAGE_MAP',
    'SUPPORTED_MANIFEST_LANGUAGES',
    'InventoryError',
    'RuntimeBudgetExceeded',
    'GitHubError',
    'ClientStats',
    'utc_now',
    'isoformat_utc',
    'parse_github_timestamp',
    'normalize_language',
    'classify_activity',
    'manifest_languages',
    '_language_totals',
    'classify_languages',
    'exclusion_reason',
    '_language_connection',
    'needs_manifest_inspection',
    'build_inventory_record',
    'redact',
    'DISCOVERY_QUERY',
    'PREFLIGHT_QUERY',
    'ROOT_TREE_QUERY',
    'ONE_LEVEL_TREE_QUERY',
    'GitHubClient',
    'preflight',
    'discover_repositories',
    'inspect_manifest_paths',
    'bounded_inspections',
    'failure_record',
    'checkpoint_configuration',
    'initialize_checkpoint',
    'load_checkpoint',
    'validate_checkpoint',
    'CheckpointWriter',
    'atomic_write_jsonl',
    'reconcile',
    'emit_event',
    'output_directory',
    'projection_accuracy',
    'projected_seconds',
    'run_preflight',
    'run_inventory',
    'run_depth_benchmark',
    'positive_integer',
    'positive_float',
    'build_parser',
    'main',
]

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class GitHubSettings:
    organization: str
    rest_api_url: str
    graphql_url: str
    web_url: str


@dataclasses.dataclass(frozen=True)
class InventorySettings:
    inspection_depth: str
    workers: int
    max_hours: float
    timeout_seconds: float
    retries: int


@dataclasses.dataclass(frozen=True)
class PropertySettings:
    activity_name: str
    languages_name: str
    policy_name: str
    assignment_mode: str

    @property
    def managed_names(self) -> tuple[str, str, str]:
        return (
            self.activity_name,
            self.languages_name,
            self.policy_name,
        )


@dataclasses.dataclass(frozen=True)
class RepositoryOverride:
    repository: str
    result: str
    reason: str


@dataclasses.dataclass(frozen=True)
class PolicySettings:
    active_known: str
    inactive_known: str
    unknown: str
    fork: str
    repository_overrides: tuple[RepositoryOverride, ...]


@dataclasses.dataclass(frozen=True)
class WorkflowSettings:
    enabled: bool
    source_repository: str
    local_path: str
    path: str
    branch: str
    url_variable_name: str
    secret_name: str
    runner: str
    timeout_minutes: int
    action_repository: str
    action_commit_sha: str


@dataclasses.dataclass(frozen=True)
class OnboardingConfig:
    schema_version: int
    github: GitHubSettings
    inventory: InventorySettings
    properties: PropertySettings
    policy: PolicySettings
    workflow: WorkflowSettings
    source_path: Path
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class InventoryBundle:
    directory: Path
    inventory: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    checkpoint_metadata: dict[str, Any]
    checkpoint_states: dict[str, dict[str, Any]]
    sha256: str


@dataclasses.dataclass(frozen=True)
class RestStatistics:
    requests: int
    retries: int
    rate_remaining: int | None
    rate_reset_epoch: float | None

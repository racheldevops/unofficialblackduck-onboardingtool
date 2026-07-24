from __future__ import annotations

from typing import Any

from .errors import ArtifactError
from .models import OnboardingConfig, RepositoryOverride


def _overrides_by_name(
    overrides: tuple[RepositoryOverride, ...],
) -> dict[str, RepositoryOverride]:
    return {
        override.repository.casefold(): override
        for override in overrides
    }


def policy_result(
    repository: dict[str, Any],
    config: OnboardingConfig,
) -> tuple[str, str]:
    name = repository.get("name_with_owner")

    if not isinstance(name, str) or not name:
        raise ArtifactError(
            "Inventory repository has no name for policy calculation."
        )

    override = _overrides_by_name(
        config.policy.repository_overrides
    ).get(name.casefold())

    if override is not None:
        return override.result, override.reason

    if repository.get("is_fork") is True:
        return config.policy.fork, "fork"

    languages = repository.get("detected_languages")

    if languages == ["unknown"]:
        return config.policy.unknown, "unknown_language"

    activity = repository.get("activity_status")

    if activity == "inactive":
        return config.policy.inactive_known, "inactive_known"

    if activity == "active":
        return config.policy.active_known, "active_known"

    raise ArtifactError(
        f"Repository '{name}' has no supported policy classification."
    )


def desired_managed_values(
    repository: dict[str, Any],
    config: OnboardingConfig,
) -> tuple[dict[str, Any], str]:
    activity = repository.get("activity_status")
    languages = repository.get("detected_languages")

    if activity not in {"active", "inactive"}:
        raise ArtifactError(
            "Inventory repository has an invalid activity status."
        )

    if not isinstance(languages, list) or not languages:
        raise ArtifactError(
            "Inventory repository has no detected languages."
        )

    result, reason = policy_result(repository, config)

    return (
        {
            config.properties.activity_name: activity,
            config.properties.languages_name: list(languages),
            config.properties.policy_name: result,
        },
        reason,
    )

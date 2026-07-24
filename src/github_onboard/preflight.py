from __future__ import annotations

import time
from typing import Any

from .github_api import GitHubPropertiesAPI
from .models import OnboardingConfig
from .rest import GitHubRestClient


def run_preflight(
    config: OnboardingConfig,
    token: str,
    *,
    insecure: bool,
    transport: Any = None,
) -> dict[str, Any]:
    deadline = (
        time.monotonic()
        + config.inventory.max_hours * 3600.0
    )

    with GitHubRestClient(
        token,
        base_url=config.github.rest_api_url,
        timeout=config.inventory.timeout_seconds,
        max_attempts=config.inventory.retries,
        mutation_enabled=False,
        deadline=deadline,
        verify=not insecure,
        transport=transport,
    ) as client:
        api = GitHubPropertiesAPI(
            client,
            config.github.organization,
        )
        result = api.preflight()
        stats = client.stats()

    return {
        **result,
        "rest_api_url": config.github.rest_api_url,
        "graphql_url": config.github.graphql_url,
        "rest_requests": stats.requests,
        "rest_retries": stats.retries,
        "rest_rate_remaining": stats.rate_remaining,
        "rest_rate_reset_epoch": stats.rate_reset_epoch,
    }

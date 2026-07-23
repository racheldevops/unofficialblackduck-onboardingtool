from __future__ import annotations

import argparse
import dataclasses
import json
import time

from ..classification import redact
from ..errors import GitHubError, InventoryError
from ..github.client import GitHubClient
from ..github.repositories import preflight
from ..reporting import emit_event


def run_preflight(
    args: argparse.Namespace,
    organization: str,
    token: str,
) -> int:
    deadline = time.monotonic() + args.max_hours * 3600.0
    client = GitHubClient(
        token,
        timeout=args.timeout,
        max_attempts=args.retries,
        deadline=deadline,
        verify=not getattr(args, "insecure", False),
    )

    try:
        result = preflight(client, organization)
        result["client_stats"] = dataclasses.asdict(client.stats())
        print(json.dumps(result, sort_keys=True))

        if not result["viewer_can_administer"]:
            emit_event(
                "fatal_error",
                category="administration_required",
                message=(
                    "GITHUB_TOKEN cannot administer the organization; "
                    "Plan 1 requires organization administration capability."
                ),
            )
            return 2

        return 0
    except (InventoryError, GitHubError) as error:
        emit_event(
            "fatal_error",
            category=getattr(
                error,
                "category",
                type(error).__name__,
            ),
            message=redact(str(error), [token]),
        )
        return 2
    finally:
        client.close()

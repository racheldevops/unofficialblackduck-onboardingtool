from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture
def repository_factory() -> Callable[..., dict[str, Any]]:

    def factory(
        *,
        name: str = "repository",
        owner: str = "acme",
        pushed_at: str | None = "2025-01-15T12:00:00Z",
        languages: tuple[tuple[str, int], ...] | None = (
            ("Python", 100),
        ),
        total_size: int | None = None,
        disk_usage_kb: int = 0,
        **overrides: Any,
    ) -> dict[str, Any]:
        language_values = languages or ()
        edges = [
            {
                "size": size,
                "node": {"name": language},
            }
            for language, size in language_values
        ]

        if total_size is None:
            total_size = sum(size for _language, size in language_values)

        repository: dict[str, Any] = {
            "id": f"R_{owner}_{name}".replace("/", "_"),
            "nameWithOwner": f"{owner}/{name}",
            "url": f"https://github.com/{owner}/{name}",
            "visibility": "PRIVATE",
            "pushedAt": pushed_at,
            "isArchived": False,
            "isFork": False,
            "isTemplate": False,
            "diskUsage": disk_usage_kb,
            "defaultBranchRef": {"name": "main"},
            "languages": {
                "totalSize": total_size,
                "pageInfo": {"hasNextPage": False},
                "edges": edges,
            },
        }
        repository.update(overrides)
        return repository

    return factory

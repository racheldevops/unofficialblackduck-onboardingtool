from __future__ import annotations

import concurrent.futures
import hashlib
from typing import Any, Callable, Iterator, Sequence

from ..classification import parse_github_timestamp
from ..errors import GitHubError, InventoryError, RuntimeBudgetExceeded
from .client import GitHubClient
from .queries import (
    DISCOVERY_QUERY,
    ONE_LEVEL_TREE_QUERY,
    PREFLIGHT_QUERY,
    ROOT_TREE_QUERY,
)


def _invalid_response(message: str) -> GitHubError:
    return GitHubError(
        "invalid_response",
        message,
        attempts=1,
    )


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_response(f"GitHub field '{field}' must be nonempty.")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise _invalid_response(
            f"GitHub field '{field}' must be a nonnegative integer."
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise _invalid_response(f"GitHub field '{field}' must be boolean.")
    return value


def preflight(
    client: GitHubClient,
    organization: str,
) -> dict[str, Any]:
    data = client.graphql(
        PREFLIGHT_QUERY,
        {"organization": organization},
        operation="authentication preflight",
    )
    organization_data = data.get("organization")

    if not isinstance(organization_data, dict):
        raise GitHubError(
            "organization_not_found",
            (
                f"GITHUB_ORG '{organization}' is unavailable to "
                "GITHUB_TOKEN."
            ),
            attempts=1,
        )

    organization_login = _nonempty_string(
        organization_data.get("login"),
        "organization.login",
    )
    if organization_login.casefold() != organization.casefold():
        raise _invalid_response(
            "GitHub returned a different organization during preflight."
        )

    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        raise _invalid_response("GitHub viewer data must be an object.")
    viewer_login = _nonempty_string(
        viewer.get("login"),
        "viewer.login",
    )

    repositories = organization_data.get("repositories")
    if not isinstance(repositories, dict):
        raise _invalid_response(
            "GitHub organization repositories must be an object."
        )
    repository_count = _nonnegative_integer(
        repositories.get("totalCount"),
        "organization.repositories.totalCount",
    )
    viewer_can_administer = _boolean(
        organization_data.get("viewerCanAdminister"),
        "organization.viewerCanAdminister",
    )

    rate = data.get("rateLimit")
    if not isinstance(rate, dict):
        raise _invalid_response("GitHub rateLimit must be an object.")

    rate_limit = _nonnegative_integer(
        rate.get("limit"),
        "rateLimit.limit",
    )
    rate_remaining = _nonnegative_integer(
        rate.get("remaining"),
        "rateLimit.remaining",
    )
    rate_reset_at = _nonempty_string(
        rate.get("resetAt"),
        "rateLimit.resetAt",
    )

    try:
        parse_github_timestamp(rate_reset_at)
    except InventoryError as error:
        raise _invalid_response(
            "GitHub rateLimit.resetAt is malformed."
        ) from error

    return {
        "record_type": "preflight",
        "organization": organization_login,
        "viewer": viewer_login,
        "viewer_can_administer": viewer_can_administer,
        "visible_repository_count": repository_count,
        "graphql_rate_limit": rate_limit,
        "graphql_rate_remaining": rate_remaining,
        "graphql_rate_reset_at": rate_reset_at,
    }


def select_representative_repositories(
    repositories: Sequence[dict[str, Any]],
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = list(repositories)

    if limit is None or limit >= len(selected):
        return selected
    if type(limit) is not int or limit <= 0:
        raise InventoryError("Pilot limit must be a positive integer.")

    def ranking(repository: dict[str, Any]) -> tuple[str, str, str]:
        repository_id = repository.get("id")
        name = repository.get("nameWithOwner")
        identity = f"{repository_id!r}\0{name!r}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return digest, str(name).casefold(), str(repository_id)

    return sorted(selected, key=ranking)[:limit]


def discover_repositories(
    client: Any,
    organization: str,
    *,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    repositories: list[dict[str, Any]] = []
    cursor: str | None = None
    expected_total: int | None = None
    used_cursors: set[str] = set()
    repository_ids: set[str] = set()
    repository_names: set[str] = set()

    while True:
        data = client.graphql(
            DISCOVERY_QUERY,
            {
                "organization": organization,
                "cursor": cursor,
                "pageSize": 100,
            },
            operation="repository discovery",
        )

        organization_data = data.get("organization")
        if not isinstance(organization_data, dict):
            raise GitHubError(
                "organization_not_found",
                (
                    f"GITHUB_ORG '{organization}' is unavailable to "
                    "GITHUB_TOKEN."
                ),
                attempts=1,
            )

        connection = organization_data.get("repositories")
        if not isinstance(connection, dict):
            raise _invalid_response(
                "GitHub repository connection must be an object."
            )

        total_count = _nonnegative_integer(
            connection.get("totalCount"),
            "repositories.totalCount",
        )
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise GitHubError(
                "pagination_error",
                "Organization repository total changed during discovery.",
                attempts=1,
            )

        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise _invalid_response(
                "GitHub repository nodes must be a list."
            )

        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise _invalid_response(
                "GitHub repository pageInfo must be an object."
            )

        has_next = _boolean(
            page_info.get("hasNextPage"),
            "repositories.pageInfo.hasNextPage",
        )
        next_cursor = page_info.get("endCursor")

        if has_next and not nodes:
            raise GitHubError(
                "pagination_error",
                "GitHub returned an empty page that declared another page.",
                attempts=1,
            )

        for node in nodes:
            if not isinstance(node, dict):
                raise _invalid_response(
                    "GitHub repository node must be an object."
                )

            repository_id = node.get("id")
            if isinstance(repository_id, str) and repository_id:
                if repository_id in repository_ids:
                    raise GitHubError(
                        "pagination_error",
                        "GitHub returned a duplicate repository ID.",
                        attempts=1,
                    )
                repository_ids.add(repository_id)

            name = node.get("nameWithOwner")
            if isinstance(name, str) and name:
                normalized_name = name.casefold()
                if normalized_name in repository_names:
                    raise GitHubError(
                        "pagination_error",
                        "GitHub returned a duplicate repository name.",
                        attempts=1,
                    )
                repository_names.add(normalized_name)

            repositories.append(node)

        if progress:
            progress(len(repositories), expected_total)

        if not has_next:
            break

        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubError(
                "pagination_error",
                "GitHub indicated another page without an end cursor.",
                attempts=1,
            )

        if next_cursor in used_cursors:
            raise GitHubError(
                "pagination_error",
                "GitHub returned a repeated pagination cursor.",
                attempts=1,
            )

        used_cursors.add(next_cursor)
        cursor = next_cursor

    if expected_total is None:
        raise _invalid_response(
            "GitHub omitted the organization repository total."
        )

    if len(repositories) != expected_total:
        raise GitHubError(
            "pagination_error",
            (
                "Repository discovery count does not match the "
                "organization total."
            ),
            attempts=1,
        )

    return (
        select_representative_repositories(repositories, limit),
        expected_total,
    )


def _tree_entries(
    tree: dict[str, Any],
    context: str,
) -> list[dict[str, Any]]:
    if tree.get("__typename") != "Tree":
        raise _invalid_response(
            f"GitHub returned a non-tree object for {context}."
        )

    entries = tree.get("entries")
    if not isinstance(entries, list):
        raise _invalid_response(
            f"GitHub returned invalid tree entries for {context}."
        )

    validated: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            raise _invalid_response(
                f"GitHub returned a malformed tree entry for {context}."
            )

        _nonempty_string(entry.get("name"), "tree.entries.name")
        entry_type = _nonempty_string(
            entry.get("type"),
            "tree.entries.type",
        )

        if entry_type not in {"blob", "tree", "commit"}:
            raise _invalid_response(
                f"GitHub returned an unsupported tree entry for {context}."
            )

        validated.append(entry)

    return validated


def inspect_manifest_paths(
    client: GitHubClient,
    repository: dict[str, Any],
    depth: str,
) -> list[str]:
    if depth not in {"root", "one"}:
        raise InventoryError(f"Unsupported inspection depth: {depth}")

    name_with_owner = repository.get("nameWithOwner")
    if (
        not isinstance(name_with_owner, str)
        or name_with_owner.count("/") != 1
        or name_with_owner.startswith("/")
        or name_with_owner.endswith("/")
    ):
        raise InventoryError("Repository has no valid nameWithOwner.")

    default_branch_data = repository.get("defaultBranchRef")
    if default_branch_data is None:
        return []
    if not isinstance(default_branch_data, dict):
        raise InventoryError("Repository has an invalid default branch.")

    default_branch = default_branch_data.get("name")
    if not isinstance(default_branch, str) or not default_branch:
        raise InventoryError("Repository has an invalid default branch name.")

    owner, name = name_with_owner.split("/", 1)
    query = ROOT_TREE_QUERY if depth == "root" else ONE_LEVEL_TREE_QUERY

    data = client.graphql(
        query,
        {
            "owner": owner,
            "name": name,
            "expression": f"{default_branch}:",
        },
        operation=f"{depth} manifest inspection for {name_with_owner}",
    )

    repository_data = data.get("repository")
    if not isinstance(repository_data, dict):
        raise GitHubError(
            "repository_unavailable",
            f"Repository became unavailable: {name_with_owner}.",
            attempts=1,
        )

    tree = repository_data.get("object")
    if tree is None:
        return []
    if not isinstance(tree, dict):
        raise _invalid_response(
            f"GitHub returned an invalid tree for {name_with_owner}."
        )

    paths: list[str] = []

    for entry in _tree_entries(tree, name_with_owner):
        entry_name = entry["name"]
        paths.append(entry_name)

        if depth != "one" or entry["type"] != "tree":
            continue

        nested_tree = entry.get("object")
        if not isinstance(nested_tree, dict):
            raise _invalid_response(
                f"GitHub omitted a nested tree for {name_with_owner}."
            )

        for nested in _tree_entries(
            nested_tree,
            f"{name_with_owner}/{entry_name}",
        ):
            paths.append(f"{entry_name}/{nested['name']}")

    return sorted(set(paths))


def bounded_inspections(
    client: GitHubClient,
    repositories: Sequence[dict[str, Any]],
    *,
    depth: str,
    workers: int,
) -> Iterator[
    tuple[dict[str, Any], list[str] | None, Exception | None]
]:
    chunk_size = max(workers, workers * 2)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="github-inventory",
    )

    try:
        for offset in range(0, len(repositories), chunk_size):
            chunk = repositories[offset : offset + chunk_size]
            futures = {
                executor.submit(
                    inspect_manifest_paths,
                    client,
                    repository,
                    depth,
                ): repository
                for repository in chunk
            }

            for future in concurrent.futures.as_completed(futures):
                repository = futures[future]

                try:
                    yield repository, future.result(), None
                except RuntimeBudgetExceeded:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as error:
                    yield repository, None, error
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

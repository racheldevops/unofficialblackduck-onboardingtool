from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from .errors import InventoryError
from .settings import (
    MANIFEST_LANGUAGE_MAP,
    SUPPORTED_MANIFEST_LANGUAGES,
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def isoformat_utc(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InventoryError("UTC formatting requires a timezone-aware value.")
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def parse_github_timestamp(value: str | None) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InventoryError("GitHub timestamp must be a string or null.")

    text = value[:-1] + "+00:00" if value.endswith("Z") else value

    try:
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError) as error:
        raise InventoryError("Malformed GitHub timestamp.") from error

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InventoryError("GitHub timestamp must include a timezone.")

    return parsed.astimezone(dt.UTC)


def normalize_language(value: str) -> str:
    if not isinstance(value, str):
        raise InventoryError("Language name must be a string.")

    normalized = re.sub(r"\s+", "-", value.strip().casefold())

    if not normalized:
        raise InventoryError("Language name must not be empty.")

    return normalized


def classify_activity(
    pushed_at: str | None,
    cutoff: dt.datetime,
) -> str:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise InventoryError("Activity cutoff must include a timezone.")

    pushed = parse_github_timestamp(pushed_at)

    if pushed is None:
        return "inactive"

    return "active" if pushed >= cutoff else "inactive"


def manifest_languages(paths: Iterable[str]) -> set[str]:
    detected: set[str] = set()

    for path in paths:
        if not isinstance(path, str) or not path:
            raise InventoryError("Manifest path must be a nonempty string.")

        basename = path.rsplit("/", 1)[-1].casefold()
        detected.update(MANIFEST_LANGUAGE_MAP.get(basename, set()))

        if basename.endswith(".csproj") or basename.endswith(".sln"):
            detected.add("c#")

    return detected


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"Repository field '{field}' must be nonempty.")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise InventoryError(
            f"Repository field '{field}' must be a nonnegative integer."
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise InventoryError(f"Repository field '{field}' must be boolean.")
    return value


def repository_identity(
    repository: dict[str, Any],
) -> tuple[str, str]:
    if not isinstance(repository, dict):
        raise InventoryError("Repository metadata must be an object.")

    repository_id = _nonempty_string(repository.get("id"), "id")
    name_with_owner = _nonempty_string(
        repository.get("nameWithOwner"),
        "nameWithOwner",
    )

    if (
        name_with_owner.count("/") != 1
        or name_with_owner.startswith("/")
        or name_with_owner.endswith("/")
    ):
        raise InventoryError(
            "Repository field 'nameWithOwner' must contain owner/name."
        )

    return repository_id, name_with_owner


def _language_totals(
    language_edges: Sequence[dict[str, Any]],
) -> list[tuple[str, int]]:
    if not isinstance(language_edges, (list, tuple)):
        raise InventoryError("Language edges must be a list.")

    totals: dict[str, int] = {}

    for index, edge in enumerate(language_edges):
        if not isinstance(edge, dict):
            raise InventoryError(f"Language edge {index} must be an object.")

        node = edge.get("node")
        if not isinstance(node, dict):
            raise InventoryError(
                f"Language edge {index} must contain a language node."
            )

        name = normalize_language(node.get("name"))
        size = _nonnegative_integer(
            edge.get("size"),
            f"languages.edges[{index}].size",
        )

        if size > 0:
            totals[name] = totals.get(name, 0) + size

    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def classify_languages(
    language_edges: Sequence[dict[str, Any]],
    recognized_total_bytes: int | None,
    disk_usage_kb: int | None,
    manifest_paths: Iterable[str] = (),
) -> list[str]:
    ordered = _language_totals(language_edges)
    edge_total = sum(
        _nonnegative_integer(
            edge.get("size"),
            f"languages.edges[{index}].size",
        )
        for index, edge in enumerate(language_edges)
    )

    if recognized_total_bytes is not None:
        recognized_total = _nonnegative_integer(
            recognized_total_bytes,
            "languages.totalSize",
        )
        if recognized_total != edge_total:
            raise InventoryError(
                "Language totalSize does not match the complete edge data."
            )

    if disk_usage_kb is not None:
        _nonnegative_integer(disk_usage_kb, "diskUsage")

    if not ordered:
        return ["unknown"]

    qualifying = {ordered[0][0]}
    supported = manifest_languages(manifest_paths)

    for language, _size in ordered[1:]:
        if language in supported:
            qualifying.add(language)

    return sorted(qualifying)


def exclusion_reason(repository: dict[str, Any]) -> str | None:
    archived = _boolean(repository.get("isArchived"), "isArchived")
    template = _boolean(repository.get("isTemplate"), "isTemplate")

    if archived and template:
        return "archived_and_template"
    if archived:
        return "archived"
    if template:
        return "template"

    return None


def _language_connection(
    repository: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    connection = repository.get("languages")

    if not isinstance(connection, dict):
        raise InventoryError("Repository languages must be an object.")

    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict):
        raise InventoryError("Language pageInfo must be an object.")

    has_next_page = page_info.get("hasNextPage")
    if type(has_next_page) is not bool:
        raise InventoryError("Language hasNextPage must be boolean.")
    if has_next_page:
        raise InventoryError(
            "Language metadata is truncated after the first 100 entries."
        )

    edges = connection.get("edges")
    if not isinstance(edges, list):
        raise InventoryError("Language edges must be a list.")

    total_size = _nonnegative_integer(
        connection.get("totalSize"),
        "languages.totalSize",
    )
    edge_total = sum(
        _nonnegative_integer(
            edge.get("size") if isinstance(edge, dict) else None,
            f"languages.edges[{index}].size",
        )
        for index, edge in enumerate(edges)
    )

    _language_totals(edges)

    if total_size != edge_total:
        raise InventoryError(
            "Language totalSize does not match the complete edge data."
        )

    return edges, total_size


def validate_repository_metadata(
    repository: dict[str, Any],
) -> None:
    _repository_id, name_with_owner = repository_identity(repository)

    url = _nonempty_string(repository.get("url"), "url")
    parsed_url = urlsplit(url)

    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.path.rstrip("/") != f"/{name_with_owner}"
    ):
        raise InventoryError("Repository field 'url' is invalid.")

    visibility = _nonempty_string(
        repository.get("visibility"),
        "visibility",
    ).upper()
    if visibility not in {"PUBLIC", "PRIVATE", "INTERNAL"}:
        raise InventoryError("Repository field 'visibility' is invalid.")

    for field in ("isArchived", "isFork", "isTemplate"):
        _boolean(repository.get(field), field)

    default_branch = repository.get("defaultBranchRef")
    if default_branch is not None:
        if not isinstance(default_branch, dict):
            raise InventoryError(
                "Repository field 'defaultBranchRef' must be an object or null."
            )
        _nonempty_string(
            default_branch.get("name"),
            "defaultBranchRef.name",
        )

    pushed_at = repository.get("pushedAt")
    if pushed_at is not None:
        _nonempty_string(pushed_at, "pushedAt")
        parse_github_timestamp(pushed_at)

    disk_usage = repository.get("diskUsage")
    if disk_usage is not None:
        _nonnegative_integer(disk_usage, "diskUsage")

    _language_connection(repository)


def needs_manifest_inspection(repository: dict[str, Any]) -> bool:
    validate_repository_metadata(repository)

    if exclusion_reason(repository):
        return False

    default_branch = repository.get("defaultBranchRef")
    if default_branch is None:
        return False

    edges, _total = _language_connection(repository)
    ordered = _language_totals(edges)

    if len(ordered) < 2:
        return False

    return any(
        language in SUPPORTED_MANIFEST_LANGUAGES
        for language, _size in ordered[1:]
    )


def build_excluded_record(
    repository: dict[str, Any],
) -> dict[str, Any]:
    validate_repository_metadata(repository)
    reason = exclusion_reason(repository)

    if reason is None:
        raise InventoryError("Repository is not archived or a template.")

    return {
        "checkpoint_status": "excluded",
        "repository_id": repository["id"],
        "name_with_owner": repository["nameWithOwner"],
        "exclusion_reason": reason,
    }


def build_inventory_record(
    repository: dict[str, Any],
    cutoff: dt.datetime,
    manifest_paths: Iterable[str] = (),
) -> dict[str, Any]:
    validate_repository_metadata(repository)
    reason = exclusion_reason(repository)
    default_branch_data = repository.get("defaultBranchRef")
    default_branch = (
        default_branch_data["name"]
        if isinstance(default_branch_data, dict)
        else None
    )
    edges, total_size = _language_connection(repository)

    if reason:
        languages: list[str] = []
    else:
        languages = classify_languages(
            edges,
            total_size,
            repository.get("diskUsage"),
            manifest_paths,
        )

    record: dict[str, Any] = {
        "repository_id": repository["id"],
        "name_with_owner": repository["nameWithOwner"],
        "url": repository["url"],
        "visibility": repository["visibility"].casefold(),
        "default_branch": default_branch,
        "is_fork": repository["isFork"],
        "is_archived": repository["isArchived"],
        "is_template": repository["isTemplate"],
        "pushed_at": repository["pushedAt"],
        "activity_status": classify_activity(
            repository["pushedAt"],
            cutoff,
        ),
        "detected_languages": languages,
    }

    if reason:
        record["exclusion_reason"] = reason

    return record


def redact(value: str, secrets: Iterable[str]) -> str:
    redacted = value

    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    return redacted

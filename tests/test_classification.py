from __future__ import annotations

import datetime as dt

import pytest

from github_inventory.classification import (
    build_inventory_record,
    classify_activity,
    classify_languages,
    exclusion_reason,
    manifest_languages,
    needs_manifest_inspection,
    parse_github_timestamp,
    redact,
)
from github_inventory.errors import InventoryError


CUTOFF = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def test_parse_github_timestamp_normalizes_to_utc() -> None:
    parsed = parse_github_timestamp("2025-01-01T02:00:00+02:00")

    assert parsed == dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def test_parse_github_timestamp_accepts_missing_value() -> None:
    assert parse_github_timestamp(None) is None
    assert parse_github_timestamp("") is None


def test_invalid_timestamp_is_not_silently_classified() -> None:
    with pytest.raises((InventoryError, ValueError)):
        parse_github_timestamp("not-a-timestamp")


@pytest.mark.parametrize(
    ("pushed_at", "expected"),
    [
        ("2025-01-01T00:00:00Z", "active"),
        ("2025-01-01T00:00:01Z", "active"),
        ("2024-12-31T23:59:59Z", "inactive"),
        (None, "inactive"),
    ],
)
def test_activity_classification(
    pushed_at: str | None,
    expected: str,
) -> None:
    assert classify_activity(pushed_at, CUTOFF) == expected


def test_manifest_language_detection_is_case_insensitive() -> None:
    paths = [
        "service/PYPROJECT.TOML",
        "frontend/package.json",
        "dotnet/Example.CSPROJ",
        "docs/README.md",
    ]

    assert manifest_languages(paths) == {
        "python",
        "javascript",
        "typescript",
        "c#",
    }


def test_no_linguist_evidence_is_unknown() -> None:
    assert classify_languages([], 0, 0) == ["unknown"]


def test_primary_language_is_retained() -> None:
    edges = [
        {"size": 900, "node": {"name": "Python"}},
        {"size": 100, "node": {"name": "Java"}},
    ]

    assert classify_languages(edges, 1000, 0) == ["python"]


def test_manifest_backed_secondary_language_is_retained() -> None:
    edges = [
        {"size": 900, "node": {"name": "Python"}},
        {"size": 100, "node": {"name": "Java"}},
    ]

    assert classify_languages(
        edges,
        1000,
        0,
        manifest_paths=["pom.xml"],
    ) == ["java", "python"]


def test_language_results_are_stable_and_sorted() -> None:
    edges = [
        {"size": 700, "node": {"name": "JavaScript"}},
        {"size": 200, "node": {"name": "TypeScript"}},
        {"size": 100, "node": {"name": "Python"}},
    ]

    result = classify_languages(
        edges,
        1000,
        0,
        manifest_paths=[
            "package.json",
            "pyproject.toml",
        ],
    )

    assert result == ["javascript", "python", "typescript"]


@pytest.mark.parametrize(
    ("archived", "template", "expected"),
    [
        (False, False, None),
        (True, False, "archived"),
        (False, True, "template"),
        (True, True, "archived_and_template"),
    ],
)
def test_exclusion_reason(
    repository_factory,
    archived: bool,
    template: bool,
    expected: str | None,
) -> None:
    repository = repository_factory(
        isArchived=archived,
        isTemplate=template,
    )

    assert exclusion_reason(repository) == expected


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("isArchived", "archived"),
        ("isTemplate", "template"),
    ],
)
def test_excluded_repositories_skip_manifest_inspection(
    repository_factory,
    field: str,
    reason: str,
) -> None:
    repository = repository_factory(
        languages=(
            ("Python", 900),
            ("Java", 100),
        ),
        **{field: True},
    )

    assert exclusion_reason(repository) == reason
    assert needs_manifest_inspection(repository) is False


def test_secondary_language_candidate_requires_inspection(
    repository_factory,
) -> None:
    repository = repository_factory(
        languages=(
            ("Python", 900),
            ("Java", 100),
        )
    )

    assert needs_manifest_inspection(repository) is True


def test_repository_without_default_branch_skips_inspection(
    repository_factory,
) -> None:
    repository = repository_factory(
        languages=(
            ("Python", 900),
            ("Java", 100),
        ),
        defaultBranchRef=None,
    )

    assert needs_manifest_inspection(repository) is False


def test_inventory_record_preserves_fork_and_activity(
    repository_factory,
) -> None:
    repository = repository_factory(
        name="forked",
        isFork=True,
        pushed_at="2025-01-01T00:00:00Z",
    )

    record = build_inventory_record(repository, CUTOFF)

    assert record["name_with_owner"] == "acme/forked"
    assert record["is_fork"] is True
    assert record["activity_status"] == "active"
    assert record["detected_languages"] == ["python"]
    assert record["visibility"] == "private"


def test_archived_inventory_record_has_no_language_classification(
    repository_factory,
) -> None:
    repository = repository_factory(
        name="archived",
        isArchived=True,
    )

    record = build_inventory_record(repository, CUTOFF)

    assert record["exclusion_reason"] == "archived"
    assert record["detected_languages"] == []


def test_redact_removes_every_supplied_secret() -> None:
    message = "token-one was followed by token-two"

    assert redact(
        message,
        ["token-one", "token-two"],
    ) == "[REDACTED] was followed by [REDACTED]"

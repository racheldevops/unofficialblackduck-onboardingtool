from __future__ import annotations

import argparse
import importlib
import json

import pytest

from github_inventory.cli import (
    build_parser,
    main,
    positive_float,
    positive_integer,
)
from github_inventory.reporting import (
    emit_event,
    projected_seconds,
    projection_accuracy,
)
from github_inventory.settings import DEFAULT_WORKERS


cli_module = importlib.import_module("github_inventory.cli")


def test_parser_uses_bounded_worker_default() -> None:
    arguments = build_parser().parse_args([])

    assert arguments.workers == DEFAULT_WORKERS
    assert arguments.limit is None
    assert arguments.inspection_depth is None


def test_parser_accepts_inventory_options() -> None:
    arguments = build_parser().parse_args(
        [
            "--workers",
            "4",
            "--limit",
            "25",
            "--inspection-depth",
            "root",
            "--max-hours",
            "1.5",
        ]
    )

    assert arguments.workers == 4
    assert arguments.limit == 25
    assert arguments.inspection_depth == "root"
    assert arguments.max_hours == 1.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_positive_integer_rejects_nonpositive_value(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_integer(value)


@pytest.mark.parametrize("value", ["0", "-0.1"])
def test_positive_float_rejects_nonpositive_value(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_float(value)


def test_main_requires_environment_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GITHUB_ORG", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(SystemExit) as captured:
        main([])

    assert captured.value.code == 2


def test_main_dispatches_preflight_without_network_access(
    monkeypatch,
) -> None:
    received: dict[str, object] = {}

    def fake_run_preflight(arguments, organization, token) -> int:
        received["arguments"] = arguments
        received["organization"] = organization
        received["token"] = token
        return 17

    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        cli_module,
        "run_preflight",
        fake_run_preflight,
    )

    result = main(["--preflight"])

    assert result == 17
    assert received["organization"] == "acme"
    assert received["token"] == "test-token"


def test_depth_benchmark_requires_explicit_limit(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    with pytest.raises(SystemExit) as captured:
        main(["--benchmark-depth"])

    assert captured.value.code == 2


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        (0, "low"),
        (9, "low"),
        (10, "medium"),
        (49, "medium"),
        (50, "high"),
    ],
)
def test_projection_accuracy(
    samples: int,
    expected: str,
) -> None:
    assert projection_accuracy(samples) == expected


def test_runtime_projection() -> None:
    assert projected_seconds(2.0, 2, 10.0) == 10.0
    assert projected_seconds(2.0, 0, 10.0) is None


def test_emit_event_writes_json_to_standard_error(capsys) -> None:
    emit_event(
        "progress",
        completed=5,
        total=10,
    )

    output = capsys.readouterr()
    event = json.loads(output.err)

    assert output.out == ""
    assert event == {
        "record_type": "progress",
        "completed": 5,
        "total": 10,
    }

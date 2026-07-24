from __future__ import annotations

import json
import tomllib
from pathlib import Path

from github_onboard.cli import build_parser, main
from github_onboard.config import load_config


def test_parser_uses_standard_workspace() -> None:
    arguments = build_parser().parse_args(
        ["properties"]
    )

    assert arguments.workspace == Path(".inventory")
    assert arguments.config is None
    assert arguments.apply is False
    assert arguments.refresh_all is False


def test_parser_accepts_workspace_and_config_overrides(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    config = tmp_path / "configuration.toml"
    arguments = build_parser().parse_args(
        [
            "--workspace",
            str(workspace),
            "--config",
            str(config),
            "properties",
            "--refresh-all",
        ]
    )

    assert arguments.workspace == workspace
    assert arguments.config == config
    assert arguments.refresh_all is True
    assert arguments.apply is False


def test_init_is_local_and_does_not_require_token(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ORG", raising=False)
    monkeypatch.chdir(tmp_path)
    workspace = Path(".inventory")

    result = main(
        [
            "--workspace",
            str(workspace),
            "init",
            "--organization",
            "acme",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    config_path = (
        tmp_path / "config" / "onboarding.toml"
    )

    assert result == 0
    assert output["network_requests"] == 0
    assert config_path.is_file()
    assert load_config(config_path).github.organization == (
        "acme"
    )


def test_inventory_parser_uses_standard_output_directory() -> None:
    from github_inventory.cli import build_parser as inventory_parser

    arguments = inventory_parser().parse_args([])

    assert arguments.output_dir == Path(
        ".inventory/inventory"
    )


def test_pyproject_registers_both_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads(
        (root / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    scripts = configuration["project"]["scripts"]

    assert scripts["github-inventory"] == (
        "github_inventory.cli:main"
    )
    assert scripts["github-onboard"] == (
        "github_onboard.cli:main"
    )

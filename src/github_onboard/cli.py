from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import (
    configured_environment_organization,
    initialize_config,
    load_config,
)
from .errors import OnboardError
from .models import OnboardingConfig
from .preflight import run_preflight
from .properties import run_properties
from .workflow import run_workflow
from .workspace import DEFAULT_WORKSPACE, Workspace


def emit_event(
    record_type: str,
    **fields: Any,
) -> None:
    print(
        json.dumps(
            {
                **fields,
                "record_type": record_type,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )


def redact(value: str, token: str) -> str:
    if not token:
        return value

    return value.replace(token, "[REDACTED]")


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Value must be an integer."
        ) from error

    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            "Value must be greater than zero."
        )

    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish GitHub inventory classifications as "
            "organization custom properties."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="Operational workspace (default: .inventory).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Configuration file. Defaults to "
            "config/onboarding.toml."
        ),
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    init_parser = commands.add_parser(
        "init",
        help="Create the default onboarding configuration.",
    )
    init_parser.add_argument(
        "--organization",
        help="GitHub organization. Defaults to GITHUB_ORG.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing configuration.",
    )

    preflight_parser = commands.add_parser(
        "preflight",
        help="Read GitHub identity and custom-property capability.",
    )
    preflight_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )

    properties_parser = commands.add_parser(
        "properties",
        help=(
            "Run fresh inventory and plan or apply custom properties."
        ),
    )
    properties_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the calculated custom-property changes.",
    )
    properties_parser.add_argument(
        "--refresh-all",
        action="store_true",
        help=(
            "Replace existing managed property values with "
            "current inventory-derived values."
        ),
    )
    properties_parser.add_argument(
        "--limit",
        type=positive_integer,
        help=(
            "Process a deterministic sample of up to this many "
            "repositories, or cap an explicit repository list."
        ),
    )
    properties_parser.add_argument(
        "--repository",
        action="append",
        default=[],
        metavar="OWNER/REPOSITORY",
        help=(
            "Process exactly this repository. Repeat for multiple "
            "repositories."
        ),
    )
    properties_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )

    workflow_parser = commands.add_parser(
        "workflow",
        help=(
            "Plan or publish the central Black Duck workflow."
        ),
    )
    workflow_parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish the calculated workflow change.",
    )
    workflow_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )

    return parser


def selected_workspace(
    args: argparse.Namespace,
) -> Workspace:
    return Workspace.from_root(
        args.workspace,
        args.config,
    )


def selected_configuration(
    args: argparse.Namespace,
) -> tuple[Workspace, OnboardingConfig]:
    workspace = selected_workspace(args)
    config = load_config(
        workspace.config_path,
        environment_organization=(
            configured_environment_organization()
        ),
    )
    return workspace, config


def required_token(
    parser: argparse.ArgumentParser,
) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if not token:
        parser.error("GITHUB_TOKEN must be set.")

    return token


def emit_insecure_warning() -> None:
    emit_event(
        "insecure_tls_warning",
        message=(
            "TLS certificate verification is disabled. "
            "GitHub server identity cannot be verified, "
            "and GITHUB_TOKEN could be exposed."
        ),
    )


def run_init_command(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> int:
    workspace = selected_workspace(args)
    organization = (
        args.organization
        or configured_environment_organization()
    )

    if not organization:
        parser.error(
            "Set GITHUB_ORG or provide --organization for init."
        )

    path = initialize_config(
        workspace.config_path,
        organization,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "record_type": "configuration_initialized",
                "configuration": str(path),
                "workspace": str(workspace.root),
                "organization": organization,
                "network_requests": 0,
            },
            sort_keys=True,
        )
    )
    return 0


def run_preflight_command(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> int:
    _workspace, config = selected_configuration(args)
    token = required_token(parser)

    if args.insecure:
        emit_insecure_warning()

    result = run_preflight(
        config,
        token,
        insecure=args.insecure,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def selected_property_repositories(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    config: OnboardingConfig,
) -> tuple[str, ...]:
    repositories = tuple(args.repository)
    normalized: set[str] = set()

    for repository in repositories:
        if (
            repository.count("/") != 1
            or repository.startswith("/")
            or repository.endswith("/")
        ):
            parser.error(
                "--repository must use OWNER/REPOSITORY."
            )

        owner, name = repository.split("/", 1)

        if (
            not owner
            or not name
            or owner.casefold()
            != config.github.organization.casefold()
        ):
            parser.error(
                "--repository must belong to the configured "
                "organization."
            )

        key = repository.casefold()

        if key in normalized:
            parser.error(
                f"Duplicate --repository: {repository}"
            )

        normalized.add(key)

    if (
        args.limit is not None
        and len(repositories) > args.limit
    ):
        parser.error(
            "Explicit --repository count exceeds --limit."
        )

    return repositories


def run_properties_command(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> int:
    workspace, config = selected_configuration(args)
    repositories = selected_property_repositories(
        parser,
        args,
        config,
    )
    token = required_token(parser)

    if args.insecure:
        emit_insecure_warning()

    result, output_directory = run_properties(
        config,
        workspace,
        token,
        apply=args.apply,
        refresh_all=args.refresh_all,
        insecure=args.insecure,
        limit=args.limit,
        repositories=repositories,
    )
    print(
        json.dumps(
            {
                "record_type": "properties_complete",
                "result": result,
                "mode": (
                    "apply"
                    if args.apply
                    else "dry_run"
                ),
                "refresh_all": args.refresh_all,
                "limit": args.limit,
                "repositories": list(repositories),
                "output_directory": str(output_directory),
            },
            sort_keys=True,
        )
    )
    return result


def run_workflow_command(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> int:
    workspace, config = selected_configuration(args)
    token = required_token(parser)

    if args.insecure:
        emit_insecure_warning()

    result, output_directory = run_workflow(
        config,
        workspace,
        token,
        apply=args.apply,
        insecure=args.insecure,
    )
    print(
        json.dumps(
            {
                "record_type": "workflow_complete",
                "result": result,
                "mode": (
                    "apply"
                    if args.apply
                    else "dry_run"
                ),
                "output_directory": str(output_directory),
            },
            sort_keys=True,
        )
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")

    try:
        if args.command == "init":
            return run_init_command(parser, args)

        if args.command == "preflight":
            return run_preflight_command(parser, args)

        if args.command == "properties":
            return run_properties_command(parser, args)

        if args.command == "workflow":
            return run_workflow_command(parser, args)

        parser.error(f"Unsupported command: {args.command}")
    except KeyboardInterrupt:
        emit_event(
            "interrupted",
            message="Operation interrupted.",
        )
        return 130
    except OnboardError as error:
        emit_event(
            "fatal_error",
            category=error.category,
            message=redact(str(error), token),
        )
        return error.exit_code
    except Exception as error:
        emit_event(
            "fatal_error",
            category=type(error).__name__,
            message=redact(str(error), token),
        )
        return 2

    return 2

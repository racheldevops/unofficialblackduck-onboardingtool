from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from .classification import *
from .errors import *
from .reporting import *
from .settings import *
from .workflows import *


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a read-only JSON Lines inventory of repositories "
            "visible in GITHUB_ORG."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Artifact directory. Defaults to output/$GITHUB_ORG. "
            "--dry-run always uses a retained temporary directory."
        ),
    )
    parser.add_argument(
        "--workers",
        type=positive_integer,
        default=DEFAULT_WORKERS,
        help=f"Bounded manifest-inspection workers (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--limit",
        type=positive_integer,
        help="Process only this many repositories for a pilot.",
    )
    parser.add_argument(
        "--inspection-depth",
        choices=("root", "one"),
        help=(
            "Approved manifest inspection depth. Run --benchmark-depth "
            "before choosing it."
        ),
    )
    parser.add_argument(
        "--benchmark-depth",
        action="store_true",
        help="Benchmark root and root-plus-one-level inspection.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate authentication and organization access only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Perform the complete read-only inventory but write artifacts "
            "to a retained temporary directory."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume using the checkpoint in the output directory.",
    )
    parser.add_argument(
        "--discard-checkpoint",
        action="store_true",
        help="Discard an existing checkpoint before starting.",
    )
    parser.add_argument(
        "--max-hours",
        type=positive_float,
        default=DEFAULT_MAX_HOURS,
        help=(
            f"Actual and projected runtime limit "
            f"(default: {DEFAULT_MAX_HOURS:g})."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"Per-request timeout seconds "
            f"(default: {DEFAULT_TIMEOUT_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--retries",
        type=positive_integer,
        default=DEFAULT_MAX_ATTEMPTS,
        help=(
            f"Maximum attempts per API operation "
            f"(default: {DEFAULT_MAX_ATTEMPTS})."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "Disable TLS certificate verification. Unsafe; prefer "
            "SSL_CERT_FILE with the corporate CA certificate."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Reserved for additional diagnostic output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    organization = os.environ.get("GITHUB_ORG", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if not organization:
        parser.error("GITHUB_ORG must be set.")
    if not token:
        parser.error("GITHUB_TOKEN must be set.")

    mode_count = int(args.preflight) + int(args.benchmark_depth)
    if mode_count > 1:
        parser.error("--preflight and --benchmark-depth are exclusive.")

    if args.insecure:
        emit_event(
            "insecure_tls_warning",
            message=(
                "TLS certificate verification is disabled. GitHub server "
                "identity cannot be verified, and GITHUB_TOKEN could be "
                "exposed."
            ),
        )
    if args.preflight:
        if args.resume or args.discard_checkpoint:
            parser.error(
                "Checkpoint options cannot be used with --preflight."
            )
        return run_preflight(args, organization, token)

    if args.benchmark_depth:
        if args.limit is None:
            parser.error(
                "--benchmark-depth requires an explicit --limit."
            )
        if args.inspection_depth:
            parser.error(
                "--inspection-depth is not used with --benchmark-depth."
            )
        if args.resume or args.discard_checkpoint:
            parser.error(
                "Checkpoint options cannot be used with "
                "--benchmark-depth."
            )
        return run_depth_benchmark(args, organization, token)

    if not args.inspection_depth:
        parser.error(
            "--inspection-depth is required after reviewing the depth "
            "benchmark."
        )

    if args.dry_run and args.resume:
        parser.error(
            "--dry-run cannot resume because it creates a new temporary "
            "output directory."
        )

    try:
        return run_inventory(args, organization, token)
    except KeyboardInterrupt:
        emit_event(
            "interrupted",
            message="Interrupted; use --resume with the same output directory.",
        )
        return 130
    except InventoryError as error:
        emit_event(
            "fatal_error",
            category=type(error).__name__,
            message=redact(str(error), [token]),
        )
        return 2
    except Exception as error:
        emit_event(
            "fatal_error",
            category=type(error).__name__,
            message=redact(str(error), [token]),
        )
        return 2

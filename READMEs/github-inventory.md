# GitHub Repository Inventory

## Setup

Activate the project virtual environment and install the package:

    source .venv/bin/activate
    python -m pip install -e '.[test]'

Set authentication without placing the token in shell history:

    export GITHUB_ORG='your-organization'
    read -rs 'GITHUB_TOKEN?GitHub token: '
    export GITHUB_TOKEN
    print

## Validate

    python -m pytest -q
    python -m github_inventory --preflight

## Benchmark

Measure root and one-level inspection before selecting a mode:

    python -m github_inventory --benchmark-depth --limit 50 --max-hours 2

Review the reported recommendation before continuing.

## Pilot

Replace root with one if selected by the benchmark:

    python -m github_inventory --limit 50 --inspection-depth root --output-dir output/pilot

Review the summary, failures, projected duration, and reconciliation result.

## Full inventory

    python -m github_inventory --inspection-depth root --output-dir output/full

Resume an interrupted run using the same options and output directory:

    python -m github_inventory --inspection-depth root --output-dir output/full --resume

## Output

The output directory contains:

- inventory.jsonl
- failures.jsonl
- summary.jsonl
- checkpoint.jsonl

A nonzero exit means the run failed, exceeded its runtime budget, or retained unresolved repository failures.

The tool is read-only. Never pass tokens as command-line arguments or commit generated output.

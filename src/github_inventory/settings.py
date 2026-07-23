from __future__ import annotations


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


ACTIVITY_DAYS = 180


ACTIVITY_POLICY_VERSION = "pushed-at-utc-180-days-v1"


LANGUAGE_POLICY_VERSION = (
    "linguist-primary-manifest-secondary-exact-unknown-v2"
)


CHECKPOINT_SCHEMA_VERSION = 2


INVENTORY_INSPECTION_DEPTH = "root"


PILOT_SELECTION_METHOD = "sha256-ranked-complete-discovery-v1"


DEFAULT_WORKERS = 16


DEFAULT_MAX_HOURS = 2.0


DEFAULT_TIMEOUT_SECONDS = 30.0


DEFAULT_MAX_ATTEMPTS = 4


MANIFEST_LANGUAGE_MAP: dict[str, set[str]] = {
    "pyproject.toml": {"python"},
    "requirements.txt": {"python"},
    "pipfile": {"python"},
    "package.json": {"javascript", "typescript"},
    "pom.xml": {"java"},
    "build.gradle": {"groovy", "java", "kotlin"},
    "build.gradle.kts": {"java", "kotlin"},
    "go.mod": {"go"},
    "cargo.toml": {"rust"},
    "gemfile": {"ruby"},
    "composer.json": {"php"},
    "package.swift": {"swift"},
}


SUPPORTED_MANIFEST_LANGUAGES = frozenset(
    language
    for languages in MANIFEST_LANGUAGE_MAP.values()
    for language in languages
) | {"c#"}

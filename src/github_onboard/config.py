from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigurationError
from .models import (
    GitHubSettings,
    InventorySettings,
    OnboardingConfig,
    PolicySettings,
    PropertySettings,
    RepositoryOverride,
    RulesetSettings,
    WorkflowSettings,
)
from .workspace import atomic_write_text


CONFIG_SCHEMA_VERSION = 1
POLICY_VALUES = frozenset({"required", "excluded", "review"})
PROPERTY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,75}$")
METADATA_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_default_config(organization: str) -> str:
    selected_organization = _organization(
        organization,
        "github.organization",
    )

    return "\n".join(
        (
            f"schema_version = {CONFIG_SCHEMA_VERSION}",
            "",
            "[github]",
            (
                "organization = "
                f"{_toml_string(selected_organization)}"
            ),
            'rest_api_url = "https://api.github.com"',
            'graphql_url = "https://api.github.com/graphql"',
            'web_url = "https://github.com"',
            "",
            "[inventory]",
            'inspection_depth = "root"',
            "workers = 16",
            "max_hours = 2.0",
            "timeout_seconds = 30.0",
            "retries = 4",
            "",
            "[properties]",
            'activity_name = "blackduck_activity"',
            'languages_name = "blackduck_languages"',
            'policy_name = "blackduck_sca_policy"',
            'assignment_mode = "initialize_only"',
            "",
            "[policy]",
            'active_known = "required"',
            'inactive_known = "review"',
            'unknown = "review"',
            'fork = "review"',
            "",
            "[workflow]",
            "enabled = false",
            'source_repository = ""',
            'local_path = "workflows/blackduck-required.yml"',
            'path = ".github/workflows/blackduck.yml"',
            'branch = ""',
            'url_variable_name = "BLACKDUCK_URL"',
            'secret_name = "BLACKDUCK_API_TOKEN"',
            'runner = ""',
            "timeout_minutes = 0",
            'action_repository = ""',
            'action_commit_sha = ""',
            "",
            "[ruleset]",
            "enabled = false",
            'name = ""',
            'enforcement = "evaluate"',
            'include_policy_value = "required"',
            "",
        )
    )


def initialize_config(
    path: Path,
    organization: str,
    *,
    force: bool = False,
) -> Path:
    if path.exists() and not force:
        raise ConfigurationError(
            f"Configuration already exists: {path}"
        )

    atomic_write_text(path, render_default_config(organization))
    return path


def _table(
    value: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    selected = value.get(key)

    if not isinstance(selected, dict):
        raise ConfigurationError(
            f"Configuration section [{key}] is required."
        )

    return selected


def _string(
    table: dict[str, Any],
    key: str,
    field: str,
) -> str:
    value = table.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"Configuration field '{field}' must be a nonempty string."
        )

    return value.strip()


def _optional_string(
    table: dict[str, Any],
    key: str,
    field: str,
    *,
    default: str = "",
) -> str:
    value = table.get(key, default)

    if not isinstance(value, str):
        raise ConfigurationError(
            f"Configuration field '{field}' must be a string."
        )

    return value.strip()


def _boolean_value(
    table: dict[str, Any],
    key: str,
    field: str,
) -> bool:
    value = table.get(key)

    if type(value) is not bool:
        raise ConfigurationError(
            f"Configuration field '{field}' must be boolean."
        )

    return value


def _nonnegative_integer(
    table: dict[str, Any],
    key: str,
    field: str,
) -> int:
    value = table.get(key)

    if type(value) is not int or value < 0:
        raise ConfigurationError(
            f"Configuration field '{field}' must be a "
            "nonnegative integer."
        )

    return value


def _positive_integer(
    table: dict[str, Any],
    key: str,
    field: str,
) -> int:
    value = table.get(key)

    if type(value) is not int or value <= 0:
        raise ConfigurationError(
            f"Configuration field '{field}' must be a positive integer."
        )

    return value


def _positive_number(
    table: dict[str, Any],
    key: str,
    field: str,
) -> float:
    value = table.get(key)

    if type(value) not in {int, float} or value <= 0:
        raise ConfigurationError(
            f"Configuration field '{field}' must be greater than zero."
        )

    return float(value)


def _organization(value: str, field: str) -> str:
    selected = value.strip()

    if (
        not selected
        or "/" in selected
        or any(character.isspace() for character in selected)
    ):
        raise ConfigurationError(
            f"Configuration field '{field}' is not a valid organization."
        )

    return selected


def _url(
    value: str,
    field: str,
    *,
    strip_trailing_slash: bool,
) -> str:
    parsed = urlsplit(value)

    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"Configuration field '{field}' must be an HTTPS URL."
        )

    if strip_trailing_slash:
        return value.rstrip("/")

    return value


def _property_name(value: str, field: str) -> str:
    if PROPERTY_NAME_PATTERN.fullmatch(value) is None:
        raise ConfigurationError(
            f"Configuration field '{field}' is not a valid property name."
        )

    return value


def _policy_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or value not in POLICY_VALUES:
        choices = ", ".join(sorted(POLICY_VALUES))
        raise ConfigurationError(
            f"Configuration field '{field}' must be one of: {choices}."
        )

    return value


def _load_overrides(
    policy: dict[str, Any],
    organization: str,
) -> tuple[RepositoryOverride, ...]:
    raw = policy.get("repository_overrides", [])

    if not isinstance(raw, list):
        raise ConfigurationError(
            "Configuration field "
            "'policy.repository_overrides' must be an array of tables."
        )

    overrides: list[RepositoryOverride] = []
    names: set[str] = set()

    for index, item in enumerate(raw):
        prefix = f"policy.repository_overrides[{index}]"

        if not isinstance(item, dict):
            raise ConfigurationError(
                f"Configuration field '{prefix}' must be a table."
            )

        repository = _string(
            item,
            "repository",
            f"{prefix}.repository",
        )
        reason = _string(
            item,
            "reason",
            f"{prefix}.reason",
        )
        result = _policy_value(
            item.get("result"),
            f"{prefix}.result",
        )

        if (
            repository.count("/") != 1
            or repository.startswith("/")
            or repository.endswith("/")
        ):
            raise ConfigurationError(
                f"Configuration field '{prefix}.repository' "
                "must use owner/name."
            )

        owner, _name = repository.split("/", 1)

        if owner.casefold() != organization.casefold():
            raise ConfigurationError(
                f"Repository override '{repository}' is outside "
                f"organization '{organization}'."
            )

        normalized = repository.casefold()

        if normalized in names:
            raise ConfigurationError(
                f"Duplicate repository override: {repository}"
            )

        names.add(normalized)
        overrides.append(
            RepositoryOverride(
                repository=repository,
                result=result,
                reason=reason,
            )
        )

    return tuple(overrides)


def _workflow_settings(
    table: dict[str, Any],
    organization: str,
) -> WorkflowSettings:
    enabled = _boolean_value(
        table,
        "enabled",
        "workflow.enabled",
    )
    source_repository = _optional_string(
        table,
        "source_repository",
        "workflow.source_repository",
    )
    local_path = _optional_string(
        table,
        "local_path",
        "workflow.local_path",
        default="workflows/blackduck-required.yml",
    )
    destination_path = _optional_string(
        table,
        "path",
        "workflow.path",
    )
    branch = _optional_string(
        table,
        "branch",
        "workflow.branch",
    )
    url_variable_name = _optional_string(
        table,
        "url_variable_name",
        "workflow.url_variable_name",
        default="BLACKDUCK_URL",
    )
    secret_name = _optional_string(
        table,
        "secret_name",
        "workflow.secret_name",
        default="BLACKDUCK_API_TOKEN",
    )
    runner = _optional_string(
        table,
        "runner",
        "workflow.runner",
    )
    timeout_minutes = _nonnegative_integer(
        table,
        "timeout_minutes",
        "workflow.timeout_minutes",
    )
    action_repository = _optional_string(
        table,
        "action_repository",
        "workflow.action_repository",
    )
    action_commit_sha = _optional_string(
        table,
        "action_commit_sha",
        "workflow.action_commit_sha",
    ).casefold()

    settings = WorkflowSettings(
        enabled=enabled,
        source_repository=source_repository,
        local_path=local_path,
        path=destination_path,
        branch=branch,
        url_variable_name=url_variable_name,
        secret_name=secret_name,
        runner=runner,
        timeout_minutes=timeout_minutes,
        action_repository=action_repository,
        action_commit_sha=action_commit_sha,
    )

    if not enabled:
        return settings

    if (
        source_repository.count("/") != 1
        or source_repository.startswith("/")
        or source_repository.endswith("/")
    ):
        raise ConfigurationError(
            "Configuration field 'workflow.source_repository' "
            "must use owner/name."
        )

    source_owner, _source_name = source_repository.split("/", 1)

    if source_owner.casefold() != organization.casefold():
        raise ConfigurationError(
            "Workflow source repository must belong to the "
            "configured organization."
        )

    local = Path(local_path)

    if (
        not local_path
        or local.is_absolute()
        or ".." in local.parts
        or local.suffix.casefold() not in {".yml", ".yaml"}
    ):
        raise ConfigurationError(
            "Configuration field 'workflow.local_path' must be "
            "a YAML filename in the configuration directory."
        )

    destination = Path(destination_path)

    if (
        not destination_path
        or destination.is_absolute()
        or ".." in destination.parts
        or len(destination.parts) < 3
        or destination.parts[:2]
        != (".github", "workflows")
        or destination.suffix.casefold()
        not in {".yml", ".yaml"}
    ):
        raise ConfigurationError(
            "Configuration field 'workflow.path' must be a "
            "relative .github/workflows YAML path."
        )

    if not branch or any(
        character.isspace()
        for character in branch
    ):
        raise ConfigurationError(
            "Configuration field 'workflow.branch' is invalid."
        )

    for value, field in (
        (
            url_variable_name,
            "workflow.url_variable_name",
        ),
        (
            secret_name,
            "workflow.secret_name",
        ),
    ):
        if METADATA_NAME_PATTERN.fullmatch(value) is None:
            raise ConfigurationError(
                f"Configuration field '{field}' is not a valid "
                "GitHub Actions metadata name."
            )

    if not runner:
        raise ConfigurationError(
            "Configuration field 'workflow.runner' "
            "must be nonempty."
        )

    if timeout_minutes <= 0:
        raise ConfigurationError(
            "Configuration field 'workflow.timeout_minutes' "
            "must be positive when workflow is enabled."
        )

    if (
        action_repository.count("/") != 1
        or action_repository.startswith("/")
        or action_repository.endswith("/")
    ):
        raise ConfigurationError(
            "Configuration field "
            "'workflow.action_repository' must use owner/name."
        )

    if COMMIT_SHA_PATTERN.fullmatch(
        action_commit_sha
    ) is None:
        raise ConfigurationError(
            "Configuration field "
            "'workflow.action_commit_sha' must be a full "
            "lowercase commit SHA."
        )

    return settings


def _ruleset_settings(
    table: dict[str, Any],
    workflow: WorkflowSettings,
) -> RulesetSettings:
    enabled = _boolean_value(
        table,
        "enabled",
        "ruleset.enabled",
    )
    name = _optional_string(
        table,
        "name",
        "ruleset.name",
    )
    enforcement = _optional_string(
        table,
        "enforcement",
        "ruleset.enforcement",
        default="evaluate",
    )
    include_policy_value = _optional_string(
        table,
        "include_policy_value",
        "ruleset.include_policy_value",
        default="required",
    )

    settings = RulesetSettings(
        enabled=enabled,
        name=name,
        enforcement=enforcement,
        include_policy_value=include_policy_value,
    )

    if not enabled:
        return settings

    if not workflow.enabled:
        raise ConfigurationError(
            "Ruleset automation requires workflow.enabled = true."
        )

    if not name or len(name) > 100:
        raise ConfigurationError(
            "Configuration field 'ruleset.name' must contain "
            "between 1 and 100 characters."
        )

    if enforcement not in {"disabled", "evaluate"}:
        raise ConfigurationError(
            "Configuration field 'ruleset.enforcement' must be "
            "'disabled' or 'evaluate'. Activation is a separate command."
        )

    if include_policy_value not in POLICY_VALUES:
        raise ConfigurationError(
            "Configuration field 'ruleset.include_policy_value' "
            "must be required, review, or excluded."
        )

    return settings


def load_config(
    path: Path,
    *,
    environment_organization: str | None = None,
) -> OnboardingConfig:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(
            f"Unable to read configuration: {path}"
        ) from error

    text = raw.decode("utf-8")

    if "github_token" in text.casefold():
        raise ConfigurationError(
            "Configuration must not contain GITHUB_TOKEN."
        )

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"Invalid TOML configuration: {error}"
        ) from error

    if not isinstance(data, dict):
        raise ConfigurationError(
            "Configuration root must be a TOML table."
        )

    schema_version = data.get("schema_version")

    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigurationError(
            "Unsupported configuration schema version: "
            f"{schema_version!r}."
        )

    github = _table(data, "github")
    inventory = _table(data, "inventory")
    properties = _table(data, "properties")
    policy = _table(data, "policy")
    workflow = _table(data, "workflow")
    ruleset = _table(data, "ruleset")

    organization = _organization(
        _string(
            github,
            "organization",
            "github.organization",
        ),
        "github.organization",
    )

    selected_environment_organization = (
        environment_organization.strip()
        if environment_organization is not None
        else ""
    )

    if (
        selected_environment_organization
        and selected_environment_organization.casefold()
        != organization.casefold()
    ):
        raise ConfigurationError(
            "GITHUB_ORG does not match github.organization."
        )

    github_settings = GitHubSettings(
        organization=organization,
        rest_api_url=_url(
            _string(
                github,
                "rest_api_url",
                "github.rest_api_url",
            ),
            "github.rest_api_url",
            strip_trailing_slash=True,
        ),
        graphql_url=_url(
            _string(
                github,
                "graphql_url",
                "github.graphql_url",
            ),
            "github.graphql_url",
            strip_trailing_slash=False,
        ),
        web_url=_url(
            _string(
                github,
                "web_url",
                "github.web_url",
            ),
            "github.web_url",
            strip_trailing_slash=True,
        ),
    )

    inspection_depth = _string(
        inventory,
        "inspection_depth",
        "inventory.inspection_depth",
    )

    if inspection_depth != "root":
        raise ConfigurationError(
            "The initial onboarding implementation requires "
            "inventory.inspection_depth = 'root'."
        )

    inventory_settings = InventorySettings(
        inspection_depth=inspection_depth,
        workers=_positive_integer(
            inventory,
            "workers",
            "inventory.workers",
        ),
        max_hours=_positive_number(
            inventory,
            "max_hours",
            "inventory.max_hours",
        ),
        timeout_seconds=_positive_number(
            inventory,
            "timeout_seconds",
            "inventory.timeout_seconds",
        ),
        retries=_positive_integer(
            inventory,
            "retries",
            "inventory.retries",
        ),
    )

    property_settings = PropertySettings(
        activity_name=_property_name(
            _string(
                properties,
                "activity_name",
                "properties.activity_name",
            ),
            "properties.activity_name",
        ),
        languages_name=_property_name(
            _string(
                properties,
                "languages_name",
                "properties.languages_name",
            ),
            "properties.languages_name",
        ),
        policy_name=_property_name(
            _string(
                properties,
                "policy_name",
                "properties.policy_name",
            ),
            "properties.policy_name",
        ),
        assignment_mode=_string(
            properties,
            "assignment_mode",
            "properties.assignment_mode",
        ),
    )

    if len(set(property_settings.managed_names)) != 3:
        raise ConfigurationError(
            "Configured property names must be unique."
        )

    if property_settings.assignment_mode != "initialize_only":
        raise ConfigurationError(
            "The initial implementation requires "
            "properties.assignment_mode = 'initialize_only'."
        )

    policy_settings = PolicySettings(
        active_known=_policy_value(
            policy.get("active_known"),
            "policy.active_known",
        ),
        inactive_known=_policy_value(
            policy.get("inactive_known"),
            "policy.inactive_known",
        ),
        unknown=_policy_value(
            policy.get("unknown"),
            "policy.unknown",
        ),
        fork=_policy_value(
            policy.get("fork"),
            "policy.fork",
        ),
        repository_overrides=_load_overrides(
            policy,
            organization,
        ),
    )
    workflow_settings = _workflow_settings(
        workflow,
        organization,
    )
    ruleset_settings = _ruleset_settings(
        ruleset,
        workflow_settings,
    )

    return OnboardingConfig(
        schema_version=schema_version,
        github=github_settings,
        inventory=inventory_settings,
        properties=property_settings,
        policy=policy_settings,
        workflow=workflow_settings,
        ruleset=ruleset_settings,
        source_path=path,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def configured_environment_organization() -> str | None:
    value = os.environ.get("GITHUB_ORG")

    if value is None or not value.strip():
        return None

    return value.strip()

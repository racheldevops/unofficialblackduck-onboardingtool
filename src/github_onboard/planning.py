from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .errors import OnboardError
from .models import InventoryBundle, OnboardingConfig
from .policy import desired_managed_values


MAX_ALLOWED_VALUES = 200


def desired_property_definitions(
    bundle: InventoryBundle,
    config: OnboardingConfig,
) -> tuple[dict[str, Any], ...]:
    languages = sorted(
        {
            language
            for repository in bundle.inventory
            for language in repository["detected_languages"]
        }
    )

    return (
        {
            "property_name": config.properties.activity_name,
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": (
                "Repository activity classification from github_inventory."
            ),
            "allowed_values": ["active", "inactive"],
        },
        {
            "property_name": config.properties.languages_name,
            "value_type": "multi_select",
            "required": False,
            "default_value": None,
            "description": (
                "Repository language classifications from github_inventory."
            ),
            "allowed_values": languages,
        },
        {
            "property_name": config.properties.policy_name,
            "value_type": "single_select",
            "required": False,
            "default_value": None,
            "description": (
                "Black Duck SCA onboarding policy classification."
            ),
            "allowed_values": [
                "excluded",
                "required",
                "review",
            ],
        },
    )


def _definitions_by_name(
    definitions: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}

    for definition in definitions:
        name = definition.get("property_name")

        if not isinstance(name, str) or not name:
            raise OnboardError(
                "GitHub returned a property definition without a name.",
                category="invalid_github_response",
            )

        if name in result:
            raise OnboardError(
                f"GitHub returned duplicate property definition '{name}'.",
                category="invalid_github_response",
            )

        result[name] = definition

    return result


def _allowed_values(
    definition: Mapping[str, Any],
    name: str,
) -> list[str]:
    values = definition.get("allowed_values")

    if not isinstance(values, list):
        raise OnboardError(
            f"Property '{name}' has invalid allowed values.",
            category="property_schema_conflict",
        )

    if any(
        not isinstance(value, str) or not value
        for value in values
    ):
        raise OnboardError(
            f"Property '{name}' has invalid allowed values.",
            category="property_schema_conflict",
        )

    if len(values) != len(set(values)):
        raise OnboardError(
            f"Property '{name}' has duplicate allowed values.",
            category="property_schema_conflict",
        )

    return values


def plan_property_definitions(
    bundle: InventoryBundle,
    config: OnboardingConfig,
    existing_definitions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    existing_by_name = _definitions_by_name(
        existing_definitions
    )
    records: list[dict[str, Any]] = []

    for desired in desired_property_definitions(bundle, config):
        name = desired["property_name"]
        desired_allowed = list(desired["allowed_values"])
        existing = existing_by_name.get(name)

        if not desired_allowed:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "blocked",
                    "reason": "no_allowed_values",
                    "existing_definition": existing,
                    "desired_definition": desired,
                }
            )
            continue

        if len(desired_allowed) > MAX_ALLOWED_VALUES:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "blocked",
                    "reason": "allowed_value_capacity_exceeded",
                    "existing_definition": existing,
                    "desired_definition": desired,
                }
            )
            continue

        if existing is None:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "create",
                    "reason": "definition_missing",
                    "existing_definition": None,
                    "desired_definition": desired,
                }
            )
            continue

        existing_type = existing.get("value_type")

        if existing_type != desired["value_type"]:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "conflict",
                    "reason": "value_type_mismatch",
                    "existing_definition": dict(existing),
                    "desired_definition": desired,
                }
            )
            continue

        try:
            current_allowed = _allowed_values(
                existing,
                name,
            )
        except OnboardError as error:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "blocked",
                    "reason": error.category,
                    "existing_definition": dict(existing),
                    "desired_definition": desired,
                }
            )
            continue

        merged_allowed = sorted(
            set(current_allowed).union(desired_allowed)
        )

        if len(merged_allowed) > MAX_ALLOWED_VALUES:
            records.append(
                {
                    "record_type": "property_definition_plan",
                    "property_name": name,
                    "action": "blocked",
                    "reason": "allowed_value_capacity_exceeded",
                    "existing_definition": dict(existing),
                    "desired_definition": desired,
                }
            )
            continue

        merged = {
            "property_name": name,
            "value_type": desired["value_type"],
            "required": (
                existing.get("required")
                if type(existing.get("required")) is bool
                else False
            ),
            "default_value": existing.get("default_value"),
            "description": (
                existing.get("description")
                if isinstance(existing.get("description"), str)
                else desired["description"]
            ),
            "allowed_values": merged_allowed,
        }

        missing = sorted(
            set(desired_allowed).difference(current_allowed)
        )
        action = "add_allowed_values" if missing else "no_change"

        records.append(
            {
                "record_type": "property_definition_plan",
                "property_name": name,
                "action": action,
                "reason": (
                    "missing_allowed_values"
                    if missing
                    else "definition_compatible"
                ),
                "missing_allowed_values": missing,
                "existing_definition": dict(existing),
                "desired_definition": merged,
            }
        )

    return tuple(records)


def _property_values_from_sequence(
    values: Sequence[Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for item in values:
        if not isinstance(item, Mapping):
            raise OnboardError(
                "GitHub returned an invalid repository property value.",
                category="invalid_github_response",
            )

        name = item.get("property_name")

        if not isinstance(name, str) or not name:
            raise OnboardError(
                "GitHub returned a repository property without a name.",
                category="invalid_github_response",
            )

        if name in result:
            raise OnboardError(
                f"GitHub returned duplicate repository property '{name}'.",
                category="invalid_github_response",
            )

        result[name] = item.get("value")

    return result


def _property_values(
    value: Any,
) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, Mapping):
        properties = value.get("properties")

        if isinstance(properties, Sequence) and not isinstance(
            properties,
            (str, bytes),
        ):
            return _property_values_from_sequence(properties)

        return {
            str(name): property_value
            for name, property_value in value.items()
            if isinstance(name, str)
        }

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes),
    ):
        return _property_values_from_sequence(value)

    raise OnboardError(
        "GitHub returned invalid repository property values.",
        category="invalid_github_response",
    )


def _empty_property_value(value: Any) -> bool:
    return value is None or value == "" or value == []


def _normalized_value(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(value)

    return value


def _managed_values_equal(
    existing: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> bool:
    return all(
        _normalized_value(existing.get(name))
        == _normalized_value(value)
        for name, value in desired.items()
    )


def _assignments_by_name(
    assignments: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for name, value in assignments.items():
        if not isinstance(name, str) or not name:
            raise OnboardError(
                "Repository assignment map contains an invalid name.",
                category="invalid_github_response",
            )

        normalized = name.casefold()

        if normalized in result:
            raise OnboardError(
                f"Duplicate repository assignment state for '{name}'.",
                category="invalid_github_response",
            )

        result[normalized] = value

    return result


def plan_repository_assignments(
    bundle: InventoryBundle,
    config: OnboardingConfig,
    existing_assignments: Mapping[str, Any],
    *,
    refresh_all: bool,
) -> tuple[dict[str, Any], ...]:
    assignments_by_name = _assignments_by_name(
        existing_assignments
    )
    managed_names = config.properties.managed_names
    records: list[dict[str, Any]] = []

    for repository in sorted(
        bundle.inventory,
        key=lambda item: item["name_with_owner"].casefold(),
    ):
        name = repository["name_with_owner"]
        raw_existing = assignments_by_name.get(name.casefold())
        all_existing = _property_values(raw_existing)
        existing = {
            property_name: all_existing.get(property_name)
            for property_name in managed_names
        }
        desired, policy_reason = desired_managed_values(
            repository,
            config,
        )
        any_existing = any(
            not _empty_property_value(value)
            for value in existing.values()
        )

        if _managed_values_equal(existing, desired):
            action = "no_change"
            reason = "managed_values_match"
        elif not any_existing:
            action = "initialize"
            reason = "managed_values_absent"
        elif refresh_all:
            action = "update"
            reason = "refresh_all"
        else:
            action = "skipped_existing"
            reason = "managed_values_preserved"

        records.append(
            {
                "record_type": "repository_property_plan",
                "repository_id": repository["repository_id"],
                "name_with_owner": name,
                "action": action,
                "reason": reason,
                "policy_reason": policy_reason,
                "existing_values": existing,
                "desired_values": desired,
            }
        )

    return tuple(records)


def build_property_plan(
    bundle: InventoryBundle,
    config: OnboardingConfig,
    existing_definitions: Sequence[Mapping[str, Any]],
    existing_assignments: Mapping[str, Any],
    *,
    refresh_all: bool,
) -> tuple[dict[str, Any], ...]:
    definitions = plan_property_definitions(
        bundle,
        config,
        existing_definitions,
    )
    assignments = plan_repository_assignments(
        bundle,
        config,
        existing_assignments,
        refresh_all=refresh_all,
    )
    shared_blocked = any(
        record["action"] in {"blocked", "conflict"}
        for record in definitions
    )
    metadata = {
        "record_type": "property_plan_metadata",
        "organization": config.github.organization,
        "inventory_sha256": bundle.sha256,
        "config_sha256": config.source_sha256,
        "refresh_all": refresh_all,
        "shared_schema_blocked": shared_blocked,
        "inventory_repository_count": len(bundle.inventory),
        "inventory_failure_count": len(bundle.failures),
    }

    return (metadata, *definitions, *assignments)

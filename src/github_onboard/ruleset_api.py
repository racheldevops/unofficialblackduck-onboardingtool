from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

from .errors import OnboardError
from .rest import GitHubRestClient


@dataclass(frozen=True)
class RulesetSummary:
    ruleset_id: int
    name: str
    target: str
    enforcement: str


def _segment(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
    ):
        raise OnboardError(
            f"Invalid GitHub path value for {field}.",
            category="invalid_request",
        )

    return quote(value, safe="")


def _ruleset_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise OnboardError(
            "GitHub ruleset has no valid numeric ID.",
            category="invalid_github_response",
        )

    return value


def _json_value(response: Any, operation: str) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise OnboardError(
            f"GitHub returned invalid JSON during {operation}.",
            category="invalid_github_response",
        ) from error


def _object(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnboardError(
            f"GitHub returned a non-object during {operation}.",
            category="invalid_github_response",
        )

    return value


def _list(value: Any, operation: str) -> list[Any]:
    if not isinstance(value, list):
        raise OnboardError(
            f"GitHub returned a non-list during {operation}.",
            category="invalid_github_response",
        )

    return value


def normalize_ruleset(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    name = value.get("name")
    target = value.get("target")
    enforcement = value.get("enforcement")
    bypass_actors = value.get("bypass_actors", [])
    conditions = value.get("conditions")
    rules = value.get("rules")

    if not isinstance(name, str) or not name:
        raise OnboardError(
            "GitHub ruleset has no name.",
            category="invalid_github_response",
        )

    if not isinstance(target, str) or not target:
        raise OnboardError(
            f"GitHub ruleset '{name}' has no target.",
            category="invalid_github_response",
        )

    if not isinstance(enforcement, str) or not enforcement:
        raise OnboardError(
            f"GitHub ruleset '{name}' has no enforcement mode.",
            category="invalid_github_response",
        )

    if not isinstance(bypass_actors, list):
        raise OnboardError(
            f"GitHub ruleset '{name}' has invalid bypass actors.",
            category="invalid_github_response",
        )

    if not isinstance(conditions, dict):
        raise OnboardError(
            f"GitHub ruleset '{name}' has invalid conditions.",
            category="invalid_github_response",
        )

    if not isinstance(rules, list):
        raise OnboardError(
            f"GitHub ruleset '{name}' has invalid rules.",
            category="invalid_github_response",
        )

    return {
        "name": name,
        "target": target,
        "enforcement": enforcement,
        "bypass_actors": copy.deepcopy(bypass_actors),
        "conditions": copy.deepcopy(conditions),
        "rules": copy.deepcopy(rules),
    }


def _validate_desired_ruleset(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    expected_keys = {
        "name",
        "target",
        "enforcement",
        "bypass_actors",
        "conditions",
        "rules",
    }

    if set(value) != expected_keys:
        raise OnboardError(
            "Ruleset request contains unsupported fields.",
            category="invalid_request",
        )

    normalized = normalize_ruleset(value)
    name = normalized["name"]
    enforcement = normalized["enforcement"]

    if len(name) > 100:
        raise OnboardError(
            "Ruleset name exceeds 100 characters.",
            category="invalid_request",
        )

    if normalized["target"] != "branch":
        raise OnboardError(
            "Only branch rulesets are supported.",
            category="invalid_request",
        )

    if enforcement not in {
        "disabled",
        "evaluate",
        "active",
    }:
        raise OnboardError(
            "Ruleset enforcement mode is unsupported.",
            category="invalid_request",
        )

    if normalized["bypass_actors"] != []:
        raise OnboardError(
            "Ruleset bypass actors are not approved.",
            category="invalid_request",
        )

    conditions = normalized["conditions"]

    if set(conditions) != {
        "ref_name",
        "repository_property",
    }:
        raise OnboardError(
            "Ruleset conditions contain unsupported selectors.",
            category="invalid_request",
        )

    ref_name = conditions.get("ref_name")

    if ref_name != {
        "include": ["~DEFAULT_BRANCH"],
        "exclude": [],
    }:
        raise OnboardError(
            "Ruleset must target only the default branch.",
            category="invalid_request",
        )

    repository_property = conditions.get(
        "repository_property"
    )

    if not isinstance(repository_property, dict):
        raise OnboardError(
            "Ruleset repository-property condition is invalid.",
            category="invalid_request",
        )

    if set(repository_property) != {
        "include",
        "exclude",
    }:
        raise OnboardError(
            "Ruleset repository-property condition has "
            "unsupported fields.",
            category="invalid_request",
        )

    includes = repository_property.get("include")
    excludes = repository_property.get("exclude")

    if (
        not isinstance(includes, list)
        or len(includes) != 1
        or excludes != []
    ):
        raise OnboardError(
            "Ruleset must contain one included property condition "
            "and no excluded property conditions.",
            category="invalid_request",
        )

    included = includes[0]

    if not isinstance(included, dict) or set(included) != {
        "name",
        "property_values",
    }:
        raise OnboardError(
            "Ruleset property selector is invalid.",
            category="invalid_request",
        )

    property_name = included.get("name")
    property_values = included.get("property_values")

    if not isinstance(property_name, str) or not property_name:
        raise OnboardError(
            "Ruleset property selector has no property name.",
            category="invalid_request",
        )

    if (
        not isinstance(property_values, list)
        or len(property_values) != 1
        or not isinstance(property_values[0], str)
        or not property_values[0]
    ):
        raise OnboardError(
            "Ruleset property selector must contain one value.",
            category="invalid_request",
        )

    rules = normalized["rules"]

    if len(rules) != 1 or not isinstance(rules[0], dict):
        raise OnboardError(
            "Ruleset must contain exactly one rule.",
            category="invalid_request",
        )

    workflow_rule = rules[0]

    if set(workflow_rule) != {"type", "parameters"}:
        raise OnboardError(
            "Ruleset workflow rule contains unsupported fields.",
            category="invalid_request",
        )

    if workflow_rule.get("type") != "workflows":
        raise OnboardError(
            "Only the required-workflow rule is supported.",
            category="invalid_request",
        )

    parameters = workflow_rule.get("parameters")

    if not isinstance(parameters, dict) or set(parameters) != {
        "do_not_enforce_on_create",
        "workflows",
    }:
        raise OnboardError(
            "Required-workflow rule parameters are invalid.",
            category="invalid_request",
        )

    if type(
        parameters.get("do_not_enforce_on_create")
    ) is not bool:
        raise OnboardError(
            "Required-workflow repository-creation behavior "
            "must be boolean.",
            category="invalid_request",
        )

    workflows = parameters.get("workflows")

    if (
        not isinstance(workflows, list)
        or len(workflows) != 1
        or not isinstance(workflows[0], dict)
    ):
        raise OnboardError(
            "Ruleset must reference exactly one required workflow.",
            category="invalid_request",
        )

    workflow = workflows[0]

    if set(workflow) != {
        "path",
        "ref",
        "repository_id",
    }:
        raise OnboardError(
            "Required workflow contains unsupported fields.",
            category="invalid_request",
        )

    path = workflow.get("path")
    reference = workflow.get("ref")
    repository_id = workflow.get("repository_id")

    if (
        not isinstance(path, str)
        or not path.startswith(".github/workflows/")
        or path.endswith("/")
    ):
        raise OnboardError(
            "Required workflow path is invalid.",
            category="invalid_request",
        )

    if (
        not isinstance(reference, str)
        or not reference.startswith("refs/heads/")
        or reference == "refs/heads/"
    ):
        raise OnboardError(
            "Required workflow branch reference is invalid.",
            category="invalid_request",
        )

    if type(repository_id) is not int or repository_id <= 0:
        raise OnboardError(
            "Required workflow repository ID is invalid.",
            category="invalid_request",
        )

    return normalized


class GitHubRulesetsAPI:

    def __init__(
        self,
        client: GitHubRestClient,
        organization: str,
    ) -> None:
        self._client = client
        self._organization = organization
        self._organization_segment = _segment(
            organization,
            "organization",
        )

    def list_rulesets(self) -> list[RulesetSummary]:
        result: list[RulesetSummary] = []
        seen_ids: set[int] = set()
        page = 1

        while True:
            response = self._client.get(
                (
                    f"/orgs/{self._organization_segment}/"
                    "rulesets"
                ),
                params={
                    "includes_parents": "false",
                    "per_page": 100,
                    "page": page,
                },
            )
            values = _list(
                _json_value(
                    response,
                    "organization ruleset listing",
                ),
                "organization ruleset listing",
            )

            for index, value in enumerate(values):
                item = _object(
                    value,
                    f"organization ruleset item {index}",
                )
                ruleset_id = _ruleset_id(item.get("id"))
                name = item.get("name")
                target = item.get("target")
                enforcement = item.get("enforcement")

                if ruleset_id in seen_ids:
                    raise OnboardError(
                        "GitHub returned a duplicate ruleset ID.",
                        category="invalid_github_response",
                    )

                if not isinstance(name, str) or not name:
                    raise OnboardError(
                        "GitHub returned a ruleset without a name.",
                        category="invalid_github_response",
                    )

                if not isinstance(target, str) or not target:
                    raise OnboardError(
                        f"Ruleset '{name}' has no target.",
                        category="invalid_github_response",
                    )

                if (
                    not isinstance(enforcement, str)
                    or not enforcement
                ):
                    raise OnboardError(
                        f"Ruleset '{name}' has no enforcement mode.",
                        category="invalid_github_response",
                    )

                seen_ids.add(ruleset_id)
                result.append(
                    RulesetSummary(
                        ruleset_id=ruleset_id,
                        name=name,
                        target=target,
                        enforcement=enforcement,
                    )
                )

            if len(values) < 100:
                break

            page += 1

        return result

    def get_ruleset(
        self,
        ruleset_id: int,
    ) -> dict[str, Any]:
        selected_id = _ruleset_id(ruleset_id)
        response = self._client.get(
            (
                f"/orgs/{self._organization_segment}/"
                f"rulesets/{selected_id}"
            )
        )
        value = _object(
            _json_value(
                response,
                f"organization ruleset {selected_id}",
            ),
            f"organization ruleset {selected_id}",
        )

        if _ruleset_id(value.get("id")) != selected_id:
            raise OnboardError(
                "GitHub returned a different ruleset ID.",
                category="invalid_github_response",
            )

        return value

    def find_by_name(
        self,
        name: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(name, str) or not name:
            raise OnboardError(
                "Ruleset name must be nonempty.",
                category="invalid_request",
            )

        matching = [
            item
            for item in self.list_rulesets()
            if item.name == name
        ]

        return [
            self.get_ruleset(item.ruleset_id)
            for item in matching
        ]

    def create_ruleset(
        self,
        desired: Mapping[str, Any],
    ) -> dict[str, Any]:
        body = _validate_desired_ruleset(desired)
        response = self._client.mutate(
            "POST",
            (
                f"/orgs/{self._organization_segment}/"
                "rulesets"
            ),
            body=body,
        )
        result = _object(
            _json_value(
                response,
                "organization ruleset creation",
            ),
            "organization ruleset creation",
        )
        _ruleset_id(result.get("id"))
        return result

    def update_ruleset(
        self,
        ruleset_id: int,
        desired: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected_id = _ruleset_id(ruleset_id)
        body = _validate_desired_ruleset(desired)
        response = self._client.mutate(
            "PUT",
            (
                f"/orgs/{self._organization_segment}/"
                f"rulesets/{selected_id}"
            ),
            body=body,
        )
        result = _object(
            _json_value(
                response,
                f"organization ruleset update {selected_id}",
            ),
            f"organization ruleset update {selected_id}",
        )

        if _ruleset_id(result.get("id")) != selected_id:
            raise OnboardError(
                "GitHub returned a different updated ruleset ID.",
                category="invalid_github_response",
            )

        return result

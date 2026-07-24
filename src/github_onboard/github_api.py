from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from .errors import OnboardError
from .rest import GitHubRestClient


MAX_ASSIGNMENT_BATCH = 30


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


class GitHubPropertiesAPI:

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

    @property
    def organization(self) -> str:
        return self._organization

    def preflight(self) -> dict[str, Any]:
        viewer = _object(
            _json_value(
                self._client.get("/user"),
                "viewer preflight",
            ),
            "viewer preflight",
        )
        organization = _object(
            _json_value(
                self._client.get(
                    f"/orgs/{self._organization_segment}"
                ),
                "organization preflight",
            ),
            "organization preflight",
        )
        viewer_login = viewer.get("login")
        organization_login = organization.get("login")

        if not isinstance(viewer_login, str) or not viewer_login:
            raise OnboardError(
                "GitHub viewer response has no login.",
                category="invalid_github_response",
            )

        if (
            not isinstance(organization_login, str)
            or not organization_login
        ):
            raise OnboardError(
                "GitHub organization response has no login.",
                category="invalid_github_response",
            )

        if (
            organization_login.casefold()
            != self._organization.casefold()
        ):
            raise OnboardError(
                "GitHub returned a different organization.",
                category="invalid_github_response",
            )

        definitions = self.list_definitions()
        stats = self._client.stats()

        return {
            "record_type": "property_preflight",
            "viewer": viewer_login,
            "organization": organization_login,
            "organization_id": organization.get("id"),
            "organization_plan": (
                organization.get("plan", {}).get("name")
                if isinstance(organization.get("plan"), dict)
                else None
            ),
            "custom_property_schema_readable": True,
            "existing_property_names": sorted(
                definition["property_name"]
                for definition in definitions
            ),
            "rest_rate_remaining": stats.rate_remaining,
            "rest_rate_reset_epoch": stats.rate_reset_epoch,
        }

    def list_definitions(self) -> list[dict[str, Any]]:
        response = self._client.get(
            f"/orgs/{self._organization_segment}/properties/schema"
        )
        values = _list(
            _json_value(
                response,
                "custom property schema listing",
            ),
            "custom property schema listing",
        )
        definitions: list[dict[str, Any]] = []
        names: set[str] = set()

        for index, value in enumerate(values):
            definition = _object(
                value,
                f"custom property schema item {index}",
            )
            name = definition.get("property_name")

            if not isinstance(name, str) or not name:
                raise OnboardError(
                    "GitHub returned a custom property without a name.",
                    category="invalid_github_response",
                )

            if name in names:
                raise OnboardError(
                    f"GitHub returned duplicate custom property '{name}'.",
                    category="invalid_github_response",
                )

            names.add(name)
            definitions.append(definition)

        return definitions

    def list_repository_assignments(
        self,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        page = 1

        while True:
            response = self._client.get(
                f"/orgs/{self._organization_segment}/properties/values",
                params={
                    "per_page": 100,
                    "page": page,
                },
            )
            values = _list(
                _json_value(
                    response,
                    "organization repository property listing",
                ),
                "organization repository property listing",
            )

            for index, value in enumerate(values):
                assignment = _object(
                    value,
                    (
                        "organization repository property item "
                        f"{index}"
                    ),
                )
                full_name = assignment.get(
                    "repository_full_name"
                )

                if (
                    not isinstance(full_name, str)
                    or full_name.count("/") != 1
                ):
                    raise OnboardError(
                        "GitHub returned repository property values "
                        "without repository_full_name.",
                        category="invalid_github_response",
                    )

                owner, _name = full_name.split("/", 1)

                if (
                    owner.casefold()
                    != self._organization.casefold()
                ):
                    raise OnboardError(
                        "GitHub returned repository property values "
                        "for another organization.",
                        category="invalid_github_response",
                    )

                normalized = full_name.casefold()

                if normalized in result:
                    raise OnboardError(
                        "GitHub returned duplicate repository "
                        f"property values for '{full_name}'.",
                        category="invalid_github_response",
                    )

                result[normalized] = assignment

            if len(values) < 100:
                break

            page += 1

        return result

    def get_repository_values(
        self,
        name_with_owner: str,
    ) -> list[dict[str, Any]]:
        owner, name = self._repository_parts(name_with_owner)
        response = self._client.get(
            f"/repos/{_segment(owner, 'repository owner')}/"
            f"{_segment(name, 'repository name')}/properties/values"
        )
        values = _list(
            _json_value(
                response,
                f"repository property read for {name_with_owner}",
            ),
            f"repository property read for {name_with_owner}",
        )
        properties: list[dict[str, Any]] = []
        names: set[str] = set()

        for index, value in enumerate(values):
            item = _object(
                value,
                f"repository property item {index}",
            )
            property_name = item.get("property_name")

            if (
                not isinstance(property_name, str)
                or not property_name
            ):
                raise OnboardError(
                    f"GitHub returned an invalid property for "
                    f"'{name_with_owner}'.",
                    category="invalid_github_response",
                )

            if property_name in names:
                raise OnboardError(
                    f"GitHub returned duplicate property "
                    f"'{property_name}' for '{name_with_owner}'.",
                    category="invalid_github_response",
                )

            names.add(property_name)
            properties.append(item)

        return properties

    def put_definition(
        self,
        definition: Mapping[str, Any],
    ) -> dict[str, Any]:
        name = definition.get("property_name")

        if not isinstance(name, str) or not name:
            raise OnboardError(
                "Property definition has no property_name.",
                category="invalid_request",
            )

        value_type = definition.get("value_type")

        if value_type not in {
            "string",
            "single_select",
            "multi_select",
            "true_false",
        }:
            raise OnboardError(
                f"Property '{name}' has an invalid value_type.",
                category="invalid_request",
            )

        body: dict[str, Any] = {
            "value_type": value_type,
            "required": bool(definition.get("required", False)),
            "default_value": definition.get("default_value"),
            "description": definition.get("description", ""),
        }

        if value_type in {"single_select", "multi_select"}:
            allowed_values = definition.get("allowed_values")

            if (
                not isinstance(allowed_values, list)
                or not allowed_values
                or any(
                    not isinstance(value, str) or not value
                    for value in allowed_values
                )
            ):
                raise OnboardError(
                    f"Property '{name}' has invalid allowed values.",
                    category="invalid_request",
                )

            body["allowed_values"] = list(allowed_values)

        response = self._client.mutate(
            "PUT",
            (
                f"/orgs/{self._organization_segment}/"
                f"properties/schema/{_segment(name, 'property name')}"
            ),
            body=body,
        )
        result = _object(
            _json_value(
                response,
                f"property definition write for {name}",
            ),
            f"property definition write for {name}",
        )

        if result.get("property_name") != name:
            raise OnboardError(
                f"GitHub returned a different property after "
                f"writing '{name}'.",
                category="invalid_github_response",
            )

        return result

    def set_repository_values(
        self,
        repositories: Sequence[str],
        values: Mapping[str, Any],
    ) -> None:
        if not repositories:
            raise OnboardError(
                "Repository assignment batch is empty.",
                category="invalid_request",
            )

        if len(repositories) > MAX_ASSIGNMENT_BATCH:
            raise OnboardError(
                "Repository assignment batch exceeds the supported size.",
                category="invalid_request",
            )

        repository_names: list[str] = []

        for name_with_owner in repositories:
            _owner, repository_name = self._repository_parts(
                name_with_owner
            )
            repository_names.append(repository_name)

        if len(repository_names) != len(set(repository_names)):
            raise OnboardError(
                "Repository assignment batch contains duplicates.",
                category="invalid_request",
            )

        properties: list[dict[str, Any]] = []

        for property_name, value in sorted(values.items()):
            if not isinstance(property_name, str) or not property_name:
                raise OnboardError(
                    "Repository assignment has an invalid property name.",
                    category="invalid_request",
                )

            if not isinstance(value, (str, list, bool)) and value is not None:
                raise OnboardError(
                    f"Repository assignment for '{property_name}' "
                    "has an invalid value.",
                    category="invalid_request",
                )

            if isinstance(value, list) and any(
                not isinstance(item, str) or not item
                for item in value
            ):
                raise OnboardError(
                    f"Repository assignment for '{property_name}' "
                    "has an invalid multi-select value.",
                    category="invalid_request",
                )

            properties.append(
                {
                    "property_name": property_name,
                    "value": value,
                }
            )

        if not properties:
            raise OnboardError(
                "Repository assignment has no properties.",
                category="invalid_request",
            )

        self._client.mutate(
            "PATCH",
            (
                f"/orgs/{self._organization_segment}/"
                "properties/values"
            ),
            body={
                "repository_names": repository_names,
                "properties": properties,
            },
        )

    def _repository_parts(
        self,
        name_with_owner: str,
    ) -> tuple[str, str]:
        if (
            not isinstance(name_with_owner, str)
            or name_with_owner.count("/") != 1
            or name_with_owner.startswith("/")
            or name_with_owner.endswith("/")
        ):
            raise OnboardError(
                "Repository name must use owner/name.",
                category="invalid_request",
            )

        owner, name = name_with_owner.split("/", 1)

        if owner.casefold() != self._organization.casefold():
            raise OnboardError(
                f"Repository '{name_with_owner}' is outside "
                f"organization '{self._organization}'.",
                category="invalid_request",
            )

        return owner, name

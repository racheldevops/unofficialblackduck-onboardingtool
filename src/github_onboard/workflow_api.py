from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .errors import OnboardError
from .rest import GitHubRestClient


@dataclass(frozen=True)
class RepositoryState:
    repository_id: int
    node_id: str
    full_name: str
    default_branch: str
    visibility: str
    empty: bool
    can_push: bool


@dataclass(frozen=True)
class WorkflowFileState:
    path: str
    sha: str
    content: bytes
    html_url: str | None


@dataclass(frozen=True)
class WorkflowWriteResult:
    content_sha: str
    commit_sha: str
    commit_html_url: str | None


def _object(
    value: Any,
    operation: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnboardError(
            f"GitHub returned a non-object during {operation}.",
            category="invalid_github_response",
        )

    return value


def _json_object(
    response: Any,
    operation: str,
) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as error:
        raise OnboardError(
            f"GitHub returned invalid JSON during {operation}.",
            category="invalid_github_response",
        ) from error

    return _object(value, operation)


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


def _content_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.endswith("/")
    ):
        raise OnboardError(
            "Workflow destination path is invalid.",
            category="invalid_request",
        )

    parts = value.split("/")

    if any(
        not part or part in {".", ".."}
        for part in parts
    ):
        raise OnboardError(
            "Workflow destination path is invalid.",
            category="invalid_request",
        )

    return "/".join(
        quote(part, safe="")
        for part in parts
    )


class GitHubWorkflowAPI:

    def __init__(
        self,
        client: GitHubRestClient,
        repository: str,
    ) -> None:
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or repository.startswith("/")
            or repository.endswith("/")
        ):
            raise OnboardError(
                "Workflow repository must use owner/name.",
                category="invalid_request",
            )

        owner, name = repository.split("/", 1)
        self._client = client
        self._repository = repository
        self._owner = owner
        self._name = name
        self._owner_segment = _segment(
            owner,
            "repository owner",
        )
        self._name_segment = _segment(
            name,
            "repository name",
        )

    @property
    def repository_name(self) -> str:
        return self._repository

    def repository_state(self) -> RepositoryState:
        response = self._client.get(
            (
                f"/repos/{self._owner_segment}/"
                f"{self._name_segment}"
            )
        )
        value = _json_object(
            response,
            "workflow repository inspection",
        )
        full_name = value.get("full_name")
        repository_id = value.get("id")
        node_id = value.get("node_id")
        default_branch = value.get("default_branch")
        visibility = value.get("visibility")
        size = value.get("size")
        permissions = value.get("permissions")

        if (
            not isinstance(full_name, str)
            or full_name.casefold()
            != self._repository.casefold()
        ):
            raise OnboardError(
                "GitHub returned a different workflow repository.",
                category="invalid_github_response",
            )

        if type(repository_id) is not int:
            raise OnboardError(
                "Workflow repository has no numeric ID.",
                category="invalid_github_response",
            )

        if not isinstance(node_id, str) or not node_id:
            raise OnboardError(
                "Workflow repository has no node ID.",
                category="invalid_github_response",
            )

        if (
            not isinstance(default_branch, str)
            or not default_branch
        ):
            raise OnboardError(
                "Workflow repository has no default branch.",
                category="invalid_github_response",
            )

        if (
            not isinstance(visibility, str)
            or visibility not in {
                "public",
                "private",
                "internal",
            }
        ):
            raise OnboardError(
                "Workflow repository has invalid visibility.",
                category="invalid_github_response",
            )

        if type(size) is not int or size < 0:
            raise OnboardError(
                "Workflow repository has invalid size.",
                category="invalid_github_response",
            )

        if not isinstance(permissions, dict):
            raise OnboardError(
                "Workflow repository permissions are unavailable.",
                category="invalid_github_response",
            )

        can_push = permissions.get("push")

        if type(can_push) is not bool:
            raise OnboardError(
                "Workflow repository push permission is unavailable.",
                category="invalid_github_response",
            )

        return RepositoryState(
            repository_id=repository_id,
            node_id=node_id,
            full_name=full_name,
            default_branch=default_branch,
            visibility=visibility,
            empty=size == 0,
            can_push=can_push,
        )

    def get_file(
        self,
        path: str,
        *,
        ref: str,
    ) -> WorkflowFileState | None:
        encoded_path = _content_path(path)
        response = self._client.get(
            (
                f"/repos/{self._owner_segment}/"
                f"{self._name_segment}/contents/"
                f"{encoded_path}"
            ),
            params={"ref": ref},
        )

        if response.status_code == 404:
            return None

        value = _json_object(
            response,
            f"workflow file inspection for {path}",
        )

        if value.get("type") != "file":
            raise OnboardError(
                f"Workflow destination is not a file: {path}",
                category="workflow_conflict",
            )

        returned_path = value.get("path")
        sha = value.get("sha")
        encoding = value.get("encoding")
        encoded_content = value.get("content")
        html_url = value.get("html_url")

        if returned_path != path:
            raise OnboardError(
                "GitHub returned a different workflow path.",
                category="invalid_github_response",
            )

        if not isinstance(sha, str) or not sha:
            raise OnboardError(
                "GitHub workflow file has no SHA.",
                category="invalid_github_response",
            )

        if (
            encoding != "base64"
            or not isinstance(encoded_content, str)
        ):
            raise OnboardError(
                "GitHub workflow file content is unavailable.",
                category="invalid_github_response",
            )

        try:
            content = base64.b64decode(
                encoded_content.replace("\n", ""),
                validate=True,
            )
        except ValueError as error:
            raise OnboardError(
                "GitHub returned invalid workflow file content.",
                category="invalid_github_response",
            ) from error

        if html_url is not None and not isinstance(
            html_url,
            str,
        ):
            raise OnboardError(
                "GitHub returned an invalid workflow URL.",
                category="invalid_github_response",
            )

        return WorkflowFileState(
            path=returned_path,
            sha=sha,
            content=content,
            html_url=html_url,
        )

    def put_file(
        self,
        path: str,
        *,
        branch: str | None,
        message: str,
        content: bytes,
        current_sha: str | None,
    ) -> WorkflowWriteResult:
        if not isinstance(message, str) or not message.strip():
            raise OnboardError(
                "Workflow commit message must be nonempty.",
                category="invalid_request",
            )

        if not isinstance(content, bytes) or not content:
            raise OnboardError(
                "Workflow content must be nonempty bytes.",
                category="invalid_request",
            )

        if branch is not None and (
            not isinstance(branch, str)
            or not branch
            or any(
                character.isspace()
                for character in branch
            )
        ):
            raise OnboardError(
                "Workflow branch is invalid.",
                category="invalid_request",
            )

        if current_sha is not None and (
            not isinstance(current_sha, str)
            or not current_sha
        ):
            raise OnboardError(
                "Current workflow file SHA is invalid.",
                category="invalid_request",
            )

        body: dict[str, Any] = {
            "message": message.strip(),
            "content": base64.b64encode(content).decode("ascii"),
        }

        if branch is not None:
            body["branch"] = branch

        if current_sha is not None:
            body["sha"] = current_sha

        encoded_path = _content_path(path)
        response = self._client.mutate(
            "PUT",
            (
                f"/repos/{self._owner_segment}/"
                f"{self._name_segment}/contents/"
                f"{encoded_path}"
            ),
            body=body,
        )
        value = _json_object(
            response,
            f"workflow file publication for {path}",
        )
        content_result = _object(
            value.get("content"),
            "workflow publication content",
        )
        commit_result = _object(
            value.get("commit"),
            "workflow publication commit",
        )
        content_sha = content_result.get("sha")
        commit_sha = commit_result.get("sha")
        commit_html_url = commit_result.get("html_url")

        if not isinstance(content_sha, str) or not content_sha:
            raise OnboardError(
                "Workflow publication returned no content SHA.",
                category="invalid_github_response",
            )

        if not isinstance(commit_sha, str) or not commit_sha:
            raise OnboardError(
                "Workflow publication returned no commit SHA.",
                category="invalid_github_response",
            )

        if commit_html_url is not None and not isinstance(
            commit_html_url,
            str,
        ):
            raise OnboardError(
                "Workflow publication returned an invalid commit URL.",
                category="invalid_github_response",
            )

        return WorkflowWriteResult(
            content_sha=content_sha,
            commit_sha=commit_sha,
            commit_html_url=commit_html_url,
        )

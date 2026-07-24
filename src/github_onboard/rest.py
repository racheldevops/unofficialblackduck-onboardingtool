from __future__ import annotations

import email.utils
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import GitHubRestError, OnboardError
from .models import RestStatistics


@dataclass(frozen=True)
class AllowedRoute:
    method: str
    pattern: re.Pattern[str]
    mutation: bool
    success_statuses: frozenset[int]


_SEGMENT = r"[^/?#]+"

ALLOWED_ROUTES = (
    AllowedRoute(
        method="GET",
        pattern=re.compile(r"^/user$"),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/orgs/{_SEGMENT}$"
        ),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/orgs/{_SEGMENT}/properties/schema$"
        ),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/orgs/{_SEGMENT}/properties/values$"
        ),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/repos/{_SEGMENT}/{_SEGMENT}/properties/values$"
        ),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="PUT",
        pattern=re.compile(
            rf"^/orgs/{_SEGMENT}/properties/schema/{_SEGMENT}$"
        ),
        mutation=True,
        success_statuses=frozenset({200, 201}),
    ),
    AllowedRoute(
        method="PATCH",
        pattern=re.compile(
            rf"^/orgs/{_SEGMENT}/properties/values$"
        ),
        mutation=True,
        success_statuses=frozenset({204}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/repos/{_SEGMENT}/{_SEGMENT}$"
        ),
        mutation=False,
        success_statuses=frozenset({200}),
    ),
    AllowedRoute(
        method="GET",
        pattern=re.compile(
            rf"^/repos/{_SEGMENT}/{_SEGMENT}/contents/.+$"
        ),
        mutation=False,
        success_statuses=frozenset({200, 404}),
    ),
    AllowedRoute(
        method="PUT",
        pattern=re.compile(
            rf"^/repos/{_SEGMENT}/{_SEGMENT}/contents/.+$"
        ),
        mutation=True,
        success_statuses=frozenset({200, 201}),
    ),
)


class GitHubRestClient:

    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        timeout: float,
        max_attempts: int,
        mutation_enabled: bool,
        deadline: float | None = None,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not token:
            raise OnboardError(
                "GITHUB_TOKEN is required.",
                category="authentication_required",
            )

        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive.")

        self._token = token
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._mutation_enabled = mutation_enabled
        self._deadline = deadline
        self._sleeper = sleeper
        self._jitter = jitter
        self._lock = threading.Lock()
        self._requests = 0
        self._retries = 0
        self._rate_remaining: int | None = None
        self._rate_reset_epoch: float | None = None
        self._client = httpx.Client(
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-organization-onboard",
            },
        )

    @property
    def mutation_enabled(self) -> bool:
        return self._mutation_enabled

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubRestClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def stats(self) -> RestStatistics:
        with self._lock:
            return RestStatistics(
                requests=self._requests,
                retries=self._retries,
                rate_remaining=self._rate_remaining,
                rate_reset_epoch=self._rate_reset_epoch,
            )

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        return self._request(
            "GET",
            path,
            params=params,
            body=None,
            mutation=False,
        )

    def mutate(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any],
    ) -> httpx.Response:
        if not self._mutation_enabled:
            raise OnboardError(
                "Mutation was attempted without --apply.",
                category="mutation_blocked",
            )

        return self._request(
            method.upper(),
            path,
            params=None,
            body=body,
            mutation=True,
        )

    def _route(
        self,
        method: str,
        path: str,
        mutation: bool,
    ) -> AllowedRoute:
        if (
            not path.startswith("/")
            or "?" in path
            or "#" in path
            or "://" in path
        ):
            raise OnboardError(
                f"REST path is not allowlisted: {path!r}",
                category="endpoint_not_allowlisted",
            )

        for route in ALLOWED_ROUTES:
            if (
                route.method == method
                and route.mutation is mutation
                and route.pattern.fullmatch(path) is not None
            ):
                return route

        raise OnboardError(
            f"REST endpoint is not allowlisted: {method} {path}",
            category="endpoint_not_allowlisted",
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None,
        mutation: bool,
    ) -> httpx.Response:
        route = self._route(method, path, mutation)
        url = f"{self._base_url}{path}"

        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_rate_limit()
            self._ensure_deadline()
            self._increment_request()

            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=dict(body) if body is not None else None,
                )
            except httpx.TransportError as error:
                message = (
                    f"GitHub network failure during {method} {path}: "
                    f"{type(error).__name__}: "
                    f"{self._redact(str(error))}"
                )
                self._retry_or_raise(
                    category="network_error",
                    message=message,
                    attempt=attempt,
                    response=None,
                    status_code=None,
                )
                continue

            self._update_rate_state(response)

            if self._is_rate_limited(response):
                self._retry_or_raise(
                    category="rate_limited",
                    message=(
                        f"GitHub rate limit reached during "
                        f"{method} {path}."
                    ),
                    attempt=attempt,
                    response=response,
                    status_code=response.status_code,
                )
                continue

            if response.status_code >= 500:
                self._retry_or_raise(
                    category="github_server_error",
                    message=(
                        f"GitHub returned HTTP {response.status_code} "
                        f"during {method} {path}."
                    ),
                    attempt=attempt,
                    response=response,
                    status_code=response.status_code,
                )
                continue

            if response.status_code not in route.success_statuses:
                raise self._status_error(
                    method,
                    path,
                    response.status_code,
                    attempt,
                )

            return response

        raise AssertionError("REST retry loop exited unexpectedly.")

    def _status_error(
        self,
        method: str,
        path: str,
        status_code: int,
        attempt: int,
    ) -> GitHubRestError:
        if status_code == 401:
            category = "authentication_failed"
            message = "GitHub rejected GITHUB_TOKEN."
        elif status_code == 403:
            category = "authorization_failed"
            message = f"GitHub denied {method} {path}."
        elif status_code == 404:
            category = "not_found"
            message = f"GitHub could not find the resource for {path}."
        elif status_code == 422:
            category = "invalid_request"
            message = (
                f"GitHub rejected the request for {method} {path}."
            )
        else:
            category = "github_rest_error"
            message = (
                f"GitHub returned HTTP {status_code} "
                f"during {method} {path}."
            )

        return GitHubRestError(
            category,
            message,
            attempts=attempt,
            status_code=status_code,
        )

    def _retry_or_raise(
        self,
        *,
        category: str,
        message: str,
        attempt: int,
        response: httpx.Response | None,
        status_code: int | None,
    ) -> None:
        if attempt >= self._max_attempts:
            raise GitHubRestError(
                category,
                self._redact(message),
                attempts=attempt,
                status_code=status_code,
                retryable=True,
            )

        self._increment_retry()
        self._sleep(self._retry_delay(attempt, response))

    def _retry_delay(
        self,
        attempt: int,
        response: httpx.Response | None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")

            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    parsed = email.utils.parsedate_to_datetime(
                        retry_after
                    )

                    if parsed.tzinfo is None:
                        parsed = parsed.replace(
                            tzinfo=__import__(
                                "datetime"
                            ).timezone.utc
                        )

                    return max(
                        0.0,
                        parsed.timestamp() - time.time(),
                    )

            reset = self._parse_integer(
                response.headers.get("x-ratelimit-reset")
            )

            if reset is not None:
                wait = float(reset) - time.time() + 1.0

                if wait > 0:
                    return wait

        exponential = min(30.0, float(2 ** (attempt - 1)))
        return exponential + self._jitter(0.0, 1.0)

    def _is_rate_limited(
        self,
        response: httpx.Response,
    ) -> bool:
        if response.status_code == 429:
            return True

        if response.status_code != 403:
            return False

        if response.headers.get("x-ratelimit-remaining") == "0":
            return True

        body = response.text.casefold()

        return (
            "rate limit" in body
            or "abuse detection" in body
            or "secondary rate" in body
        )

    def _wait_for_rate_limit(self) -> None:
        self._ensure_deadline()

        with self._lock:
            remaining = self._rate_remaining
            reset_epoch = self._rate_reset_epoch

        if (
            remaining is not None
            and remaining <= 0
            and reset_epoch is not None
        ):
            wait = reset_epoch - time.time() + 1.0

            if wait > 0:
                self._sleep(wait)

    def _update_rate_state(
        self,
        response: httpx.Response,
    ) -> None:
        remaining = self._parse_integer(
            response.headers.get("x-ratelimit-remaining")
        )
        reset = self._parse_integer(
            response.headers.get("x-ratelimit-reset")
        )

        with self._lock:
            if remaining is not None:
                self._rate_remaining = remaining

            if reset is not None:
                self._rate_reset_epoch = float(reset)

    def _ensure_deadline(self) -> None:
        if (
            self._deadline is not None
            and time.monotonic() >= self._deadline
        ):
            raise OnboardError(
                "The runtime budget was exhausted before a "
                "GitHub REST request.",
                category="runtime_budget_exceeded",
            )

    def _sleep(self, seconds: float) -> None:
        delay = max(0.0, seconds)

        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()

            if remaining <= 0 or delay >= remaining:
                raise OnboardError(
                    "A required retry or rate-limit wait would "
                    "exceed the runtime budget.",
                    category="runtime_budget_exceeded",
                )

        self._sleeper(delay)

    def _increment_request(self) -> None:
        with self._lock:
            self._requests += 1

    def _increment_retry(self) -> None:
        with self._lock:
            self._retries += 1

    def _redact(self, value: str) -> str:
        if not self._token:
            return value

        return value.replace(self._token, "[REDACTED]")

    @staticmethod
    def _parse_integer(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

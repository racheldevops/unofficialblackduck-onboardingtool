from __future__ import annotations
import datetime as dt
import email.utils
import random
import threading
import time
from typing import Any, Callable
import httpx
from ..classification import parse_github_timestamp, redact
from ..errors import *
from ..models import ClientStats
from ..settings import *

class GitHubClient:

    def __init__(self, token: str, *, endpoint: str=GITHUB_GRAPHQL_URL, timeout: float=DEFAULT_TIMEOUT_SECONDS, max_attempts: int=DEFAULT_MAX_ATTEMPTS, deadline: float | None=None, verify: bool=True, transport: httpx.BaseTransport | None=None, sleeper: Callable[[float], None]=time.sleep, jitter: Callable[[float, float], float]=random.uniform) -> None:
        self._token = token
        self._max_attempts = max_attempts
        self._deadline = deadline
        self._sleeper = sleeper
        self._jitter = jitter
        self._lock = threading.Lock()
        self._requests = 0
        self._graphql_cost = 0
        self._retries = 0
        self._rate_remaining: int | None = None
        self._rate_reset_epoch: float | None = None
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=timeout, verify=verify, transport=transport, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'github-organization-repository-inventory'})

    def close(self) -> None:
        self._client.close()

    def stats(self) -> ClientStats:
        with self._lock:
            return ClientStats(requests=self._requests, graphql_cost=self._graphql_cost, retries=self._retries)

    def _ensure_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise RuntimeBudgetExceeded('The runtime budget was exhausted before a GitHub request.')

    def _increment_request(self) -> None:
        with self._lock:
            self._requests += 1

    def _increment_retry(self) -> None:
        with self._lock:
            self._retries += 1

    def _update_rate_state(self, remaining: int | None, reset_epoch: float | None, cost: int | None=None) -> None:
        with self._lock:
            if remaining is not None:
                self._rate_remaining = remaining
            if reset_epoch is not None:
                self._rate_reset_epoch = reset_epoch
            if cost is not None:
                self._graphql_cost += max(0, cost)

    def _sleep(self, seconds: float) -> None:
        delay = max(0.0, seconds)
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or delay >= remaining:
                raise RuntimeBudgetExceeded('A required retry or rate-limit wait would exceed the runtime budget.')
        self._sleeper(delay)

    def _wait_for_rate_limit(self) -> None:
        self._ensure_deadline()
        with self._lock:
            remaining = self._rate_remaining
            reset_epoch = self._rate_reset_epoch
        if remaining is not None and remaining <= 10 and (reset_epoch is not None):
            wait = reset_epoch - time.time() + 1.0
            if wait > 0:
                self._sleep(wait)

    @staticmethod
    def _parse_reset_at(value: str | None) -> float | None:
        parsed = parse_github_timestamp(value)
        return parsed.timestamp() if parsed else None

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _update_response_rate(self, response: httpx.Response) -> None:
        remaining = self._parse_int(response.headers.get('x-ratelimit-remaining'))
        reset_epoch_raw = self._parse_int(response.headers.get('x-ratelimit-reset'))
        reset_epoch = float(reset_epoch_raw) if reset_epoch_raw is not None else None
        self._update_rate_state(remaining, reset_epoch)

    def _update_graphql_rate(self, data: dict[str, Any]) -> None:
        rate = data.get('rateLimit')
        if not isinstance(rate, dict):
            return
        self._update_rate_state(self._parse_int(rate.get('remaining')), self._parse_reset_at(rate.get('resetAt')), self._parse_int(rate.get('cost')))

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get('retry-after')
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    return max(0.0, parsed.timestamp() - time.time())
            reset = self._parse_int(response.headers.get('x-ratelimit-reset'))
            if reset is not None:
                wait = float(reset) - time.time() + 1.0
                if wait > 0:
                    return wait
        exponential = min(30.0, float(2 ** (attempt - 1)))
        return exponential + self._jitter(0.0, 1.0)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get('x-ratelimit-remaining') == '0':
            return True
        body = response.text.casefold()
        return 'rate limit' in body or 'abuse detection' in body

    def _retry_or_raise(self, *, category: str, message: str, attempt: int, response: httpx.Response | None=None) -> None:
        if attempt >= self._max_attempts:
            raise GitHubError(category, message, attempts=attempt, retryable=True)
        self._increment_retry()
        self._sleep(self._retry_delay(attempt, response))

    def graphql(self, query: str, variables: dict[str, Any], *, operation: str) -> dict[str, Any]:
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_rate_limit()
            self._increment_request()
            try:
                response = self._client.post(self._endpoint, json={'query': query, 'variables': variables})
            except httpx.TransportError as error:
                self._retry_or_raise(category='network_error', message=f'GitHub network failure during {operation}: {type(error).__name__}: {redact(str(error), [self._token])}', attempt=attempt)
                continue
            self._update_response_rate(response)
            if self._is_rate_limited(response):
                self._retry_or_raise(category='rate_limited', message=f'GitHub rate limit reached during {operation}.', attempt=attempt, response=response)
                continue
            if response.status_code >= 500:
                self._retry_or_raise(category='github_server_error', message=f'GitHub returned HTTP {response.status_code} during {operation}.', attempt=attempt, response=response)
                continue
            if response.status_code == 401:
                raise GitHubError('authentication_failed', 'GitHub rejected GITHUB_TOKEN.', attempts=attempt)
            if response.status_code == 403:
                raise GitHubError('authorization_failed', f'GitHub denied {operation}.', attempts=attempt)
            if response.status_code == 404:
                raise GitHubError('not_found', f'GitHub could not find the resource for {operation}.', attempts=attempt)
            if response.status_code >= 400:
                raise GitHubError('invalid_request', f'GitHub returned HTTP {response.status_code} during {operation}.', attempts=attempt)
            try:
                payload = response.json()
            except ValueError:
                self._retry_or_raise(category='invalid_response', message=f'GitHub returned invalid JSON during {operation}.', attempt=attempt, response=response)
                continue
            data = payload.get('data')
            if isinstance(data, dict):
                self._update_graphql_rate(data)
            errors = payload.get('errors') or []
            if errors:
                messages = '; '.join((str(error.get('message', 'GraphQL error')) for error in errors if isinstance(error, dict)))
                messages = redact(messages, [self._token])
                error_types = {str(error.get('type', '')).casefold() for error in errors if isinstance(error, dict)}
                lowered = messages.casefold()
                transient = 'rate_limited' in error_types or 'rate limit' in lowered or 'something went wrong' in lowered or ('timeout' in lowered)
                if transient:
                    self._retry_or_raise(category='graphql_transient_error', message=f'Transient GraphQL failure during {operation}.', attempt=attempt, response=response)
                    continue
                if 'forbidden' in error_types:
                    category = 'authorization_failed'
                elif 'could not resolve' in lowered:
                    category = 'not_found'
                else:
                    category = 'graphql_error'
                raise GitHubError(category, f'GraphQL failure during {operation}: {messages}', attempts=attempt)
            if not isinstance(data, dict):
                raise GitHubError('invalid_response', f'GitHub omitted GraphQL data during {operation}.', attempts=attempt)
            return data
        raise AssertionError('Retry loop exited unexpectedly.')

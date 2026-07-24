from __future__ import annotations


class OnboardError(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        category: str = "onboard_error",
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.exit_code = exit_code


class ConfigurationError(OnboardError):

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            category="configuration_error",
            exit_code=2,
        )


class ArtifactError(OnboardError):

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            category="invalid_inventory",
            exit_code=2,
        )


class GitHubRestError(OnboardError):

    def __init__(
        self,
        category: str,
        message: str,
        *,
        attempts: int,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            message,
            category=category,
            exit_code=2,
        )
        self.attempts = attempts
        self.status_code = status_code
        self.retryable = retryable


class RepositoryOperationError(OnboardError):

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            category="repository_operation_error",
            exit_code=1,
        )

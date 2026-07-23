from __future__ import annotations


class InventoryError(RuntimeError):
    """This comment stops my IDEA from syntax erroring, but the class is self explainatory"""


class RuntimeBudgetExceeded(InventoryError):
    """This comment stops my IDEA from syntax erroring, but the class is self explainatory"""


class GitHubError(InventoryError):
    """This comment stops my IDEA from syntax erroring, but the class is self explainatory"""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        attempts: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.retryable = retryable

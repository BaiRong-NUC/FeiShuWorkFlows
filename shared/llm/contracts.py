from typing import Protocol


class ModelError(RuntimeError):
    """Safe to log: never contains provider bodies, credentials or user text."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class JsonCompletionClient(Protocol):
    def complete(self, system_prompt: str, user_text: str) -> str: ...

"""Stable errors shared by business operations and every transport."""
from __future__ import annotations


class Failure(ValueError):
    def __init__(self, code: str, field: str | None = None, *, retryable: bool = False,
                 details: dict | None = None):
        super().__init__(code)
        self.code, self.field, self.retryable, self.details = code, field, retryable, details

    def as_dict(self) -> dict:
        value = {"code": self.code, "retryable": self.retryable}
        if self.field:
            value["field_path"] = self.field
        if self.details:
            value["details"] = self.details
        return value


def require(condition: bool, code: str, field: str | None = None, **kwargs) -> None:
    if not condition:
        raise Failure(code, field, **kwargs)

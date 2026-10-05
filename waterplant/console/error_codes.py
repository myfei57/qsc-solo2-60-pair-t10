"""Stable API error codes shared by every console module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Module(IntEnum):
    """Top three digits of a six digit error code.

    The registry is global; modules own fixed segments. Zero is reserved for
    transport and request-contract errors that do not belong to one module.
    """

    COMMON = 0
    HISTORY = 100
    INTAKE = 101
    COAGULATION = 102
    FLOW = 103
    CHLORINATION = 104
    CLEARWELL = 105
    FILTER = 106
    BACKWASH = 107
    QUOTA = 108
    CYCLE = 109
    PH = 110
    SCHEDULE = 111
    INVENTORY = 112
    OPS = 113
    METRICS = 114
    EXPORT = 115
    AUDIT = 116
    IDEMPOTENCY = 117


class Reason(IntEnum):
    """Last three digits of an error code."""

    MALFORMED_JSON = 1
    REQUEST_TYPE = 2
    MISSING_FIELD = 3
    INVALID_TYPE = 4
    VALUE_OUT_OF_RANGE = 5
    UNKNOWN_FIELD = 6
    NOT_FOUND = 7
    CONFLICT = 8
    PRECONDITION_FAILED = 9
    PARTIAL_FAILURE = 10
    IDEMPOTENCY_CONFLICT = 11
    INTERNAL = 500


@dataclass(frozen=True)
class ErrorCode:
    """A globally unique code and its operator-facing description."""

    code: int
    reason: Reason
    http_status: int
    message: str

    @property
    def code_text(self) -> str:
        return f"{self.code:06d}"


def error_code(module: Module, reason: Reason, http_status: int, message: str) -> ErrorCode:
    return ErrorCode(
        code=int(module) * 1000 + int(reason),
        reason=reason,
        http_status=http_status,
        message=message,
    )


def module_error(module: Module, reason: Reason, http_status: int | None = None) -> ErrorCode:
    """Build a canonical code with the fixed reason description."""

    messages = {
        Reason.MALFORMED_JSON: "request body is not valid JSON",
        Reason.REQUEST_TYPE: "request body must be a JSON object",
        Reason.MISSING_FIELD: "required field is missing",
        Reason.INVALID_TYPE: "field has the wrong type",
        Reason.VALUE_OUT_OF_RANGE: "field value is outside its allowed range",
        Reason.UNKNOWN_FIELD: "unknown field is not allowed by this contract",
        Reason.NOT_FOUND: "referenced resource was not found",
        Reason.CONFLICT: "request conflicts with the current state",
        Reason.PRECONDITION_FAILED: "a required precondition was not met",
        Reason.PARTIAL_FAILURE: "the operation stopped after some steps succeeded",
        Reason.IDEMPOTENCY_CONFLICT: "the idempotency key was reused with a different request",
        Reason.INTERNAL: "unexpected server error",
    }
    status = http_status or (400 if int(reason) < 500 else 500)
    if reason is Reason.NOT_FOUND:
        status = 404
    if reason in (Reason.CONFLICT, Reason.IDEMPOTENCY_CONFLICT):
        status = 409
    if reason is Reason.PRECONDITION_FAILED:
        status = 412
    if reason is Reason.PARTIAL_FAILURE:
        status = 202
    return error_code(module, reason, status, messages[reason])

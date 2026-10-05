"""Global error catalogue used by every console endpoint.

Codes are stable strings.  Their numeric namespace is fixed as well so logs and
external dashboards can reserve ranges without parsing the suffix:

* ``COMMON-0xxx``: transport and request-contract failures
* ``<MODULE>-1xxx``: input validation rejected before business code runs
* ``<MODULE>-2xxx``: business preconditions such as a missing resource
* ``<MODULE>-4xxx``: business processing / partial-failure and recovery
* ``COMMON-5xxx``: server and dependency failures
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    """One stable, documented response error."""

    code: str
    http_status: int
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": self.http_status,
            "message": self.message,
            "retryable": self.retryable,
        }


# Common transport/request contract: COMMON-0000..0099
INVALID_JSON = ErrorCode("COMMON-0001", 400, "request body is not valid JSON")
BODY_NOT_OBJECT = ErrorCode("COMMON-0002", 400, "request body must be a JSON object")
BODY_REQUIRED = ErrorCode("COMMON-0003", 400, "request body is required")
UNKNOWN_FIELD = ErrorCode("COMMON-0004", 400, "unknown request field")
MISSING_FIELD = ErrorCode("COMMON-0005", 400, "required field is missing")
NULL_FIELD = ErrorCode("COMMON-0006", 400, "field must not be null")
INVALID_TYPE = ErrorCode("COMMON-0007", 400, "field has the wrong type")
OUT_OF_RANGE = ErrorCode("COMMON-0008", 400, "field is outside its allowed range")
INVALID_VALUE = ErrorCode("COMMON-0009", 400, "field value is not allowed")
ROUTE_NOT_FOUND = ErrorCode("COMMON-0201", 404, "route does not exist")
METHOD_NOT_ALLOWED = ErrorCode("COMMON-0202", 405, "HTTP method is not allowed for this route")
IDEMPOTENCY_REQUIRED = ErrorCode("COMMON-0301", 400, "Idempotency-Key is required for this operation")
IDEMPOTENCY_CONFLICT = ErrorCode("COMMON-0302", 409, "Idempotency-Key is already used by a different request")
IDEMPOTENCY_IN_PROGRESS = ErrorCode(
    "COMMON-0303", 409, "an operation with this Idempotency-Key is still in progress", True
)

# Shared business preconditions and processing outcomes.
RESOURCE_NOT_FOUND = ErrorCode("COMMON-2001", 404, "referenced resource does not exist")
RESOURCE_CONFLICT = ErrorCode("COMMON-2002", 409, "resource state conflicts with the request")
PARTIAL_FAILURE = ErrorCode("COMMON-4001", 500, "the operation failed partway through and was recovered", True)
RECOVERY_FAILED = ErrorCode(
    "COMMON-4002", 500, "the operation failed partway through and recovery did not complete"
)
INTERNAL_ERROR = ErrorCode("COMMON-5001", 500, "unexpected server error", True)

# Module specific ranges.  Do not renumber an existing code.
INVENTORY_INSUFFICIENT = ErrorCode("INVENTORY-2001", 409, "chemical inventory is insufficient for the request")
FILTER_BED_NOT_FOUND = ErrorCode("FILTER-2001", 404, "filter bed does not exist")
FILTER_ZONE_EMPTY = ErrorCode("FILTER-2002", 404, "no filter bed currently occupies the requested zone")
FILTER_ZONE_CONFLICT = ErrorCode("FILTER-2003", 409, "filter zone is already assigned to another bed")


# Message fragments raised by the domain are mapped once, here, rather than by
# individual handlers.  This keeps the same condition on one HTTP status.
_DOMAIN_CODES = (
    ("insufficient", INVENTORY_INSUFFICIENT),
    ("no filter bed for zone", FILTER_ZONE_EMPTY),
    ("not found", FILTER_BED_NOT_FOUND),
)


def code_for_domain_error(exc: Exception) -> ErrorCode:
    """Map an unavoidable domain ``ValueError`` to its catalogue entry."""

    text = str(exc)
    for fragment, code in _DOMAIN_CODES:
        if fragment in text:
            return code
    return INVALID_VALUE


def error_payload(
    code: ErrorCode,
    *,
    request_id: str = "",
    message: str | None = None,
    details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Render the single error envelope used by all endpoints."""

    payload: dict[str, object] = {
        "error": {
            "code": code.code,
            "message": message or code.message,
            "request_id": request_id,
            "retryable": code.retryable,
        }
    }
    if details:
        payload["error"]["details"] = details
    return payload

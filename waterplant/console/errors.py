"""Mapping from domain exceptions to the published console error contract."""

from __future__ import annotations

from .error_codes import Module, Reason
from .http import ApiError


def business_error(exc: ValueError, module: Module = Module.COMMON) -> ApiError:
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered or "no filter bed" in lowered:
        return ApiError(module, Reason.NOT_FOUND, message)
    if "conflict" in lowered or "already" in lowered:
        return ApiError(module, Reason.CONFLICT, message)
    if "insufficient" in lowered:
        return ApiError(module, Reason.PRECONDITION_FAILED, message, status=400)
    if "range" in lowered or "must" in lowered:
        return ApiError(module, Reason.VALUE_OUT_OF_RANGE, message)
    return ApiError(module, Reason.PRECONDITION_FAILED, message)

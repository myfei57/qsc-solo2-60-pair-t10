"""Declarative request contracts and validation that runs before business code."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import (
    INVALID_TYPE,
    INVALID_VALUE,
    MISSING_FIELD,
    NULL_FIELD,
    OUT_OF_RANGE,
    UNKNOWN_FIELD,
    ErrorCode,
)

_MISSING = object()


class Importance(str, Enum):
    """Controls how omission is treated when no field default is declared."""

    CRITICAL = "critical"
    STANDARD = "standard"
    TOLERANT = "tolerant"


@dataclass(frozen=True)
class Field:
    """One typed and bounded JSON request field."""

    name: str
    kind: str
    required: bool = False
    default: Any = _MISSING
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    non_empty: bool = False
    item_kind: str | None = None

    def has_default(self) -> bool:
        return self.default is not _MISSING


@dataclass(frozen=True)
class EndpointContract:
    """Complete input contract for one method/path pair."""

    method: str
    path: str
    importance: Importance
    fields: tuple[Field, ...] = field(default_factory=tuple)
    body_required: bool = False
    idempotency_required: bool = False
    unknown_fields_rejected: bool = True

    def field(self, name: str) -> Field | None:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        return None


class ContractViolation(Exception):
    """A request that failed validation, before any business step was invoked."""

    def __init__(self, code: ErrorCode, message: str, details: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


def _detail(field_name: str, code: ErrorCode, message: str, value: Any = _MISSING) -> dict[str, object]:
    item: dict[str, object] = {"field": field_name, "code": code.code, "message": message}
    if value is not _MISSING:
        item["rejected_value"] = _safe_value(value)
    return item


def _safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coordinate(kind: str, value: Any) -> tuple[bool, Any]:
    if kind == "float":
        return _is_number(value), float(value) if _is_number(value) else None
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return False, None
        return True, value
    if kind == "str":
        if not isinstance(value, str):
            return False, None
        return True, value
    if kind == "bool":
        return isinstance(value, bool), value
    raise ValueError(f"unsupported contract type {kind}")


def _validate_scalar(spec: Field, value: Any, details: list[dict[str, object]]) -> Any:
    ok, coerced = _coordinate(spec.kind, value)
    if not ok:
        details.append(_detail(spec.name, INVALID_TYPE, f"{spec.name} must be {_type_name(spec.kind)}", value))
        return None

    if spec.kind in {"float", "int"}:
        numeric = float(coerced)
        if spec.minimum is not None and numeric < spec.minimum:
            details.append(
                _detail(
                    spec.name,
                    OUT_OF_RANGE,
                    f"{spec.name} must be >= {spec.minimum}",
                    value,
                )
            )
        if spec.maximum is not None and numeric > spec.maximum:
            details.append(
                _detail(
                    spec.name,
                    OUT_OF_RANGE,
                    f"{spec.name} must be <= {spec.maximum}",
                    value,
                )
            )

    if spec.kind == "str":
        text = coerced
        length = len(text)
        if spec.non_empty and not text:
            details.append(_detail(spec.name, INVALID_VALUE, f"{spec.name} must not be empty", value))
        if spec.min_length is not None and length < spec.min_length:
            details.append(_detail(spec.name, OUT_OF_RANGE, f"{spec.name} length must be >= {spec.min_length}", value))
        if spec.max_length is not None and length > spec.max_length:
            details.append(_detail(spec.name, OUT_OF_RANGE, f"{spec.name} length must be <= {spec.max_length}", value))

    return coerced


def _type_name(kind: str) -> str:
    return {
        "float": "a number",
        "int": "an integer",
        "str": "a string",
        "bool": "a boolean",
        "list": "a list",
    }.get(kind, kind)


def _validate_field(spec: Field, payload: dict[str, Any], importance: Importance,
                    details: list[dict[str, object]]) -> tuple[str, Any]:
    present = spec.name in payload
    if not present:
        if spec.has_default():
            return spec.name, spec.default
        if spec.required or importance is Importance.CRITICAL:
            details.append({"field": spec.name, "code": MISSING_FIELD.code, "message": f"{spec.name} is required"})
            return spec.name, None
        # Explicit contract decision for non-critical optional input: omission
        # is replaced by a safe, documented default rather than failing.
        return spec.name, _empty_default(spec.kind)

    value = payload[spec.name]
    if value is None:
        if spec.has_default():
            return spec.name, spec.default
        if not spec.required and importance is not Importance.CRITICAL:
            return spec.name, _empty_default(spec.kind)
        details.append(_detail(spec.name, NULL_FIELD, f"{spec.name} must not be null", None))
        return spec.name, None

    if spec.kind == "list":
        if not isinstance(value, list):
            details.append(
                _detail(
                    spec.name,
                    INVALID_TYPE,
                    f"{spec.name} must be a list of {_type_name(spec.item_kind or 'value')}s",
                    value,
                )
            )
            return spec.name, None
        items: list[Any] = []
        item_spec = Field(name=f"{spec.name}[]", kind=spec.item_kind or "str")
        for item in value:
            items.append(_validate_scalar(item_spec, item, details))
        if spec.non_empty and not value:
            details.append(_detail(spec.name, INVALID_VALUE, f"{spec.name} must not be empty", value))
        if spec.max_length is not None and len(value) > spec.max_length:
            details.append(_detail(spec.name, OUT_OF_RANGE, f"{spec.name} length must be <= {spec.max_length}", value))
        return spec.name, items

    return spec.name, _validate_scalar(spec, value, details)


def _empty_default(kind: str) -> Any:
    if kind == "float":
        return 0.0
    if kind == "int":
        return 0
    if kind == "list":
        return []
    if kind == "bool":
        return False
    return ""


def validate_request(contract: EndpointContract, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate all fields and return a complete, coerced parameter dict.

    All independent violations are reported in one response.  No component or
    store method is called by this function.
    """

    details: list[dict[str, object]] = []

    allowed = {spec.name for spec in contract.fields}
    if contract.unknown_fields_rejected:
        for name in sorted(set(payload) - allowed):
            details.append({"field": name, "code": UNKNOWN_FIELD.code, "message": f"unknown field {name}"})

    cleaned: dict[str, Any] = {}
    for spec in contract.fields:
        name, value = _validate_field(spec, payload, contract.importance, details)
        cleaned[name] = value

    if details:
        priority = (OUT_OF_RANGE.code, INVALID_TYPE.code, NULL_FIELD.code, MISSING_FIELD.code, UNKNOWN_FIELD.code)
        codes = [str(item["code"]) for item in details]
        top_code = next((code for code in priority if code in codes), INVALID_VALUE)
        catalogue_code = next(
            candidate for candidate in (OUT_OF_RANGE, INVALID_TYPE, NULL_FIELD, MISSING_FIELD, UNKNOWN_FIELD)
            if candidate.code == top_code
        )
        raise ContractViolation(catalogue_code, "request validation failed", details)
    return cleaned

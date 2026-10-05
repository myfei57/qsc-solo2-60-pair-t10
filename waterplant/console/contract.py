"""Declarative request contracts executed before business handlers run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from waterplant.chlor import validate_demand
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import validate_flow
from waterplant.inventory import validate_quantity
from waterplant.ph import validate_ph
from waterplant.quota import validate_amount
from waterplant.scheduler import validate_threshold

from .error_codes import Module, Reason

CRITICAL = "critical"
STANDARD = "standard"
BEST_EFFORT = "best_effort"

NUMBER = "number"
INTEGER = "integer"
STRING = "string"
LIST = "list"
UNSET = object()


class ValidationFailure(Exception):
    """All contract violations found before the business operation started."""

    def __init__(self, issues: list["ValidationIssue"]) -> None:
        super().__init__("request contract validation failed")
        self.issues = issues


@dataclass(frozen=True)
class ValidationIssue:
    module: Module
    reason: Reason
    field: str
    message: str

    @property
    def code(self) -> int:
        return int(self.module) * 1000 + int(self.reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": f"{self.code:06d}",
            "field": self.field,
            "reason": self.reason.name.lower(),
            "message": self.message,
        }


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool | None = None
    default: object = UNSET
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    item_kind: str | None = None
    validator: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class Contract:
    module: Module
    criticality: str
    fields: tuple[Field, ...] = field(default_factory=tuple)

    def field_map(self) -> dict[str, Field]:
        return {item.name: item for item in self.fields}

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module.name.lower(),
            "criticality": self.criticality,
            "missing_field_policy": missing_policy(self.criticality),
            "fields": [
                {
                    "name": item.name,
                    "type": item.kind,
                    "required": _is_required(item, self.criticality),
                    **({"default": item.default} if item.default is not UNSET else {}),
                }
                for item in self.fields
            ],
        }


def missing_policy(criticality: str) -> str:
    if criticality == CRITICAL:
        return "reject_request"
    if criticality == STANDARD:
        return "reject_when_no_default"
    return "use_default"


def _is_required(item: Field, criticality: str) -> bool:
    if item.required is not None:
        return item.required
    if criticality == CRITICAL:
        return True
    if criticality == BEST_EFFORT:
        return False
    return item.default is UNSET


def _issue(module: Module, reason: Reason, field_name: str, message: str) -> ValidationIssue:
    return ValidationIssue(module=module, reason=reason, field=field_name, message=message)


def _check_type(value: object, kind: str) -> bool:
    if kind == NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == STRING:
        return isinstance(value, str)
    if kind == LIST:
        return isinstance(value, list)
    return False


def _validate_scalar(
    module: Module, spec: Field, value: object, issues: list[ValidationIssue]
) -> object:
    if value is None or not _check_type(value, spec.kind):
        issues.append(
            _issue(
                module,
                Reason.INVALID_TYPE,
                spec.name,
                f"{spec.name} must be a {spec.kind}",
            )
        )
        return value

    numeric = float(value) if spec.kind in (NUMBER, INTEGER) else None
    if numeric is not None:
        if spec.minimum is not None and numeric < spec.minimum:
            issues.append(
                _issue(
                    module,
                    Reason.VALUE_OUT_OF_RANGE,
                    spec.name,
                    f"{spec.name} must be >= {spec.minimum}",
                )
            )
        if spec.maximum is not None and numeric > spec.maximum:
            issues.append(
                _issue(
                    module,
                    Reason.VALUE_OUT_OF_RANGE,
                    spec.name,
                    f"{spec.name} must be <= {spec.maximum}",
                )
            )

    if spec.kind == STRING:
        text = str(value)
        length = len(text)
        if spec.min_length is not None and length < spec.min_length:
            issues.append(
                _issue(module, Reason.VALUE_OUT_OF_RANGE, spec.name, f"{spec.name} is too short")
            )
        if spec.max_length is not None and length > spec.max_length:
            issues.append(
                _issue(module, Reason.VALUE_OUT_OF_RANGE, spec.name, f"{spec.name} is too long")
            )

    if spec.validator is not None and not issues:
        try:
            spec.validator(value)
        except ValueError as exc:
            issues.append(_issue(module, Reason.VALUE_OUT_OF_RANGE, spec.name, str(exc)))
    return value


def _type_default(kind: str) -> object:
    if kind == NUMBER:
        return 0.0
    if kind == INTEGER:
        return 0
    if kind == LIST:
        return []
    return ""


def validate_payload(contract: Contract, payload: dict[str, object]) -> dict[str, object]:
    """Validate and normalize one request; no business state is touched here."""

    allowed = contract.field_map()
    issues: list[ValidationIssue] = []
    for name in payload:
        if name not in allowed and name != "idempotency_key":
            issues.append(
                _issue(contract.module, Reason.UNKNOWN_FIELD, name, f"unknown field {name}")
            )

    normalized: dict[str, object] = {}
    for spec in contract.fields:
        present = spec.name in payload
        value = payload.get(spec.name)
        required = _is_required(spec, contract.criticality)
        if not present or value is None:
            if present and value is None:
                issues.append(
                    _issue(
                        contract.module,
                        Reason.INVALID_TYPE,
                        spec.name,
                        f"{spec.name} must be a {spec.kind}, not null",
                    )
                )
            elif required:
                issues.append(
                    _issue(
                        contract.module,
                        Reason.MISSING_FIELD,
                        spec.name,
                        f"{spec.name} is required",
                    )
                )
            else:
                default = spec.default
                if default is UNSET:
                    default = _type_default(spec.kind)
                normalized[spec.name] = default
            continue

        if spec.kind == LIST:
            if not isinstance(value, list):
                issues.append(
                    _issue(
                        contract.module,
                        Reason.INVALID_TYPE,
                        spec.name,
                        f"{spec.name} must be a list",
                    )
                )
                continue
            normalized_items: list[object] = []
            item_spec = Field(
                name=f"{spec.name}[]",
                kind=spec.item_kind or NUMBER,
                minimum=spec.minimum,
                maximum=spec.maximum,
                validator=spec.validator,
            )
            for _index, item in enumerate(value):
                before = len(issues)
                checked = _validate_scalar(contract.module, item_spec, item, issues)
                if len(issues) == before:
                    normalized_items.append(checked)
            normalized[spec.name] = normalized_items
        else:
            normalized[spec.name] = _validate_scalar(contract.module, spec, value, issues)

    if issues:
        raise ValidationFailure(issues)
    return normalized


def _f(name: str, kind: str, **kwargs: object) -> Field:
    return Field(name=name, kind=kind, **kwargs)  # type: ignore[arg-type]


def _contracts() -> dict[tuple[str, str], Contract]:
    common_non_negative = dict(minimum=0.0, maximum=1_000_000.0)
    return {
        ("POST", "/history"): Contract(
            Module.HISTORY,
            CRITICAL,
            (_f("kind", STRING, min_length=1, max_length=64), _f("value", STRING, required=False, default="", max_length=512)),
        ),
        ("POST", "/intake/flow"): Contract(
            Module.INTAKE,
            STANDARD,
            (
                _f("flow", NUMBER, required=False, default=0.0, validator=validate_flow),
                _f("turbidity", NUMBER, required=False, default=0.0, **common_non_negative),
            ),
        ),
        ("POST", "/coag/dose"): Contract(
            Module.COAGULATION, CRITICAL, (_f("flow", NUMBER, validator=validate_flow),)
        ),
        ("POST", "/coag/turbidity"): Contract(
            Module.COAGULATION,
            STANDARD,
            (_f("samples", LIST, required=False, default=[], item_kind=NUMBER, **common_non_negative),),
        ),
        ("POST", "/flow/replace"): Contract(
            Module.FLOW,
            CRITICAL,
            (
                _f("serial", STRING, min_length=1, max_length=64),
                _f("factor", NUMBER, validator=validate_factor),
            ),
        ),
        ("POST", "/chlor/target"): Contract(
            Module.CHLORINATION, CRITICAL, (_f("demand", NUMBER, validator=validate_demand),)
        ),
        ("POST", "/chlor/dose"): Contract(Module.CHLORINATION, CRITICAL),
        ("POST", "/clearwell/level"): Contract(
            Module.CLEARWELL, CRITICAL, (_f("target", NUMBER, validator=validate_level),)
        ),
        ("POST", "/filter/add"): Contract(
            Module.FILTER,
            CRITICAL,
            (
                _f("id", STRING, min_length=1, max_length=64),
                _f("zone", INTEGER, validator=validate_zone),
                _f("load", NUMBER, required=False, default=0.0, **common_non_negative),
            ),
        ),
        ("POST", "/filter/close"): Contract(
            Module.FILTER, CRITICAL, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/filter/open"): Contract(
            Module.FILTER, CRITICAL, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/filter/renumber"): Contract(
            Module.FILTER,
            CRITICAL,
            (
                _f("id", STRING, min_length=1, max_length=64),
                _f("zone", INTEGER, validator=validate_zone),
            ),
        ),
        ("POST", "/filter/load"): Contract(
            Module.FILTER,
            CRITICAL,
            (
                _f("id", STRING, min_length=1, max_length=64),
                _f("load", NUMBER, **common_non_negative),
            ),
        ),
        ("POST", "/filter/reset"): Contract(Module.FILTER, STANDARD),
        ("POST", "/filter/remove"): Contract(
            Module.FILTER, CRITICAL, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/filter/zone"): Contract(
            Module.FILTER, STANDARD, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/backwash/start"): Contract(
            Module.BACKWASH, CRITICAL, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/backwash/select"): Contract(
            Module.BACKWASH, CRITICAL, (_f("zone", INTEGER, validator=validate_zone),)
        ),
        ("POST", "/backwash/enqueue"): Contract(
            Module.BACKWASH, CRITICAL, (_f("id", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/backwash/recover"): Contract(Module.BACKWASH, STANDARD),
        ("POST", "/backwash/replay"): Contract(Module.BACKWASH, STANDARD),
        ("POST", "/quota/add"): Contract(
            Module.QUOTA, CRITICAL, (_f("amount", NUMBER, validator=validate_amount),)
        ),
        ("POST", "/quota/check"): Contract(
            Module.QUOTA,
            CRITICAL,
            (
                _f("chemical", STRING, min_length=1, max_length=64),
                _f("used", NUMBER, **common_non_negative),
                _f("limit", NUMBER, minimum=0.0, maximum=1_000_000.0),
            ),
        ),
        ("POST", "/audit/filter"): Contract(
            Module.AUDIT, CRITICAL, (_f("kind", STRING, min_length=1, max_length=64),)
        ),
        ("POST", "/cycle"): Contract(
            Module.CYCLE,
            STANDARD,
            (
                _f("flow", NUMBER, required=False, default=0.0, validator=validate_flow),
                _f("samples", LIST, required=False, default=[], item_kind=NUMBER, **common_non_negative),
                _f("demand", NUMBER, required=False, default=0.0, validator=validate_demand),
                _f("level", NUMBER, required=False, default=0.0, validator=validate_level),
                _f("bed_id", STRING, required=False, default="", max_length=64),
                _f("zone", INTEGER, required=False, default=0, validator=validate_zone),
                _f("amount", NUMBER, required=False, default=0.0, validator=validate_amount),
            ),
        ),
        ("POST", "/simulate"): Contract(
            Module.CYCLE,
            BEST_EFFORT,
            (_f("ticks", INTEGER, required=False, default=5, minimum=1, maximum=100),),
        ),
        ("POST", "/ph/read"): Contract(
            Module.PH, CRITICAL, (_f("value", NUMBER, validator=validate_ph),)
        ),
        ("POST", "/schedule/threshold"): Contract(
            Module.SCHEDULE, CRITICAL, (_f("threshold", NUMBER, validator=validate_threshold),)
        ),
        ("POST", "/schedule/plan"): Contract(Module.SCHEDULE, STANDARD),
        ("POST", "/intake/trend/window"): Contract(
            Module.INTAKE,
            STANDARD,
            (_f("window", INTEGER, required=False, default=12, minimum=1, maximum=1000),),
        ),
        ("POST", "/intake/trend/reset"): Contract(Module.INTAKE, STANDARD),
        ("POST", "/inventory/receive"): Contract(
            Module.INVENTORY,
            CRITICAL,
            (
                _f("chemical", STRING, min_length=1, max_length=64),
                _f("lot_id", STRING, min_length=1, max_length=64),
                _f("amount", NUMBER, validator=validate_quantity),
            ),
        ),
        ("POST", "/inventory/consume"): Contract(
            Module.INVENTORY,
            CRITICAL,
            (
                _f("chemical", STRING, min_length=1, max_length=64),
                _f("amount", NUMBER, validator=validate_quantity),
            ),
        ),
        ("POST", "/inventory/reorder-level"): Contract(
            Module.INVENTORY,
            CRITICAL,
            (_f("level", NUMBER, validator=lambda value: validate_quantity(value, "level")),),
        ),
        ("POST", "/metrics/reset"): Contract(Module.METRICS, STANDARD),
        ("POST", "/ops/reset-store"): Contract(Module.OPS, CRITICAL),
        ("POST", "/history/clear"): Contract(Module.HISTORY, CRITICAL),
    }


CONTRACTS = _contracts()


def contract_for(method: str, path: str) -> Contract | None:
    return CONTRACTS.get((method.upper(), path))

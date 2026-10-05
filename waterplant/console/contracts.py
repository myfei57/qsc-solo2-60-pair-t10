"""Route level request contracts.

Importance is an explicit, fixed decision for each endpoint:

* ``CRITICAL``: missing fields and null fields reject the whole operation.
* ``STANDARD``: declared required fields reject; undeclared omission uses a safe
  default only for a field that explicitly declares one.
* ``TOLERANT``: omission and null on optional fields use the documented default.
"""

from __future__ import annotations

from .contract import EndpointContract, Field, Importance


_NUM = "float"
_INT = "int"
_STR = "str"
_BOOL = "bool"
_LIST = "list"

_CONTRACTS: dict[tuple[str, str], EndpointContract] = {}


def _register(contract: EndpointContract) -> EndpointContract:
    _CONTRACTS[(contract.method, contract.path)] = contract
    return contract


def _get(path: str) -> EndpointContract:
    return _register(EndpointContract("GET", path, Importance.TOLERANT))


def _post(path: str, importance: Importance, fields: tuple[Field, ...], *,
          body_required: bool = False, idempotency_required: bool = False) -> EndpointContract:
    return _register(
        EndpointContract(
            "POST",
            path,
            importance,
            fields,
            body_required=body_required,
            idempotency_required=idempotency_required,
        )
    )


# Read-only routes accept no JSON fields. A non-empty JSON object on a GET is a
# contract violation rather than being silently ignored.
for _read_path in (
    "/health",
    "/health/checks",
    "/snapshot",
    "/describe",
    "/report",
    "/pipeline",
    "/catalog",
    "/ops",
    "/telemetry",
    "/version",
    "/system",
    "/history",
    "/intake/flow",
    "/coag/ratio",
    "/backwash/order",
    "/quota",
    "/audit",
    "/audit/summary",
    "/ph",
    "/ph/verdict",
    "/schedule",
    "/intake/trend",
    "/audit/totals",
    "/inventory",
    "/metrics",
    "/requests",
    "/export/audit",
    "/export/telemetry",
):
    _get(_read_path)


_post("/ops/reset-store", Importance.CRITICAL, (), idempotency_required=True)
_post(
    "/history",
    Importance.STANDARD,
    (
        Field("kind", _STR, required=True, non_empty=True, max_length=64),
        Field("value", _STR, default="", max_length=1024),
    ),
    body_required=True,
)
_post("/history/clear", Importance.STANDARD, (), idempotency_required=True)

_post(
    "/intake/flow",
    Importance.CRITICAL,
    (
        Field("flow", _NUM, required=True, minimum=0, maximum=1_000_000),
        Field("turbidity", _NUM, default=0.0, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/coag/dose",
    Importance.CRITICAL,
    (Field("flow", _NUM, required=True, minimum=0, maximum=1_000_000),),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/coag/turbidity",
    Importance.STANDARD,
    (Field("samples", _LIST, default=(), item_kind=_NUM, max_length=1024),),
)
_post(
    "/flow/replace",
    Importance.CRITICAL,
    (
        Field("serial", _STR, required=True, non_empty=True, max_length=128),
        Field("factor", _NUM, required=True, minimum=0.000001, maximum=100),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/chlor/target",
    Importance.CRITICAL,
    (Field("demand", _NUM, required=True, minimum=0, maximum=1000),),
    body_required=True,
    idempotency_required=True,
)
_post("/chlor/dose", Importance.STANDARD, (), idempotency_required=False)
_post(
    "/clearwell/level",
    Importance.CRITICAL,
    (Field("target", _NUM, required=True, minimum=0, maximum=1_000_000),),
    body_required=True,
    idempotency_required=True,
)

_post(
    "/filter/add",
    Importance.CRITICAL,
    (
        Field("id", _STR, required=True, non_empty=True, max_length=128),
        Field("zone", _INT, required=True, minimum=0, maximum=100_000),
        Field("load", _NUM, default=0.0, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
    idempotency_required=True,
)
for _bed_path in ("/filter/close", "/filter/open", "/filter/remove"):
    _post(
        _bed_path,
        Importance.STANDARD,
        (Field("id", _STR, required=True, non_empty=True, max_length=128),),
        body_required=True,
    )
_post(
    "/filter/renumber",
    Importance.CRITICAL,
    (
        Field("id", _STR, required=True, non_empty=True, max_length=128),
        Field("zone", _INT, required=True, minimum=0, maximum=100_000),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/filter/load",
    Importance.STANDARD,
    (
        Field("id", _STR, required=True, non_empty=True, max_length=128),
        Field("load", _NUM, required=True, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
)
_post("/filter/reset", Importance.STANDARD, (), idempotency_required=True)
_post(
    "/filter/zone",
    Importance.TOLERANT,
    (Field("id", _STR, default="", max_length=128),),
)

_post(
    "/backwash/start",
    Importance.CRITICAL,
    (Field("id", _STR, required=True, non_empty=True, max_length=128),),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/backwash/select",
    Importance.STANDARD,
    (Field("zone", _INT, required=True, minimum=0, maximum=100_000),),
    body_required=True,
)
_post(
    "/backwash/enqueue",
    Importance.STANDARD,
    (Field("id", _STR, required=True, non_empty=True, max_length=128),),
    body_required=True,
    idempotency_required=True,
)
_post("/backwash/recover", Importance.CRITICAL, (), idempotency_required=True)
_post("/backwash/replay", Importance.CRITICAL, (), idempotency_required=True)

_post(
    "/quota/add",
    Importance.CRITICAL,
    (Field("amount", _NUM, required=True, minimum=0, maximum=1_000_000),),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/quota/check",
    Importance.CRITICAL,
    (
        Field("chemical", _STR, required=True, non_empty=True, max_length=128),
        Field("used", _NUM, required=True, minimum=0, maximum=1_000_000),
        Field("limit", _NUM, required=True, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
)
_post(
    "/audit/filter",
    Importance.STANDARD,
    (Field("kind", _STR, required=True, non_empty=True, max_length=64),),
    body_required=True,
)

_post(
    "/cycle",
    Importance.CRITICAL,
    (
        Field("flow", _NUM, required=True, minimum=0, maximum=1_000_000),
        Field("samples", _LIST, required=True, item_kind=_NUM, non_empty=True, max_length=1024),
        Field("demand", _NUM, required=True, minimum=0, maximum=1000),
        Field("level", _NUM, required=True, minimum=0, maximum=1_000_000),
        Field("bed_id", _STR, default="", max_length=128),
        Field("zone", _INT, default=0, minimum=0, maximum=100_000),
        Field("amount", _NUM, required=True, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/simulate",
    Importance.TOLERANT,
    (Field("ticks", _INT, default=5, minimum=1, maximum=100),),
)
_post(
    "/ph/read",
    Importance.CRITICAL,
    (Field("value", _NUM, required=True, minimum=0, maximum=14),),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/schedule/threshold",
    Importance.CRITICAL,
    (Field("threshold", _NUM, required=True, minimum=0.000001, maximum=1_000_000),),
    body_required=True,
    idempotency_required=True,
)
_post("/schedule/plan", Importance.TOLERANT, ())
_post(
    "/intake/trend/window",
    Importance.STANDARD,
    (Field("window", _INT, required=True, minimum=1, maximum=1000),),
    body_required=True,
)
_post("/intake/trend/reset", Importance.STANDARD, (), idempotency_required=True)

_post(
    "/inventory/receive",
    Importance.CRITICAL,
    (
        Field("chemical", _STR, required=True, non_empty=True, max_length=128),
        Field("lot_id", _STR, required=True, non_empty=True, max_length=128),
        Field("amount", _NUM, required=True, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/inventory/consume",
    Importance.CRITICAL,
    (
        Field("chemical", _STR, required=True, non_empty=True, max_length=128),
        Field("amount", _NUM, required=True, minimum=0, maximum=1_000_000),
    ),
    body_required=True,
    idempotency_required=True,
)
_post(
    "/inventory/reorder-level",
    Importance.STANDARD,
    (Field("level", _NUM, required=True, minimum=0, maximum=1_000_000),),
    body_required=True,
)
_post("/metrics/reset", Importance.TOLERANT, (), idempotency_required=True)


def contract_for(method: str, path: str) -> EndpointContract | None:
    """Return the fixed contract for a route, or ``None`` for an unknown route."""

    return _CONTRACTS.get((method.upper(), path))


def all_contracts() -> list[EndpointContract]:
    return [_CONTRACTS[key] for key in sorted(_CONTRACTS)]

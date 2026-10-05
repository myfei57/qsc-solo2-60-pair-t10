"""One full treatment control cycle."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.quota import validate_amount

from .http import Request, Response, json_response
from .runtime import Runtime
from .saga import Saga, Step


def _record_intake(rt: Runtime, sensor: Sensor) -> bool:
    rt.flow_repository.record(sensor)
    rt.trend.record(sensor.flow)
    return True


def _operation_key(request: Request) -> str:
    if request.operation_key:
        return request.operation_key
    raw = json.dumps(
        {"path": request.path, "payload": request.payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "cycle:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _execute(rt: Runtime, request: Request, values: dict[str, Any]) -> None:
    flow = values["flow"]
    samples = values["samples"]
    demand = values["demand"]
    level = values["level"]
    bed_id = values["bed_id"]
    amount = values["amount"]
    operation_key = _operation_key(request)
    saga = Saga(rt.store, operation_key)

    mixed = mix(samples)
    saga.run(
        Step("record_intake", lambda: _record_intake(rt, Sensor(flow=flow, turbidity=mixed)))
    )
    values["coag_dose"] = saga.run(
        Step(
            "coagulant_dose",
            lambda: rt.coag_doser.update_flow_and_dose(
                flow, f"{operation_key}:coag" if request.operation_key else ""
            ),
        )
    )
    values["turb_dose"] = saga.run(Step("turbidity_dose", lambda: rt.sampler.judge(samples)))
    stable, adjustment = saga.run(
        Step(
            "ph_verdict",
            lambda: (lambda verdict: (verdict.stable, verdict.adjustment))(
                rt.stabilizer.stabilize()
            ),
        )
    )
    values["ph_stable"] = stable
    values["ph_adjustment"] = adjustment
    saga.run(Step("residual_target", lambda: rt.well.update_residual_demand(demand)))
    if stable:
        values["chlor_dose"] = saga.run(
            Step(
                "chlorine_dose",
                lambda: rt.chlor_doser.apply_residual(
                    f"{operation_key}:chlor" if request.operation_key else ""
                ),
            )
        )
    else:
        values["chlor_dose"] = 0.0
    values["min_level"] = saga.run(
        Step("clearwell_level", lambda: rt.well.adjust_level(level, rt.inlet, rt.outlet))
    )
    values["rotation"] = saga.run(Step("rotation", lambda: rt.backwash.order_rotation()))
    values["on_duty"] = rt.bank.on_duty() or ""
    if bed_id:
        saga.run(
            Step(
                f"backwash:{bed_id}",
                lambda: rt.backwash.start(bed_id),
                compensate=lambda: rt.bank.open(bed_id),
            )
        )
    values["quota_value"] = saga.run(Step("quota", lambda: rt.accumulator.add(amount)))
    values["completed_steps"] = saga.completed_steps()


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole line once, in production order."""

    flow = request.float_field("flow")
    samples = request.float_list("samples")
    demand = request.float_field("demand")
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)

    values: dict[str, Any] = {
        "flow": flow,
        "samples": samples,
        "demand": demand,
        "level": level,
        "bed_id": bed_id,
        "amount": amount,
    }
    try:
        _execute(rt, request, values)
    except ValueError as exc:
        return json_response(
            {
                "error": str(exc),
                "code": "109010",
                "message": "cycle stopped after one or more steps completed",
                "retry": "resubmit the same request; completed checkpoints will not run again",
                "operation_key": _operation_key(request),
            },
            202,
        )

    return json_response(
        {
            "coag_dose": values["coag_dose"],
            "turb_dose": values["turb_dose"],
            "chlor_dose": values["chlor_dose"],
            "min_level": values["min_level"],
            "level": rt.well.level(),
            "quota": values["quota_value"],
            "rotation": values["rotation"],
            "on_duty": values["on_duty"],
            "backwash": bed_id,
            "drains": rt.backwash.drain_count(),
            "audit_count": rt.auditor.count(),
            "ph_stable": values["ph_stable"],
            "ph_adjustment": values["ph_adjustment"],
            "completed_steps": values["completed_steps"],
        }
    )

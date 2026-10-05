"""WSGI application that serves the control console."""

from __future__ import annotations

import json
import uuid
from typing import Callable, Iterable

from waterplant.store import Store

from . import handlers
from .contract import ValidationFailure, contract_for, validate_payload
from .error_codes import Module, Reason, module_error
from .errors import business_error
from .http import (
    ApiError,
    Request,
    RequestError,
    Response,
    Router,
    build_request,
    error_response,
    json_response,
)
from .idempotency import IdempotencyConflict, IdempotencyStore
from .metrics import Metrics
from .request_audit import RequestAuditor
from .routes import register_routes
from .runtime import Runtime


class Server:
    """Owns the runtime state and dispatches console requests."""

    def __init__(self, store: Store) -> None:
        self.runtime = Runtime(store)
        self.router = Router()
        self.metrics = Metrics()
        self.request_auditor = RequestAuditor(store)
        self.idempotency = IdempotencyStore(store)
        register_routes(self.router, handlers)

    def respond(self, request: Request) -> Response:
        if not request.request_id:
            request = _with_request_id(request, uuid.uuid4().hex)
        response = self._route(request)
        request_id = response.request_id or request.request_id
        self.metrics.record(request.method, request.path, response.status)
        return _with_response_id(response, request_id)

    def _route(self, request: Request) -> Response:
        handler = self.router.match(request.method, request.path)
        if handler is None:
            if self.router.allows(request.path):
                response = error_response(
                    405,
                    f"method {request.method} not allowed for {request.path}",
                    module_error(Module.COMMON, Reason.PRECONDITION_FAILED).code,
                    request.request_id,
                )
            else:
                response = error_response(
                    404,
                    f"no route for {request.path}",
                    module_error(Module.COMMON, Reason.NOT_FOUND).code,
                    request.request_id,
                )
            self._audit_response(request, response, "rejected", response.body)
            return response

        contract = contract_for(request.method, request.path)
        try:
            if contract is not None:
                validated = validate_payload(contract, request.payload)
                request = Request(
                    method=request.method,
                    path=request.path,
                    query=request.query,
                    payload=request.payload,
                    validated=validated,
                    request_id=request.request_id,
                    idempotency_key=request.idempotency_key,
                    operation_key=request.operation_key,
                )

            # Validation has fully completed before this lookup. A duplicate is
            # returned from the durable operation record and business code is
            # not entered a second time.
            if request.method == "POST" and request.idempotency_key:
                fingerprint = self.idempotency.fingerprint(request.method, request.path, request.payload)
                cached = self.idempotency.get(request.idempotency_key, fingerprint)
                if cached is not None:
                    request = _with_request_id(request, cached.request_id)
                    response = _with_response_id(
                        json_response(cached.body, cached.status), cached.request_id
                    )
                    self._audit_response(
                        request,
                        response,
                        "idempotent_replay",
                        json.dumps(cached.body, ensure_ascii=False).encode("utf-8"),
                    )
                    return response

            response = handler(self, request)
        except IdempotencyConflict as exc:
            response = _api_error_response(
                ApiError(
                    Module.IDEMPOTENCY,
                    Reason.IDEMPOTENCY_CONFLICT,
                    f"idempotency key {exc} is already used by another request",
                ),
                request.request_id,
            )
            self._audit_response(request, response, "rejected", response.body)
            return response
        except ValidationFailure as exc:
            first = exc.issues[0]
            response = error_response(
                400,
                first.message,
                first.code,
                request.request_id,
                {"issues": [issue.as_dict() for issue in exc.issues]},
            )
            self._audit_response(request, response, "rejected", response.body, issues=exc.issues)
            return response
        except ApiError as exc:
            response = _api_error_response(exc, request.request_id)
            self._audit_response(request, response, "rejected", response.body)
            return response
        except ValueError as exc:
            response = _api_error_response(
                business_error(exc, contract.module if contract else Module.COMMON),
                request.request_id,
            )
            self._audit_response(request, response, "rejected", response.body)
            return response
        except Exception as exc:  # noqa: BLE001 - last resort for a live console
            canonical = ApiError(Module.COMMON, Reason.INTERNAL, str(exc))
            response = _api_error_response(canonical, request.request_id)
            self._audit_response(request, response, "error", response.body)
            return response

        if (
            request.method == "POST"
            and request.idempotency_key
            and response.status != 202
        ):
            body = _response_json(response)
            if isinstance(body, dict):
                self.idempotency.save(
                    request.idempotency_key,
                    self.idempotency.fingerprint(request.method, request.path, request.payload),
                    response.status,
                    body,
                    request.request_id,
                )
        outcome = "partial_failure" if response.status == 202 else "completed"
        self._audit_response(request, response, outcome, response.body)
        return response

    def _audit_response(
        self,
        request: Request,
        response: Response,
        outcome: str,
        body: bytes,
        issues: object | None = None,
    ) -> None:
        payload = _decode_body(body)
        code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
        if not code:
            code = "000000" if response.status < 400 else f"{response.status:06d}"
        reason = str(payload.get("message", payload.get("error", ""))) if isinstance(payload, dict) else ""
        issue_list: list[dict[str, object]] = []
        if issues is not None:
            issue_list = [
                issue.as_dict() if hasattr(issue, "as_dict") else dict(issue)
                for issue in issues  # type: ignore[union-attr]
            ]
        elif isinstance(payload, dict) and isinstance(payload.get("details"), dict):
            detail_issues = payload["details"].get("issues", [])
            if isinstance(detail_issues, list):
                issue_list = [item for item in detail_issues if isinstance(item, dict)]
        self.request_auditor.record(
            method=request.method,
            path=request.path,
            status=response.status,
            code=code,
            outcome=outcome,
            reason=reason,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            issues=issue_list,
        )

    def dispatch(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> Response:
        """Call a route directly, used by the test suite and CLI probes."""

        request = Request(
            method=method.upper(),
            path=path,
            query={},
            payload=dict(payload or {}),
            idempotency_key=str((payload or {}).get("idempotency_key", "")),
            operation_key=str((payload or {}).get("idempotency_key", "")),
        )
        return self.respond(request)

    def __call__(
        self, environ: dict[str, object], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        request_id = str(uuid.uuid4().hex)
        try:
            request = build_request(environ, request_id)
            response = self.respond(request)
        except RequestError as exc:
            response = _api_error_response(exc, request_id)
            self._audit_response(
                Request(
                    method=str(environ.get("REQUEST_METHOD", "GET")),
                    path=str(environ.get("PATH_INFO", "/") or "/"),
                    query={},
                    payload={},
                    request_id=request_id,
                ),
                response,
                "rejected",
                response.body,
            )
        status, headers = response.wsgi()
        start_response(status, headers)
        return [response.body]


def _with_request_id(request: Request, request_id: str) -> Request:
    return Request(
        method=request.method,
        path=request.path,
        query=request.query,
        payload=request.payload,
        validated=request.validated,
        request_id=request_id,
        idempotency_key=request.idempotency_key,
        operation_key=request.operation_key,
    )


def _with_response_id(response: Response, request_id: str) -> Response:
    return Response(
        status=response.status,
        body=response.body,
        content_type=response.content_type,
        request_id=request_id,
    )


def _api_error_response(error: ApiError, request_id: str) -> Response:
    status = error.status if error.status is not None else error_code_status(error)
    if status is None:
        status = error_code_status(error)
    return error_response(
        status,
        error.message,
        error.code,
        request_id,
        error.details,
    )


def error_code_status(error: ApiError) -> int:
    reason = error.reason
    if reason == Reason.NOT_FOUND:
        return 404
    if reason in (Reason.CONFLICT, Reason.IDEMPOTENCY_CONFLICT):
        return 409
    if reason == Reason.PRECONDITION_FAILED:
        return 412
    if reason == Reason.INTERNAL:
        return 500
    return 400


def _response_json(response: Response) -> object:
    if "application/json" not in response.content_type:
        return None
    try:
        return json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _decode_body(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def create_application(store: Store) -> Server:
    """Build a ready to serve console for the supplied store."""

    return Server(store)

"""WSGI application that serves the control console."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Callable, Iterable

from waterplant.request_context import RequestContext, bind_context, reset_context
from waterplant.store import Store

from . import handlers
from .contract import ContractViolation, validate_request
from .contracts import contract_for
from .errors import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_IN_PROGRESS,
    IDEMPOTENCY_REQUIRED,
    INTERNAL_ERROR,
    INVALID_JSON,
    METHOD_NOT_ALLOWED,
    PARTIAL_FAILURE,
    RECOVERY_FAILED,
    ROUTE_NOT_FOUND,
    ErrorCode,
    code_for_domain_error,
    error_payload,
)
from .http import Request, RequestError, Response, Router, build_request, json_response
from .idempotency import IdempotencyStore
from .journal import RequestJournal
from .metrics import Metrics
from .routes import register_routes
from .runtime import Runtime
from .uow import UnitOfWork

SUCCESS_CODE = "OK"
VALIDATION_STAGE = "validate"
BUSINESS_STAGE = "business"
TRANSPORT_STAGE = "transport"
IDEMPOTENCY_STAGE = "idempotency"


class Server:
    """Owns the runtime state and dispatches console requests."""

    def __init__(self, store: Store) -> None:
        self.runtime = Runtime(store)
        self.router = Router()
        self.metrics = Metrics()
        self.journal = RequestJournal(store)
        self.idempotency = IdempotencyStore(store)
        register_routes(self.router, handlers)

    def respond(self, request: Request) -> Response:
        request = self._with_correlation(request)
        response = self._route(request)
        self.metrics.record(request.method, request.path, response.status)
        return response

    def _route(self, request: Request) -> Response:
        handler = self.router.match(request.method, request.path)
        if handler is None:
            if self.router.allows(request.path):
                return self._finish_error(
                    request,
                    METHOD_NOT_ALLOWED,
                    TRANSPORT_STAGE,
                    f"method {request.method} not allowed for {request.path}",
                )
            return self._finish_error(
                request,
                ROUTE_NOT_FOUND,
                TRANSPORT_STAGE,
                f"no route for {request.path}",
            )

        contract = contract_for(request.method, request.path)
        if contract is None:  # Defensive: every registered route must have a contract.
            return self._finish_error(request, INTERNAL_ERROR, TRANSPORT_STAGE, "route contract is missing")

        try:
            cleaned = validate_request(contract, request.payload)
        except ContractViolation as exc:
            return self._finish_error(request, exc.code, VALIDATION_STAGE, exc.message, exc.details)

        if request.method == "POST" and contract.idempotency_required and not request.idempotency_key:
            return self._finish_error(
                request,
                IDEMPOTENCY_REQUIRED,
                IDEMPOTENCY_STAGE,
                "this mutating operation requires an Idempotency-Key",
            )

        replay: Response | None = None
        idempotency_lock = None
        idempotency_started_at: float | None = None
        unit = UnitOfWork(self.runtime.store, self.runtime.bank)
        unit.begin()
        if request.method == "POST" and request.idempotency_key:
            status, replay, idempotency_lock, idempotency_started_at = self._idempotency_gate(
                request, cleaned
            )
            if replay is not None:
                unit.rollback()
                if status == "replay":
                    self._record_replay(request, replay.status)
                elif status in {"conflict", "in_progress"}:
                    self._record_outcome(
                        request,
                        replay.status,
                        replay.body.decode("utf-8"),
                        IDEMPOTENCY_STAGE,
                    )
                if idempotency_lock is not None:
                    idempotency_lock.release()
                unit.commit()
                return replay

        request = Request(
            method=request.method,
            path=request.path,
            query=request.query,
            payload=request.payload,
            params=cleaned,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            headers=request.headers,
        )
        token = bind_context(RequestContext(request.request_id, request.idempotency_key))
        try:
            try:
                response = handler(self, request)
            except ValueError as exc:
                unit.rollback()
                if idempotency_lock is not None:
                    self.idempotency.discard(request.idempotency_key)
                code = code_for_domain_error(exc)
                response = self._error_response(request, code, BUSINESS_STAGE, str(exc))
                self._record_outcome(request, response.status, response.body.decode("utf-8"), BUSINESS_STAGE)
                return response
            except Exception as exc:  # noqa: BLE001 - last resort for a live console
                recovered = unit.rollback()
                if idempotency_lock is not None:
                    self.idempotency.discard(request.idempotency_key)
                code = PARTIAL_FAILURE if recovered else RECOVERY_FAILED
                response = self._error_response(
                    request, code, BUSINESS_STAGE, f"{code.message}: {exc}"
                )
                self._record_outcome(request, response.status, response.body.decode("utf-8"), BUSINESS_STAGE)
                return response

            if request.idempotency_key and idempotency_lock is not None:
                self.idempotency.finish(
                    request.idempotency_key,
                    request.method,
                    request.path,
                    self._fingerprint(cleaned),
                    response.status,
                    response.body.decode("utf-8"),
                    idempotency_started_at,
                )
            response = self._with_request_header(response, request.request_id)
            unit.commit()
            self._record_success(request, response.status)
            return response
        finally:
            reset_context(token)
            if idempotency_lock is not None:
                idempotency_lock.release()

    def _idempotency_gate(
        self, request: Request, cleaned: dict[str, object]
    ) -> tuple[str, Response | None, object, float | None]:
        import time

        lock = self.idempotency.lock_for(request.idempotency_key)
        if not lock.acquire():
            return (
                "in_progress",
                self._error_response(
                    request,
                    IDEMPOTENCY_IN_PROGRESS,
                    IDEMPOTENCY_STAGE,
                    "the original operation has not finished; retry it with the same key",
                ),
                None,
                None,
            )
        try:
            fingerprint = self._fingerprint(cleaned)
            record = self.idempotency.load(request.idempotency_key)
            if record is not None:
                if record.method != request.method or record.path != request.path or (
                    record.fingerprint != fingerprint
                ):
                    response = self._error_response(
                        request,
                        IDEMPOTENCY_CONFLICT,
                        IDEMPOTENCY_STAGE,
                        "Idempotency-Key was reused with a different operation or parameters",
                    )
                    lock.release()
                    return "conflict", response, None, None
                if record.status == "completed":
                    response = Response(
                        status=record.response_status,
                        body=record.response_body.encode("utf-8"),
                        content_type="application/json; charset=utf-8",
                        extra_headers=(("X-Request-Id", request.request_id), ("Idempotent-Replay", "true")),
                    )
                    lock.release()
                    return "replay", response, None, None
                if not self.idempotency.stale_in_progress(record):
                    response = self._error_response(
                        request,
                        IDEMPOTENCY_IN_PROGRESS,
                        IDEMPOTENCY_STAGE,
                        "the original operation has not finished; retry it with the same key",
                    )
                    lock.release()
                    return "in_progress", response, None, None

            started_at = time.time()
            self.idempotency.begin(request.idempotency_key, request.method, request.path, fingerprint)
            return "started", None, lock, started_at
        except Exception:
            lock.release()
            raise

    def dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Call a route directly, used by the test suite and CLI probes."""

        supplied = {str(key).title(): str(value) for key, value in (headers or {}).items()}
        request = Request(
            method=method.upper(),
            path=path,
            query={},
            payload=dict(payload or {}),
            request_id=supplied.get("X-Request-Id", ""),
            idempotency_key=supplied.get("Idempotency-Key", ""),
            headers=supplied,
        )
        return self.respond(request)

    def __call__(
        self, environ: dict[str, object], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/") or "/")
        request_id = str(environ.get("HTTP_X_REQUEST_ID", "")) or uuid.uuid4().hex
        idempotency_key = str(environ.get("HTTP_IDEMPOTENCY_KEY", ""))
        try:
            response = self.respond(build_request(environ))
        except RequestError as exc:
            code = exc.code or INVALID_JSON
            response = self._raw_error(code, request_id, TRANSPORT_STAGE, exc.message)
            self.metrics.record_error(code.code)
            self.journal.record(
                request_id=request_id,
                idempotency_key=idempotency_key,
                method=method,
                path=path,
                status=code.http_status,
                code=code.code,
                stage=TRANSPORT_STAGE,
                message=exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - malformed WSGI input must still render JSON
            response = self._raw_error(INTERNAL_ERROR, request_id, TRANSPORT_STAGE, str(exc))
            self.metrics.record_error(INTERNAL_ERROR.code)
            self.journal.record(
                request_id=request_id,
                idempotency_key=idempotency_key,
                method=method,
                path=path,
                status=INTERNAL_ERROR.http_status,
                code=INTERNAL_ERROR.code,
                stage=TRANSPORT_STAGE,
                message=str(exc),
            )
        status, headers = response.wsgi()
        start_response(status, headers)
        return [response.body]

    def _with_correlation(self, request: Request) -> Request:
        if request.request_id:
            return request
        return Request(
            method=request.method,
            path=request.path,
            query=request.query,
            payload=request.payload,
            params=request.params,
            request_id=uuid.uuid4().hex,
            idempotency_key=request.idempotency_key,
            headers=request.headers,
        )

    def _finish_error(
        self,
        request: Request,
        code: ErrorCode,
        stage: str,
        message: str,
        details: list[dict[str, object]] | None = None,
    ) -> Response:
        response = self._error_response(request, code, stage, message, details)
        self.metrics.record_error(code.code)
        self.journal.record(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            method=request.method,
            path=request.path,
            status=code.http_status,
            code=code.code,
            stage=stage,
            message=message,
            details=details or [],
        )
        return response

    def _error_response(
        self,
        target: Request | str,
        code: ErrorCode,
        stage: str,
        message: str,
        details: list[dict[str, object]] | None = None,
    ) -> Response:
        request_id = target.request_id if isinstance(target, Request) else target
        return self._raw_error(code, request_id, stage, message, details)

    def _record_outcome(
        self,
        request: Request,
        status: int,
        body: str,
        stage: str,
    ) -> None:
        code = SUCCESS_CODE
        message = "accepted"
        details: list[dict[str, object]] = []
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            error = parsed["error"]
            code = str(error.get("code", ""))
            message = str(error.get("message", ""))
            raw_details = error.get("details", [])
            if isinstance(raw_details, list):
                details = [item for item in raw_details if isinstance(item, dict)]
        elif status >= 400:
            code = INTERNAL_ERROR.code
            message = body
        if status >= 400:
            self.metrics.record_error(code)
        self.journal.record(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            method=request.method,
            path=request.path,
            status=status,
            code=code,
            stage=stage,
            message=message,
            details=details,
        )

    def _raw_error(
        self,
        code: ErrorCode,
        request_id: str,
        stage: str,
        message: str,
        details: list[dict[str, object]] | None = None,
    ) -> Response:
        payload = error_payload(code, request_id=request_id, message=message, details=details)
        payload["error"]["stage"] = stage
        return json_response(
            payload,
            code.http_status,
            (("X-Request-Id", request_id),),
        )

    def _record_success(self, request: Request, status: int) -> None:
        self.journal.record(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            method=request.method,
            path=request.path,
            status=status,
            code=SUCCESS_CODE,
            stage=BUSINESS_STAGE,
            message="accepted",
            details=[],
        )

    def _record_replay(self, request: Request, status: int) -> None:
        self.journal.record(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            method=request.method,
            path=request.path,
            status=status,
            code="IDEMPOTENCY-REPLAY",
            stage=IDEMPOTENCY_STAGE,
            message="returned the stored response without running business steps",
            details=[],
        )

    def _with_request_header(self, response: Response, request_id: str) -> Response:
        if any(name.lower() == "x-request-id" for name, _ in response.extra_headers):
            return response
        return Response(
            status=response.status,
            body=response.body,
            content_type=response.content_type,
            extra_headers=(*response.extra_headers, ("X-Request-Id", request_id)),
        )

    @staticmethod
    def _fingerprint(cleaned: dict[str, object]) -> str:
        canonical = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_application(store: Store) -> Server:
    """Build a ready to serve console for the supplied store."""

    return Server(store)

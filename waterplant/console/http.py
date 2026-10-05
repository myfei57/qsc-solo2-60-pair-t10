"""Minimal WSGI toolkit: routing, requests, responses and JSON helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs

from .error_codes import Module, Reason, error_code, module_error

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server

STATUS_TEXT = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    412: "Precondition Failed",
    500: "Internal Server Error",
}


@dataclass
class ApiError(Exception):
    """Business-layer failure carrying a fixed global/module error code."""

    module: Module
    reason: Reason
    message: str
    status: int | None = None
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def code(self) -> int:
        return int(self.module) * 1000 + int(self.reason)

    def payload(self, request_id: str = "") -> dict[str, object]:
        body: dict[str, object] = {
            "error": self.message,
            "code": f"{self.code:06d}",
            "message": self.message,
            "request_id": request_id,
        }
        if self.details:
            body["details"] = self.details
        return body


class RequestError(ApiError):
    """Backwards-compatible exception used by low level HTTP parsing."""

    def __init__(
        self,
        status: int,
        message: str,
        reason: Reason = Reason.INTERNAL,
        module: Module = Module.COMMON,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(module=module, reason=reason, message=message, status=status, details=details or {})


@dataclass(frozen=True)
class Response:
    """A rendered HTTP response."""

    status: int
    body: bytes
    content_type: str
    request_id: str = ""

    def wsgi(self) -> tuple[str, list[tuple[str, str]]]:
        reason = STATUS_TEXT.get(self.status, "Unknown")
        headers = [
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            ("X-Request-Id", self.request_id),
        ]
        return f"{self.status} {reason}", headers


def json_response(payload: object, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(status=status, body=body, content_type="application/json; charset=utf-8")


def text_response(text: str, status: int = 200) -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type="text/plain; charset=utf-8")


def csv_response(text: str, status: int = 200) -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type="text/csv; charset=utf-8")


def error_response(
    status: int,
    message: str,
    code: int | None = None,
    request_id: str = "",
    details: dict[str, object] | None = None,
) -> Response:
    resolved_code = code if code is not None else _default_code(status)
    payload: dict[str, object] = {
        "error": message,
        "code": f"{resolved_code:06d}",
        "message": message,
        "request_id": request_id,
    }
    if details:
        payload["details"] = details
    return json_response(payload, status)


def _default_code(status: int) -> int:
    if status == 404:
        return module_error(Module.COMMON, Reason.NOT_FOUND).code
    if status == 405:
        return error_code(Module.COMMON, Reason.PRECONDITION_FAILED, 405, "method not allowed").code
    if status >= 500:
        return module_error(Module.COMMON, Reason.INTERNAL).code
    return module_error(Module.COMMON, Reason.INVALID_TYPE).code


def _to_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(400, f"field {name} must be a number", Reason.INVALID_TYPE)
    return float(value)


def _to_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise RequestError(400, f"field {name} must be an integer", Reason.INVALID_TYPE)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise RequestError(400, f"field {name} must be an integer", Reason.INVALID_TYPE)


@dataclass(frozen=True)
class Request:
    """A parsed inbound request."""

    method: str
    path: str
    query: dict[str, list[str]]
    payload: dict[str, object]
    validated: dict[str, object] = field(default_factory=dict)
    request_id: str = ""
    idempotency_key: str = ""
    operation_key: str = ""

    def data(self, name: str) -> object:
        if name in self.validated:
            return self.validated[name]
        return self.payload.get(name)

    def float_field(self, name: str, default: float = 0.0) -> float:
        if name not in self.payload and name not in self.validated:
            return default
        return _to_float(name, self.data(name))

    def int_field(self, name: str, default: int = 0) -> int:
        if name not in self.payload and name not in self.validated:
            return default
        return _to_int(name, self.data(name))

    def str_field(self, name: str, default: str = "") -> str:
        if name not in self.payload and name not in self.validated:
            return default
        value = self.data(name)
        if value is None:
            return default
        if isinstance(value, str):
            return value
        return str(value)

    def float_list(self, name: str) -> list[float]:
        if name not in self.payload and name not in self.validated:
            return []
        value = self.data(name)
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise RequestError(400, f"field {name} must be a list of numbers", Reason.INVALID_TYPE)
        return [_to_float(name, item) for item in value]


Handler = Callable[["Server", Request], Response]


class Router:
    """Exact match router keyed by method and path."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}

    def add(self, method: str, path: str, handler: Handler) -> None:
        self._routes[(method.upper(), path)] = handler

    def match(self, method: str, path: str) -> Handler | None:
        return self._routes.get((method.upper(), path))

    def allows(self, path: str) -> bool:
        return any(route_path == path for _, route_path in self._routes)

    def paths(self) -> list[str]:
        return sorted({route_path for _, route_path in self._routes})


def build_request(environ: dict[str, object], request_id: str = "") -> Request:
    """Turn a WSGI environ into a parsed :class:`Request`."""

    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = str(environ.get("PATH_INFO", "/") or "/")
    query = parse_qs(str(environ.get("QUERY_STRING", "")))
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    stream = environ.get("wsgi.input")
    raw = stream.read(length) if length and hasattr(stream, "read") else b""
    payload: dict[str, object] = {}
    if raw:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestError(
                400,
                f"invalid JSON body: {exc}",
                Reason.MALFORMED_JSON,
                details={"reason": "malformed_json"},
            ) from exc
        if not isinstance(decoded, dict):
            raise RequestError(400, "JSON body must be an object", Reason.REQUEST_TYPE)
        payload = decoded
    idempotency_key = str(payload.get("idempotency_key") or "")
    return Request(
        method=method,
        path=path,
        query=query,
        payload=payload,
        request_id=request_id,
        idempotency_key=idempotency_key,
        operation_key=idempotency_key,
    )

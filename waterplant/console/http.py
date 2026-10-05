"""Minimal WSGI toolkit: routing, requests, responses and JSON helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs

from .errors import BODY_NOT_OBJECT, INVALID_JSON, INVALID_TYPE, ErrorCode

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}


class RequestError(Exception):
    """Raised by request parsing with a stable catalogue error code."""

    def __init__(self, status: int, message: str, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Response:
    """A rendered HTTP response."""

    status: int
    body: bytes
    content_type: str
    extra_headers: tuple[tuple[str, str], ...] = ()

    def wsgi(self) -> tuple[str, list[tuple[str, str]]]:
        reason = STATUS_TEXT.get(self.status, "Unknown")
        headers = [
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            *self.extra_headers,
        ]
        return f"{self.status} {reason}", headers


def json_response(
    payload: object,
    status: int = 200,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(
        status=status,
        body=body,
        content_type="application/json; charset=utf-8",
        extra_headers=extra_headers,
    )


def text_response(text: str, status: int = 200) -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type="text/plain; charset=utf-8")


def csv_response(text: str, status: int = 200) -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type="text/csv; charset=utf-8")


def error_response(status: int, message: str) -> Response:
    return json_response({"error": message}, status=status)


def _to_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RequestError(400, f"field {name} must be a number", INVALID_TYPE)
    try:
        return float(value)
    except ValueError as exc:
        raise RequestError(400, f"field {name} must be a number", INVALID_TYPE) from exc


def _to_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise RequestError(400, f"field {name} must be an integer", INVALID_TYPE)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise RequestError(400, f"field {name} must be an integer", INVALID_TYPE) from exc
    raise RequestError(400, f"field {name} must be an integer", INVALID_TYPE)


@dataclass(frozen=True)
class Request:
    """A parsed inbound request."""

    method: str
    path: str
    query: dict[str, list[str]]
    payload: dict[str, object]
    params: dict[str, object] | None = None
    request_id: str = ""
    idempotency_key: str = ""
    headers: dict[str, str] | None = None

    def _value(self, name: str, default: object) -> object:
        if self.params is not None and name in self.params:
            return self.params[name]
        return self.payload.get(name, default)

    def float_field(self, name: str, default: float = 0.0) -> float:
        return _to_float(name, self._value(name, default))

    def int_field(self, name: str, default: int = 0) -> int:
        return _to_int(name, self._value(name, default))

    def str_field(self, name: str, default: str = "") -> str:
        value = self._value(name, default)
        if value is None:
            return default
        if isinstance(value, str):
            return value
        return str(value)

    def float_list(self, name: str) -> list[float]:
        if self.params is not None and name in self.params:
            value = self.params[name]
        else:
            value = self.payload.get(name, [])
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise RequestError(
                400, f"field {name} must be a list of numbers", INVALID_TYPE
            )
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


def build_request(environ: dict[str, object]) -> Request:
    """Turn a WSGI environ into a parsed :class:`Request`."""

    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = str(environ.get("PATH_INFO", "/") or "/")
    query = parse_qs(str(environ.get("QUERY_STRING", "")))
    headers = {
        str(key)[5:].replace("_", "-").title(): str(value)
        for key, value in environ.items()
        if str(key).startswith("HTTP_")
    }
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
            raise RequestError(INVALID_JSON.http_status, INVALID_JSON.message, INVALID_JSON) from exc
        if not isinstance(decoded, dict):
            raise RequestError(
                BODY_NOT_OBJECT.http_status, BODY_NOT_OBJECT.message, BODY_NOT_OBJECT
            )
        payload = decoded
    return Request(
        method=method,
        path=path,
        query=query,
        payload=payload,
        request_id=headers.get("X-Request-Id", ""),
        idempotency_key=headers.get("Idempotency-Key", ""),
        headers=headers,
    )

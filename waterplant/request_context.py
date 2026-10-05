"""Process-local correlation context shared by domain services."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """Correlation fields attached to domain audit records."""

    request_id: str = ""
    idempotency_key: str = ""


_CURRENT: ContextVar[RequestContext] = ContextVar("request_context", default=RequestContext())


def current_context() -> RequestContext:
    return _CURRENT.get()


def bind_context(context: RequestContext) -> Token[RequestContext]:
    return _CURRENT.set(context)


def reset_context(token: Token[RequestContext]) -> None:
    _CURRENT.reset(token)

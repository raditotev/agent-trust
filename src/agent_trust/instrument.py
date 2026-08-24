from __future__ import annotations

import functools
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from agent_trust.logging_config import bind_request_context, clear_request_context
from agent_trust.metrics import (
    HTTP_IN_FLIGHT,
    HTTP_REQUESTS_TOTAL,
    JOB_DURATION_SECONDS,
    JOB_ERRORS_TOTAL,
    JOB_RUNS_TOTAL,
    MCP_REQUESTS_TOTAL,
    TOOL_CALLS_TOTAL,
    TOOL_DURATION_SECONDS,
    TOOL_ERRORS_TOTAL,
    TOOL_IN_FLIGHT,
)

# Kwargs that signal the call carried credentials — not the caller's identity
# (see bind_request_context), just whether it was authenticated at all.
_AUTH_KWARGS = ("access_token", "public_key_hex")


def track_tool_call[F: Callable[..., Coroutine[Any, Any, Any]]](fn: F) -> F:
    """Decorator that records Prometheus metrics for an async MCP tool function.

    Tracks:
    - Total calls (labelled by tool_name and status: "success" or "error")
    - Call duration histogram (labelled by tool_name and status)
    - Error count (labelled by tool_name and error_type)
    - Calls currently in flight (labelled by tool_name)
    - Binds a per-call request_id + tool_name + authenticated flag via
      structlog contextvars, so every log line emitted during this call
      carries the same correlation id. Cleared in `finally` so it never
      leaks into an unrelated call on the same worker.

    Preserves the original function's name, docstring, and signature so that
    FastMCP can introspect them correctly when registering the tool.
    """
    tool_name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        status = "success"
        authenticated = any(kwargs.get(k) for k in _AUTH_KWARGS)
        auth_label = str(authenticated).lower()
        bind_request_context(
            request_id=str(uuid.uuid4()), tool_name=tool_name, authenticated=authenticated
        )
        TOOL_IN_FLIGHT.labels(tool_name=tool_name).inc()
        try:
            result = await fn(*args, **kwargs)
            if isinstance(result, dict) and "error_code" in result:
                status = "error"
                TOOL_CALLS_TOTAL.labels(
                    tool_name=tool_name, status="error", authenticated=auth_label
                ).inc()
                TOOL_ERRORS_TOTAL.labels(
                    tool_name=tool_name, error_type=result["error_code"]
                ).inc()
            else:
                TOOL_CALLS_TOTAL.labels(
                    tool_name=tool_name, status="success", authenticated=auth_label
                ).inc()
            return result
        except Exception as exc:
            status = "error"
            error_type = type(exc).__name__
            TOOL_CALLS_TOTAL.labels(
                tool_name=tool_name, status="error", authenticated=auth_label
            ).inc()
            TOOL_ERRORS_TOTAL.labels(tool_name=tool_name, error_type=error_type).inc()
            raise
        finally:
            TOOL_IN_FLIGHT.labels(tool_name=tool_name).dec()
            TOOL_DURATION_SECONDS.labels(tool_name=tool_name, status=status).observe(
                time.perf_counter() - start
            )
            clear_request_context()

    return wrapper  # type: ignore[return-value]


def track_job[F: Callable[..., Coroutine[Any, Any, Any]]](fn: F) -> F:
    """Decorator for arq background job functions (see workers/__init__.py) —
    a sibling to track_tool_call rather than a reuse of it, because these
    aren't MCP tool calls: a separate JOB_* metric family keeps that
    distinction out of TOOL_CALLS_TOTAL's labels.

    arq's own retry/failure semantics are exception-based (a job either
    returns or raises — there's no `error_code` dict convention here the way
    tools have), so status is derived from whether the call raised.
    """
    job_name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        status = "success"
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            status = "error"
            JOB_ERRORS_TOTAL.labels(job_name=job_name, error_type=type(exc).__name__).inc()
            raise
        finally:
            JOB_RUNS_TOTAL.labels(job_name=job_name, status=status).inc()
            JOB_DURATION_SECONDS.labels(job_name=job_name, status=status).observe(
                time.perf_counter() - start
            )

    return wrapper  # type: ignore[return-value]


def instrument_mcp_handlers(mcp: Any) -> None:  # noqa: ANN401 - FastMCP has no public type
    """Wrap every JSON-RPC request handler FastMCP registered on its low-level
    Server, so MCP_REQUESTS_TOTAL counts protocol-level traffic — tools/list,
    resources/*, prompts/*, tools/call — not just individual tool calls.
    `initialize` is handled by the session itself and never reaches
    request_handlers, so it isn't visible here; the HTTP middleware covers it.

    tools/call is deliberately counted both here (protocol-level, one counter
    per request regardless of which tool) and by track_tool_call (per-tool
    detail) — they answer different questions.

    Call once, after all tools/resources/prompts are registered (request_handlers
    is only populated as each @mcp.tool()/@mcp.resource()/@mcp.prompt() runs).

    Depends on mcp.server.lowlevel.Server internals (request_handlers is not
    part of the MCP SDK's public API) — see the self-check below, which fails
    loudly if a future SDK version removes or renames it.
    """
    server = mcp._mcp_server  # noqa: SLF001 - no public accessor for the low-level server
    for req_type, handler in list(server.request_handlers.items()):
        # Idempotent: `mcp` is a module-level singleton, so anything that
        # builds the app more than once in a process (test fixtures calling
        # create_app() per test, most commonly) would otherwise stack a new
        # counting layer each time and count every request once per layer.
        if getattr(handler, "_metrics_wrapped", False):
            continue
        method = req_type.model_fields["method"].default

        @functools.wraps(handler)
        async def wrapped(req: Any, _handler: Any = handler, _method: str = method) -> Any:
            try:
                result = await _handler(req)
                MCP_REQUESTS_TOTAL.labels(method=_method, status="success").inc()
                return result
            except Exception:
                MCP_REQUESTS_TOTAL.labels(method=_method, status="error").inc()
                raise

        wrapped._metrics_wrapped = True  # type: ignore[attr-defined]
        server.request_handlers[req_type] = wrapped


class MetricsMiddleware:
    """Pure ASGI middleware recording HTTP_REQUESTS_TOTAL / HTTP_IN_FLIGHT for
    every request on the streamable-http transport — /health, /metrics,
    /mcp's initialize handshake, and any transport-level 4xx/5xx that never
    reaches an MCP handler at all.

    `path` is the matched Starlette route template (e.g. "/mcp"), not the raw
    URL, so cardinality stays bounded regardless of what a client sends. Reads
    `scope["route"]` after the inner app runs — Starlette's router sets it on
    the same scope dict during dispatch, before the endpoint is called.
    """

    def __init__(self, app: Any) -> None:  # noqa: ANN401 - ASGI app, no shared base type
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        HTTP_IN_FLIGHT.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_FLIGHT.dec()
            route = scope.get("route")
            # No matched route (404, or a path handled inside a Mount) leaves
            # scope["route"] unset. Bucket those under one constant rather than
            # the raw URL: this app answers unauthenticated requests for any
            # path, so a scanner walking URLs would otherwise mint one
            # permanent Prometheus series per URL it tries.
            path = route.path if route is not None else "<unmatched>"
            HTTP_REQUESTS_TOTAL.labels(
                method=scope["method"], path=path, status=status_code
            ).inc()


def _demo() -> None:
    """Self-check: handled tool_error() dicts must count as status=error, not
    success, and instrument_mcp_handlers must find a real request_handlers
    dict to wrap (fails loudly if the MCP SDK ever renames/removes it)."""
    import asyncio

    from mcp.server.fastmcp import FastMCP

    from agent_trust.errors import tool_error

    async def ok_tool() -> dict:
        return {"ok": True}

    async def failing_tool() -> dict:
        return tool_error("rate_limit_exceeded", "too many requests")

    async def raising_tool() -> dict:
        raise ValueError("boom")

    asyncio.run(track_tool_call(ok_tool)())
    before = TOOL_CALLS_TOTAL.labels(
        tool_name="failing_tool", status="error", authenticated="false"
    )._value.get()
    asyncio.run(track_tool_call(failing_tool)())
    after = TOOL_CALLS_TOTAL.labels(
        tool_name="failing_tool", status="error", authenticated="false"
    )._value.get()
    assert after == before + 1, "tool_error() dict must increment status=error"
    assert (
        TOOL_ERRORS_TOTAL.labels(
            tool_name="failing_tool", error_type="rate_limit_exceeded"
        )._value.get()
        == 1
    )
    assert TOOL_IN_FLIGHT.labels(tool_name="ok_tool")._value.get() == 0, (
        "in-flight gauge must return to 0 after completion"
    )

    async def authed_tool(access_token: str | None = None) -> dict:
        return {"ok": True}

    asyncio.run(track_tool_call(authed_tool)(access_token="tok"))
    assert (
        TOOL_CALLS_TOTAL.labels(
            tool_name="authed_tool", status="success", authenticated="true"
        )._value.get()
        == 1
    ), "access_token kwarg must be reflected as authenticated=true"

    try:
        asyncio.run(track_tool_call(raising_tool)())
    except ValueError:
        pass
    assert (
        TOOL_CALLS_TOTAL.labels(
            tool_name="raising_tool", status="error", authenticated="false"
        )._value.get()
        == 1
    )

    demo_mcp = FastMCP("demo")

    @demo_mcp.tool()
    def noop() -> str:
        return "ok"

    instrument_mcp_handlers(demo_mcp)
    wrapped_count = sum(
        1
        for h in demo_mcp._mcp_server.request_handlers.values()  # noqa: SLF001
        if getattr(h, "__wrapped__", None) is not None
    )
    assert wrapped_count > 0, "instrument_mcp_handlers found nothing to wrap"

    async def ok_job(ctx: dict) -> dict:
        return {"done": True}

    async def failing_job(ctx: dict) -> None:
        raise RuntimeError("boom")

    asyncio.run(track_job(ok_job)({}))
    assert JOB_RUNS_TOTAL.labels(job_name="ok_job", status="success")._value.get() == 1

    try:
        asyncio.run(track_job(failing_job)({}))
    except RuntimeError:
        pass
    assert JOB_RUNS_TOTAL.labels(job_name="failing_job", status="error")._value.get() == 1
    assert (
        JOB_ERRORS_TOTAL.labels(job_name="failing_job", error_type="RuntimeError")._value.get()
        == 1
    )

    print("instrument.py self-check passed")


if __name__ == "__main__":
    _demo()

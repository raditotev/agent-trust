from __future__ import annotations

import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any

from agent_trust.metrics import TOOL_CALLS_TOTAL, TOOL_DURATION_SECONDS, TOOL_ERRORS_TOTAL


def track_tool_call[F: Callable[..., Coroutine[Any, Any, Any]]](fn: F) -> F:
    """Decorator that records Prometheus metrics for an async MCP tool function.

    Tracks:
    - Total calls (labelled by tool_name and status: "success" or "error")
    - Call duration histogram (labelled by tool_name)
    - Error count (labelled by tool_name and error_type)

    Preserves the original function's name, docstring, and signature so that
    FastMCP can introspect them correctly when registering the tool.
    """
    tool_name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = await fn(*args, **kwargs)
            TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="success").inc()
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="error").inc()
            TOOL_ERRORS_TOTAL.labels(tool_name=tool_name, error_type=error_type).inc()
            raise
        finally:
            TOOL_DURATION_SECONDS.labels(tool_name=tool_name).observe(
                time.perf_counter() - start
            )

    return wrapper  # type: ignore[return-value]

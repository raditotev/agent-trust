from __future__ import annotations


def tool_error(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    **extra: object,
) -> dict:
    """Build a structured error response for MCP tools.

    All error responses include:
    - error_code: machine-readable error identifier (e.g. "rate_limit_exceeded")
    - error: human/LLM-readable description
    - hint: actionable guidance for what the agent should do next

    Additional keyword arguments are merged into the response dict.
    """
    result: dict = {
        "error_code": code,
        "error": message,
    }
    if hint:
        result["hint"] = hint
    result.update(extra)
    return result


# Common error codes as constants for consistency
RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
AUTHENTICATION_REQUIRED = "authentication_required"
AUTHENTICATION_FAILED = "authentication_failed"
AUTHORIZATION_FAILED = "authorization_failed"
INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
DUPLICATE = "duplicate"
LIMIT_REACHED = "limit_reached"
COOLDOWN_ACTIVE = "cooldown_active"
COMPUTE_FAILED = "compute_failed"
SIGNING_FAILED = "signing_failed"
BLOCKED = "blocked"

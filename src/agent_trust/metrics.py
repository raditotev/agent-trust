from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Histogram

# Total MCP tool invocations, labelled by tool name and outcome status.
TOOL_CALLS_TOTAL = Counter(
    "agent_trust_tool_calls_total",
    "Total number of MCP tool invocations",
    labelnames=["tool_name", "status"],
    registry=REGISTRY,
)

# End-to-end tool call duration in seconds, labelled by tool name.
TOOL_DURATION_SECONDS = Histogram(
    "agent_trust_tool_duration_seconds",
    "MCP tool call duration in seconds",
    labelnames=["tool_name"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# Tool errors broken down by error class name for faster triage.
TOOL_ERRORS_TOTAL = Counter(
    "agent_trust_tool_errors_total",
    "Total number of MCP tool errors by exception type",
    labelnames=["tool_name", "error_type"],
    registry=REGISTRY,
)

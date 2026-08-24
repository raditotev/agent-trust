from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

# Total MCP tool invocations, labelled by tool name, outcome status, and
# whether the call carried credentials (access_token/public_key_hex) — not
# who called, just anonymous-vs-authenticated traffic volume.
TOOL_CALLS_TOTAL = Counter(
    "agent_trust_tool_calls_total",
    "Total number of MCP tool invocations",
    labelnames=["tool_name", "status", "authenticated"],
    registry=REGISTRY,
)

# End-to-end tool call duration in seconds, labelled by tool name and status —
# status is included so a spike of fast auth failures can't be mistaken for
# the real workload getting faster.
TOOL_DURATION_SECONDS = Histogram(
    "agent_trust_tool_duration_seconds",
    "MCP tool call duration in seconds",
    labelnames=["tool_name", "status"],
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

# Calls currently in flight per tool — separates "slow work" from "queued
# behind something else" in a way a duration histogram alone can't.
TOOL_IN_FLIGHT = Gauge(
    "agent_trust_tool_in_flight",
    "MCP tool calls currently in progress",
    labelnames=["tool_name"],
    registry=REGISTRY,
)

# Protocol-level JSON-RPC requests (tools/list, initialize is handled by the
# session and isn't visible here, resources/*, prompts/*, tools/call). This is
# what actually answers "how much traffic does this server take" — tool_calls_
# total only counts tools/call, and clients hit tools/list on every connect.
MCP_REQUESTS_TOTAL = Counter(
    "agent_trust_mcp_requests_total",
    "MCP JSON-RPC requests by method and outcome",
    labelnames=["method", "status"],
    registry=REGISTRY,
)

# HTTP-level, from the ASGI app (streamable-http transport only). Distinct
# from MCP_REQUESTS_TOTAL because it also sees /health, /metrics, and
# transport-level failures that never make it to a JSON-RPC handler.
HTTP_REQUESTS_TOTAL = Counter(
    "agent_trust_http_requests_total",
    "HTTP requests by method, route and status code",
    labelnames=["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_IN_FLIGHT = Gauge(
    "agent_trust_http_in_flight",
    "HTTP requests currently being handled",
    registry=REGISTRY,
)

# Background arq jobs (recompute_score, dispatch_alerts, cron jobs) — a
# separate family from TOOL_* because these aren't MCP tool calls, they run
# in the `worker` container against a different Prometheus registry/port
# (see scripts/run_worker.py), scraped as job="agent-trust-worker".
JOB_RUNS_TOTAL = Counter(
    "agent_trust_job_runs_total",
    "Total number of background job runs",
    labelnames=["job_name", "status"],
    registry=REGISTRY,
)

JOB_DURATION_SECONDS = Histogram(
    "agent_trust_job_duration_seconds",
    "Background job duration in seconds",
    labelnames=["job_name", "status"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)

JOB_ERRORS_TOTAL = Counter(
    "agent_trust_job_errors_total",
    "Total number of background job errors by exception type",
    labelnames=["job_name", "error_type"],
    registry=REGISTRY,
)

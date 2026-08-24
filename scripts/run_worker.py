#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import arq
from prometheus_client import start_http_server


def main() -> None:
    """Run the arq background worker."""
    from agent_trust.config import settings
    from agent_trust.logging_config import configure_logging
    from agent_trust.workers import WorkerSettings

    configure_logging(json_logs=settings.json_logs, log_level=settings.log_level)

    if settings.metrics_enabled:
        # Separate process from the app, so a separate port — scraped as
        # job="agent-trust-worker" (see ~/Projects/observability/prometheus).
        start_http_server(9100)

    arq.run_worker(WorkerSettings)


if __name__ == "__main__":
    main()

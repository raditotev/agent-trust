from __future__ import annotations

import arq
import arq.connections

from agent_trust.config import settings
from agent_trust.workers.alert_dispatcher import dispatch_alerts
from agent_trust.workers.attestation_expiry import expire_attestations as expire_attestations
from agent_trust.workers.decay_refresh import refresh_all_scores as refresh_all_scores
from agent_trust.workers.score_recomputer import recompute_score


class WorkerSettings:
    """arq worker configuration."""

    functions = [
        arq.func(recompute_score, name="recompute_score", max_tries=3, timeout=120),
        arq.func(dispatch_alerts, name="dispatch_alerts", max_tries=3, timeout=60),
    ]
    cron_jobs = [
        arq.cron(refresh_all_scores, hour=2, minute=0),
        arq.cron(expire_attestations, minute=0),
    ]

    redis_settings = arq.connections.RedisSettings.from_dsn(settings.redis_url)

    # Retry backoff: 10s, 30s, 90s
    retry_jobs = True
    max_jobs = 20
    job_timeout = 120

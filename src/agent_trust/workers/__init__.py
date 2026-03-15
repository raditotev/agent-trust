from __future__ import annotations

from agent_trust.workers.alert_dispatcher import dispatch_alerts
from agent_trust.workers.attestation_expiry import expire_attestations as expire_attestations
from agent_trust.workers.decay_refresh import refresh_all_scores as refresh_all_scores
from agent_trust.workers.score_recomputer import recompute_score


class WorkerSettings:
    """arq worker configuration."""

    functions = [recompute_score, dispatch_alerts]
    cron_jobs = [
        # Run decay refresh nightly at 2am UTC
        # arq.cron(refresh_all_scores, hour=2, minute=0),
        # Run attestation expiry every hour
        # arq.cron(expire_attestations, minute=0),
    ]

    @staticmethod
    def get_redis_settings():
        import arq.connections

        from agent_trust.config import settings

        return arq.connections.RedisSettings.from_dsn(settings.redis_url)

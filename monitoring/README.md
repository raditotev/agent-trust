# monitoring/

This directory holds one thing: the Grafana dashboard for AgentTrust.

There is no Prometheus config and no Grafana provisioning here any more. Both are
owned by the shared observability stack in `~/Projects/observability`, which runs
a single Grafana (https://logs.radi.pro), Prometheus and Loki for every service
on this machine.

- **Logs** are collected with no configuration at all — Alloy discovers every
  container on the Docker socket.
- **Metrics** are scraped from `agent-trust:8000/metrics`. The target is declared
  in `~/Projects/observability/prometheus/prometheus.yml`, and the app joins that
  stack's `observability` Docker network in `docker-compose.prod.yml` so the name
  resolves.
- **This dashboard** is bind-mounted read-only into that Grafana and appears in
  the `agent-trust` folder. It stays versioned here, in the repo that owns it.

Every panel and target pins `datasource: {type: prometheus, uid: prometheus}`.
Do not remove those: Loki is the default datasource in the shared Grafana, so a
panel that leaves `datasource` unset silently queries Loki with PromQL.

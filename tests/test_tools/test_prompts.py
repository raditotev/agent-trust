from __future__ import annotations

from agent_trust.prompts.diagnose import dispute_assessment, explain_score_change
from agent_trust.prompts.evaluate import evaluate_counterparty


def test_evaluate_counterparty_contains_agent_id():
    result = evaluate_counterparty("agent-123", "1000 USD", "transaction")
    assert "agent-123" in result
    assert "PROCEED" in result or "CAUTION" in result or "DECLINE" in result
    assert "check_trust" in result


def test_evaluate_counterparty_defaults():
    result = evaluate_counterparty("agent-xyz")
    assert "agent-xyz" in result
    assert "general" in result
    assert "unknown" in result


def test_explain_score_change_contains_agent_id():
    result = explain_score_change("agent-456")
    assert "agent-456" in result
    assert "check_trust" in result
    assert "get_score_breakdown" in result


def test_dispute_assessment_contains_dispute_id():
    result = dispute_assessment("dispute-789")
    assert "dispute-789" in result
    assert "upheld" in result
    assert "dismissed" in result
    assert "resolve_dispute" in result


def test_evaluate_counterparty_transaction_type():
    result = evaluate_counterparty("agent-abc", transaction_type="delegation")
    assert "delegation" in result

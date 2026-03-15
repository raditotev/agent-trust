from __future__ import annotations


def evaluate_counterparty(
    agent_id: str,
    transaction_value: str = "unknown",
    transaction_type: str = "general",
) -> str:
    """Structured evaluation of a potential counterparty agent.

    Guides an LLM through a systematic trust assessment before
    entering a transaction. Returns a PROCEED/CAUTION/DECLINE verdict.
    """
    return f"""Evaluate agent {agent_id} as a potential counterparty for \
a {transaction_type} transaction (value: {transaction_value}).

Steps:
1. Call check_trust for agent {agent_id} with score_type "overall"
2. If score < 0.3 → DECLINE immediately. If confidence < 0.2 → flag as UNVERIFIED.
3. Call get_score_breakdown for agent {agent_id} to understand score composition.
4. Call get_interaction_history for agent {agent_id} with since_days=30 to review recent activity.
5. Call check_trust with score_type "reliability" for transaction-specific assessment.
6. Check if any open disputes exist by reviewing recent interactions.
7. Synthesize findings into a verdict: PROCEED / CAUTION / DECLINE

Decision framework:
- PROCEED: score ≥ 0.7 AND confidence ≥ 0.5 AND no recent failures
- CAUTION: score 0.4–0.7 OR confidence 0.2–0.5 OR some failures present
- DECLINE: score < 0.4 OR confidence < 0.1 (unknown) OR multiple recent failures

Weight confidence heavily — a 0.8 score with 0.1 confidence is riskier
than a 0.6 score with 0.9 confidence. Unknown agents (low confidence)
should be treated as high-risk regardless of score value.

Format your response as:
VERDICT: [PROCEED/CAUTION/DECLINE]
SCORE: [overall score] (confidence: [confidence])
REASONING: [2-3 sentences]
RISK FACTORS: [bullet list of concerns, or "None identified"]
"""

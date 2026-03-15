from __future__ import annotations


def explain_score_change(agent_id: str) -> str:
    """Diagnostic prompt for investigating a trust score change.

    Guides an LLM through systematically identifying what caused
    a change in an agent's trust score.
    """
    return f"""Investigate the trust score change for agent {agent_id}.

Steps:
1. Call check_trust for agent {agent_id} to get current score and confidence.
2. Call get_score_breakdown for agent {agent_id} for detailed factor attribution.
3. Call get_interaction_history for agent {agent_id} with since_days=7 to see recent activity.
4. Compare the factor_breakdown fields: bayesian_raw, dispute_penalty, interactions_weighted.
5. Check for recently resolved disputes (look for interaction patterns suggesting disputes).
6. Identify the primary driver of the change from these categories:
   - New interactions (positive or negative outcomes)
   - Dispute resolution (upheld or dismissed)
   - Time decay (old positive interactions expiring)
   - Credibility reweighting (reporters' own scores changed)

Format your response as:
CURRENT SCORE: [score] (confidence: [confidence])
CHANGE DRIVER: [primary cause]
DETAIL: [2-3 sentences explaining the change]
ACTION RECOMMENDED: [what the agent should do, if anything]
"""


def dispute_assessment(dispute_id: str) -> str:
    """Structured assessment prompt for evaluating a dispute as an arbitrator.

    Guides an LLM arbitrator through evaluating a dispute fairly.
    """
    return f"""Assess dispute {dispute_id} for resolution.

Steps:
1. Read the dispute details from trust://disputes/{dispute_id}
2. Call get_interaction_history for the agent filed against to review their track record.
3. Call get_interaction_history for the agent who filed the dispute to assess their history.
4. Call check_trust for both parties to understand their current standing.
5. Review the evidence provided in the dispute record.
6. Consider:
   - Is the disputed outcome consistent with the agent's general pattern?
   - Does the filer have a history of filing disputes?
   - Is there corroborating evidence from other parties?

Resolution options:
- upheld: The dispute is valid. The agent filed against was in the wrong.
          This applies a 0.03 penalty to their trust score.
- dismissed: The dispute is frivolous or unsubstantiated.
             A small 0.01 penalty applies to the filer.
- split: Both parties share responsibility.

Format your response as:
RECOMMENDED RESOLUTION: [upheld/dismissed/split]
CONFIDENCE: [high/medium/low]
REASONING: [3-4 sentences]
EVIDENCE CONSIDERED: [what evidence was available and how it was weighted]

After forming your assessment, call resolve_dispute with dispute_id="{dispute_id}"
and your chosen resolution.
"""

🔴 CRITICAL

1.  Sybil Attack (Score Manipulation) — register_agent() has no rate limit. An attacker creates 100+
    free agents, coordinates fake success/failure interactions to inflate or crush any target's score.
    Sybil detection only catches ring/burst patterns.
2.  Identity Hijacking — link_agentauth() doesn't require proof of ownership of the standalone agent's
    private key. An attacker can steal any agent's reputation by linking it to their AgentAuth account.
3.  Attestation Forgery — The signing key is stored unencrypted on disk (NoEncryption()). If the
    server is compromised, attestations can be forged for any agent with any score. No kid header means no
    rotation detection.
4.  Dispute Weaponization — 10 sybil agents filing 4 disputes each = 40 disputes × penalty =
    significant score damage, even when all disputes are dismissed.

🟠 HIGH

1.  Token revocation bypass — 300s cache means revoked tokens work for 5 more minutes
2.  Unauthenticated score enumeration — check_trust() is public, enables network mapping
3.  Arbitrator collusion — single arbitrator can falsely resolve disputes, no multi-sig
4.  Low-credibility reporter spam — new agents still get 30% credibility, 100 × that adds up

🟡 MEDIUM

1.  No TLS on HTTP transport (plaintext tokens)
2.  Unbounded dispute reason field (DoS via storage)
3.  Weak default credentials (postgres/redis)
4.  No key rotation mechanism
5.  Attestation replay (no nonce/recipient binding)

Top 3 fixes: Rate-limit agent registration, require ownership proof for link_agentauth(), encrypt the
signing key. Full report saved to session files.

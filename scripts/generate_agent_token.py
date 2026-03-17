#!/usr/bin/env python
"""Generate a short-lived signed access token for a standalone agent.

Usage:
    uv run python scripts/generate_agent_token.py \\
        --agent-id  <uuid>  \\
        --private-key-hex  <64-char hex>  \\
        [--ttl-minutes 60]

The output token can be pasted directly into the access_token field of any
AgentTrust MCP tool (e.g. report_interaction, file_dispute, issue_attestation).

The agent must have been registered with its corresponding public_key_hex.
Tokens expire after --ttl-minutes (default 60). Generate a fresh one when
the old one expires.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_trust.crypto.agent_token import sign_agent_token


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a signed access token for a standalone agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent UUID (returned by register_agent)",
    )
    parser.add_argument(
        "--private-key-hex",
        required=True,
        help="32-byte Ed25519 private key as 64 hex chars (from register_agent response)",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=60,
        help="Token lifetime in minutes (default: 60)",
    )
    args = parser.parse_args()

    if len(args.private_key_hex) != 64:
        print(
            f"Error: private_key_hex must be 64 hex characters (got {len(args.private_key_hex)})",
            file=sys.stderr,
        )
        sys.exit(1)

    token = sign_agent_token(
        agent_id=args.agent_id,
        private_key_hex=args.private_key_hex,
        ttl_minutes=args.ttl_minutes,
    )

    print(token)


if __name__ == "__main__":
    main()

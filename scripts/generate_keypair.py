#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_trust.config import settings
from agent_trust.crypto.keys import (
    generate_ed25519_keypair,
    get_public_key_hex,
    save_private_key,
    save_public_key,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Ed25519 keypair for AgentTrust")
    parser.add_argument(
        "--password",
        default=None,
        help="Password to encrypt the private key (omit for unencrypted)",
    )
    args = parser.parse_args()

    key_path = Path(settings.signing_key_path)
    pub_path = key_path.with_suffix(".pub")

    if key_path.exists():
        print(f"Key already exists at {key_path}")
        print("Delete it first if you want to regenerate.")
        return

    print("Generating Ed25519 keypair...")
    private_key, public_key = generate_ed25519_keypair()

    password: str | None = args.password
    save_private_key(private_key, key_path, password=password)
    save_public_key(public_key, pub_path)

    pub_hex = get_public_key_hex(public_key)
    print(f"Private key: {key_path}")
    print(f"Public key:  {pub_path}")
    print(f"Public key hex: {pub_hex}")
    if password:
        print("Private key is ENCRYPTED with the provided password.")
    else:
        print("Private key is UNENCRYPTED. Consider using --password for production.")
    print("Done. Keep the private key secure!")


if __name__ == "__main__":
    main()

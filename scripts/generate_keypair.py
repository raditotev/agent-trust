#!/usr/bin/env python
from __future__ import annotations

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
    key_path = Path(settings.signing_key_path)
    pub_path = key_path.with_suffix(".pub")

    if key_path.exists():
        print(f"Key already exists at {key_path}")
        print("Delete it first if you want to regenerate.")
        return

    print("Generating Ed25519 keypair...")
    private_key, public_key = generate_ed25519_keypair()

    save_private_key(private_key, key_path)
    save_public_key(public_key, pub_path)

    pub_hex = get_public_key_hex(public_key)
    print(f"Private key: {key_path}")
    print(f"Public key:  {pub_path}")
    print(f"Public key hex: {pub_hex}")
    print("Done. Keep the private key secure!")


if __name__ == "__main__":
    main()

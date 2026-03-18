from __future__ import annotations

import os
from pathlib import Path

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

log = structlog.get_logger()


def generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a new Ed25519 keypair for the service."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def save_private_key(
    private_key: Ed25519PrivateKey,
    path: str | Path,
    password: str | None = None,
) -> None:
    """Save Ed25519 private key to PEM file, optionally encrypted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    path.write_bytes(pem)
    os.chmod(path, 0o600)
    log.info("private_key_saved", path=str(path), encrypted=bool(password))


def save_public_key(public_key: Ed25519PublicKey, path: str | Path) -> None:
    """Save Ed25519 public key to PEM file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(pem)
    log.info("public_key_saved", path=str(path))


def load_private_key(
    path: str | Path,
    password: str | None = None,
) -> Ed25519PrivateKey:
    """Load Ed25519 private key from PEM file, optionally decrypting."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Signing key not found at {path}. Run: uv run python scripts/generate_keypair.py"
        )
    pem = path.read_bytes()
    key = serialization.load_pem_private_key(
        pem,
        password=password.encode() if password else None,
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"Expected Ed25519 private key, got {type(key)}")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    """Load Ed25519 public key from PEM file."""
    path = Path(path)
    pem = path.read_bytes()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"Expected Ed25519 public key, got {type(key)}")
    return key


def get_service_private_key() -> Ed25519PrivateKey:
    """Load the service signing key from the configured path."""
    from agent_trust.config import settings

    return load_private_key(
        settings.signing_key_path,
        password=settings.signing_key_password or None,
    )


def get_public_key_hex(public_key: Ed25519PublicKey) -> str:
    """Get the raw hex representation of an Ed25519 public key."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()

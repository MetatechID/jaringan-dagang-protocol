"""Beckn protocol Ed25519 signing and verification utilities.

Implements the Beckn authorization header signing scheme using Ed25519 (via PyNaCl).

Signature format:
    Signature keyId="{subscriber_id}|{unique_key_id}|ed25519",
              algorithm="ed25519",
              created="{created_unix}",
              expires="{expires_unix}",
              headers="(created) (expires) digest",
              signature="{base64_signature}"

The digest is a BLAKE-512 hash of the request body, base64-encoded.
The signing string is:
    (created): {created_unix}
    (expires): {expires_unix}
    digest: BLAKE-512={base64_digest}
"""

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from nacl.encoding import Base64Encoder
from nacl.signing import SigningKey, VerifyKey


@dataclass
class KeyPair:
    """An Ed25519 key pair for Beckn signing."""

    signing_key: SigningKey
    verify_key: VerifyKey

    @property
    def private_key_base64(self) -> str:
        """Return the private (signing) key as base64."""
        return base64.b64encode(bytes(self.signing_key)).decode()

    @property
    def public_key_base64(self) -> str:
        """Return the public (verify) key as base64."""
        return base64.b64encode(bytes(self.verify_key)).decode()


def generate_keypair() -> KeyPair:
    """Generate a new Ed25519 key pair for Beckn protocol signing.

    Returns:
        KeyPair with signing_key and verify_key.
    """
    signing_key = SigningKey.generate()
    return KeyPair(
        signing_key=signing_key,
        verify_key=signing_key.verify_key,
    )


def _blake512_digest(body: bytes) -> str:
    """Compute BLAKE2b-512 digest of the body and return base64-encoded string.

    The Beckn spec uses BLAKE-512 for the digest computation.
    """
    hasher = hashlib.blake2b(body, digest_size=64)
    digest_bytes = hasher.digest()
    return base64.b64encode(digest_bytes).decode()


def _build_signing_string(
    created: int,
    expires: int,
    digest: str,
) -> str:
    """Build the signing string per Beckn spec.

    Format:
        (created): {created}
        (expires): {expires}
        digest: BLAKE-512={digest}
    """
    return (
        f"(created): {created}\n"
        f"(expires): {expires}\n"
        f"digest: BLAKE-512={digest}"
    )


def _build_auth_header(
    subscriber_id: str,
    unique_key_id: str,
    created: int,
    expires: int,
    signature_b64: str,
) -> str:
    """Build the Beckn Authorization header value."""
    return (
        f'Signature keyId="{subscriber_id}|{unique_key_id}|ed25519",'
        f'algorithm="ed25519",'
        f'created="{created}",'
        f'expires="{expires}",'
        f'headers="(created) (expires) digest",'
        f'signature="{signature_b64}"'
    )


def _parse_auth_header(auth_header: str) -> dict[str, str]:
    """Parse a Beckn Signature authorization header into its components.

    Returns a dict with keys: keyId, algorithm, created, expires, headers, signature.
    """
    # Remove the 'Signature ' prefix if present
    header = auth_header
    if header.startswith("Signature "):
        header = header[len("Signature "):]

    params: dict[str, str] = {}
    # Split on ',' but respect quoted values
    current_key = ""
    current_val = ""
    in_quotes = False
    i = 0
    while i < len(header):
        ch = header[i]
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if ch == "=" and not in_quotes and not current_key:
            current_key = current_val.strip()
            current_val = ""
            i += 1
            continue
        if ch == "," and not in_quotes:
            if current_key:
                params[current_key] = current_val.strip()
            current_key = ""
            current_val = ""
            i += 1
            continue
        current_val += ch
        i += 1

    # Capture last pair
    if current_key:
        params[current_key] = current_val.strip()

    return params


class BecknSigner:
    """Ed25519 signer for Beckn protocol requests.

    Usage:
        signer = BecknSigner(
            signing_key=signing_key,
            subscriber_id="my-bap.example.com",
            unique_key_id="key-1",
        )
        auth_header = signer.sign(request_body)

    The resulting auth_header is placed in the HTTP Authorization header.
    """

    def __init__(
        self,
        signing_key: SigningKey,
        subscriber_id: str,
        unique_key_id: str,
        ttl_seconds: int = 300,
    ) -> None:
        """Initialize the signer.

        Args:
            signing_key: Ed25519 private signing key (from PyNaCl).
            subscriber_id: Beckn subscriber ID of this node (BAP or BPP).
            unique_key_id: Unique key identifier registered with the registry.
            ttl_seconds: How long the signature is valid (default 300s / 5 min).
        """
        self.signing_key = signing_key
        self.subscriber_id = subscriber_id
        self.unique_key_id = unique_key_id
        self.ttl_seconds = ttl_seconds

    def sign(self, body: bytes, created: Optional[int] = None) -> str:
        """Sign a request body and return the Authorization header value.

        Args:
            body: The raw request body bytes (JSON-encoded Beckn request).
            created: Unix timestamp for signature creation. Defaults to now.

        Returns:
            The full Signature header value to be placed in the Authorization header.
        """
        if created is None:
            created = int(time.time())
        expires = created + self.ttl_seconds

        # Compute digest
        digest = _blake512_digest(body)

        # Build signing string
        signing_string = _build_signing_string(created, expires, digest)

        # Sign with Ed25519
        signed = self.signing_key.sign(
            signing_string.encode(),
            encoder=Base64Encoder,
        )
        # signed.signature is the detached signature in base64
        signature_b64 = signed.signature.decode()

        return _build_auth_header(
            subscriber_id=self.subscriber_id,
            unique_key_id=self.unique_key_id,
            created=created,
            expires=expires,
            signature_b64=signature_b64,
        )


def sign_request(
    body: bytes,
    signing_key: SigningKey,
    subscriber_id: str,
    unique_key_id: str,
    ttl_seconds: int = 300,
    created: Optional[int] = None,
) -> str:
    """Convenience function to sign a request body.

    Args:
        body: Raw request body bytes.
        signing_key: Ed25519 signing key.
        subscriber_id: Beckn subscriber ID.
        unique_key_id: Key ID registered with the registry.
        ttl_seconds: Signature validity duration.
        created: Unix timestamp (defaults to now).

    Returns:
        The Signature authorization header value.
    """
    signer = BecknSigner(
        signing_key=signing_key,
        subscriber_id=subscriber_id,
        unique_key_id=unique_key_id,
        ttl_seconds=ttl_seconds,
    )
    return signer.sign(body, created=created)


def verify_request(
    body: bytes,
    auth_header: str,
    public_key_base64: str,
) -> bool:
    """Verify a Beckn request signature.

    In production, the public key would be looked up from the Beckn registry
    using the keyId from the auth header. This function takes the public key
    directly for flexibility.

    Args:
        body: Raw request body bytes.
        auth_header: The Authorization header value (Signature ...).
        public_key_base64: Base64-encoded Ed25519 public key.

    Returns:
        True if the signature is valid and not expired, False otherwise.
    """
    try:
        params = _parse_auth_header(auth_header)

        created = int(params["created"])
        expires = int(params["expires"])
        signature_b64 = params["signature"]

        # Check expiry
        now = int(time.time())
        if now > expires:
            return False

        # Compute expected digest
        digest = _blake512_digest(body)

        # Build the expected signing string
        signing_string = _build_signing_string(created, expires, digest)

        # Decode public key and signature
        public_key_bytes = base64.b64decode(public_key_base64)
        verify_key = VerifyKey(public_key_bytes)

        signature_bytes = base64.b64decode(signature_b64)

        # Verify -- raises BadSignatureError on failure
        verify_key.verify(signing_string.encode(), signature_bytes)
        return True

    except Exception:
        return False

# @jaringan-dagang/beckn-protocol

## 0.1.0

### Patch Changes

- 8c9705d: Clarify Dipay SNAP v2.1 conformance and remove speculative signing code.

  Verified against Midtrans' known-conformant SNAP-BI reference client
  (`midtrans-php/SnapBi/SnapBi.php`) that our scheme is byte-identical:
  access token = base64 SHA256withRSA over `"{client_key}|{x_timestamp}"`;
  business calls = base64 HMAC-SHA512 over the **5-leg, path-form**
  `"{METHOD}:{url_path}:{accessToken}:{sha256hex(minifiedBody)}:{x_timestamp}"`.

  - **Corrected the module docstring** in `services/dipay_client.py`, which
    described a hex-over-full-URL scheme that the code has never used.
  - **Removed dead probe helpers** (`_rsa_sha256_hex`, `_hmac_sha512_hex`, and
    two unused aliases) left over from spec-vs-gateway bisection.
  - **Simplified the inbound callback verifier**: `verify_notification_signature`
    now tries the canonical re-minified body first and the raw wire bytes second
    (logging which matched) instead of a 12-candidate shape spray; the webhook
    router calls it directly rather than introspecting it via `inspect`.
  - **Removed the dead `customer_reference` parameter** from
    `create_disbursement` — Dipay rejects `additionalInfo.customerReference` with
    a bogus `4014300`, so the parameter only advertised a capability we never use.

  No wire-format behaviour changed: `X-EXTERNAL-ID` remains header-only (not part
  of `stringToSign`), and the URL leg remains the path.

- a7e7275: Harden Dipay payment and disbursement lifecycle:
  - **Credential isolation & lifecycle:** Ensure strict separation of per-brand credentials and environment fallback settings, secure write-only handling, and robust token caching.
  - **Signature conformance & timestamps:** Align with SNAP BI specifications by enforcing second-precision ISO-8601 timestamps (`timespec="seconds"`, length 25), symmetric HMAC-SHA512 request signing, and asymmetric RSA-SHA256 inbound callback signature verification via `DIPAY_CALLBACK_PUBLIC_KEY`.
  - **Bank code expansion:** Add complete official Dipay bank code table (all 119 supported banks from official documentation, including digital banks BCA 014, Mandiri 008, BNI 009, BRI 002, Permata 013, CIMB Niaga 022, BSI 451, BCA Digital 501, Bank Jago 542, SeaBank 535, Superbank 562, Allo Bank 567, Krom 459, Neo Commerce 490, etc.) across seller dashboard and storefront admin forms with popular banks listed at the top.
  - **SNAP callback envelopes & lifecycle:** Implement standard SNAP acknowledgment response codes (`2004800` for QRIS payment notifications, `2003600` for disbursement status callbacks), document pre-funding requirements for deposit-based disbursements, and clarify manual refund handling.

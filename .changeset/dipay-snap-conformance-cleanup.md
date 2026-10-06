---
"@jaringan-dagang/beckn-protocol": patch
---

Clarify Dipay SNAP v2.1 conformance and remove speculative signing code.

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

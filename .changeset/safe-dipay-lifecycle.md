---
"@jaringan-dagang/beckn-protocol": patch
---

Harden Dipay payment and disbursement lifecycle:
- **Credential isolation & lifecycle:** Ensure strict separation of per-brand credentials and environment fallback settings, secure write-only handling, and robust token caching.
- **Signature conformance & timestamps:** Align with SNAP BI specifications by enforcing second-precision ISO-8601 timestamps (`timespec="seconds"`, length 25), symmetric HMAC-SHA512 request signing, and asymmetric RSA-SHA256 inbound callback signature verification via `DIPAY_CALLBACK_PUBLIC_KEY`.
- **Bank code expansion:** Add complete official Dipay bank code table (all 119 supported banks from official documentation, including digital banks BCA 014, Mandiri 008, BNI 009, BRI 002, Permata 013, CIMB Niaga 022, BSI 451, BCA Digital 501, Bank Jago 542, SeaBank 535, Superbank 562, Allo Bank 567, Krom 459, Neo Commerce 490, etc.) across seller dashboard and storefront admin forms with popular banks listed at the top.
- **SNAP callback envelopes & lifecycle:** Implement standard SNAP acknowledgment response codes (`2004800` for QRIS payment notifications, `2003600` for disbursement status callbacks), document pre-funding requirements for deposit-based disbursements, and clarify manual refund handling.

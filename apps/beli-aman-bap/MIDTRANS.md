# Midtrans Payment Link

Midtrans is an optional hosted payment collection provider alongside Xendit, OY, Sento and Dipay. Existing brand defaults do not change. No Midtrans payout or refund automation is included; automatic release explicitly refuses Midtrans instead of falling back to Xendit. Internal HOLD bookkeeping does not establish a regulated escrow product or a settlement guarantee.

## Setup (operator-controlled)

1. Review `scripts/add-midtrans-columns.py` dry-run output. Apply reviewed migration explicitly to intended database only after backup/change approval. This version adds two payment tables, not per-brand credentials; existing deployments need those tables before using Midtrans. No startup migration is installed.
2. Operator configures one server-only platform account: `MIDTRANS_SERVER_KEY`, `MIDTRANS_ENVIRONMENT` (`sandbox` or `production`), and optional `MIDTRANS_MERCHANT_ID` for extra verification. No per-brand override or credential database columns exist. Never put Server Key in browser variables or image build args.
3. Seller selects brand provider `midtrans` through protected payment gateway settings; no merchant keys, environment or merchant ID are requested. Save fails with a sanitized error when global configuration is invalid. Store membership authorization and existing provider defaults remain unchanged.
4. In the matching Midtrans dashboard configure HTTPS payment notification URL `https://<BAP-host>/webhooks/midtrans`. Finish redirect is dashboard-wide; tenant-specific per-request redirects are not guaranteed. Redirect parameters never prove payment.
5. Authorize sandbox testing separately. Use dedicated test brand and authenticated checkout, not production recipients or credentials. Including buyer email in Midtrans API would trigger email; this integration intentionally omits customer details.

## Lifecycle

Both SDK `/api/v1/orders/{id}/invoice` and bot cart checkout use existing `invoice_url` response. SDKs identify provider as `midtrans` and open hosted checkout; QR-only fields stay empty. Cart `qr_image_url` remains legacy hosted-URL alias.

A unique durable base reference reserves each owner before provider I/O. Creation/retry uses same reference, `usage_limit: 1`, 60-minute expiry and integer IDR total. Timeout/conflict reconciles link details; unresolved creation returns retryable error, never automatically changes reference. Expired links require explicit reconciliation rather than replacement.

Midtrans appends timestamp to each actual payment-attempt order ID. Webhook resolves exact stored base, verifies SHA512 signature, then queries authenticated transaction status and checks reference, amount, currency and optional merchant ID. Pending/expired attempts do not automatically expire parent link. One accepted attempt creates existing paid/HOLD effects; another successful attempt is recorded `duplicate_paid` for manual review. Refund observations do not automatically reverse fulfillment/bookkeeping.

Outstanding link records block brand provider changes. Global credential/environment rotation is operator-owned and must reconcile outstanding links across all stores first; seller APIs cannot rotate platform credentials. Reconcile unresolved attempts before operator changes; no automatic sweeping/deletion job exists. Provider API 404 before payment-method selection is not evidence of failed payment. No new public reconciliation/status endpoint is added.

## Verification and limits

Offline:
```sh
python -m pytest -q tests/test_midtrans_client.py tests/test_midtrans_invoices.py tests/test_webhooks_midtrans.py tests/test_routes_midtrans_invoice.py tests/test_brands_payouts.py
python -m pytest -q tests/test_dipay_client.py tests/test_dipay_invoices.py tests/test_routes_dipay_invoice.py tests/test_webhooks_dipay.py
```

Focused tests cover signatures, configuration isolation, hosted URL validation, request payload, status checks, reservation ordering, uncertain-result recovery, callback-before-create-response, replay, duplicate payments, commit-failure effect suppression, hosted SDK response reuse, and write-only admin settings. DB boundaries are fake or SQLite; they do not prove PostgreSQL concurrency, migration compatibility, real notification delivery or authenticated end-to-end checkout. Those require a separately approved disposable DB/sandbox integration check. No live provider requests or production migration are part of normal tests.

New reservation and protected configuration updates serialize on the brand row; existing link locks precede checkout-owner locks, matching webhook ordering. Seller dispatch/attribution for this provider is scheduled only after successful payment commit. These tasks remain best-effort, not a durable outbox: a process crash after commit can lose dispatch. Link creation uses stable provider order-ID uniqueness, not a separate creation lease. Recovery may retry POST of that same reference after a lookup 404; it never chooses a new reference. No automated reconciliation tool is included; unresolved/expired records need operator review, not deletion as a shortcut.

## Coordinated source and local evidence

Changes are explicitly mirrored, not linked or published. Baseline revisions:
- Protocol BAP: `766aec0f8599598e4bbdc8dda56dc06453347b72`.
- Private consumer repository: `4f028cdf3e5d2e799febcfca74d8d21e8570857f` (admin settings and vendored SDK).
- Public SDK: `83d03dde3897a77fc7f5c4db45c1c9e8f1cdb104` (provider recognition and hosted category).

Local offline verification on 2026-11-09: 282 focused Midtrans/Dipay/OY/admin tests passed. Migration dry-run passed; DDL was not applied. Public SDK typecheck/build and 31 unit tests passed; seller-dashboard typecheck passed. Storefront typecheck failed on unresolved vendored React/Firebase modules and an untouched `StepDone.tsx` state comparison. Full BAP collection is blocked by legacy `python` imports. With two blocked collection modules excluded and async auto mode enabled, broader run had 1,510 passed, nine failed and 21 skipped; remaining failures concern legacy canonical-module paths and bot checkout fixtures/seller-poll expectations. Follow-up repair: full default BAP offline unit suite now passes 1,564 tests with zero failures/skips; one PostgreSQL integration test is explicitly deselected. Legacy canonical imports and enum fixture paths were fixed, async auto mode persisted, and bot tests now assert webhook-authoritative state rather than unsigned seller polling. PostgreSQL integration remains unexecuted: run `python -m pytest -m integration tests/test_webhook_audit_postgres.py` only with an explicitly provisioned disposable `AUDIT_TEST_DATABASE_URL`; that test drops/truncates its audit table. Seller payout payload tests: eight passed. Integrated Compose checks: four passed. No clean-baseline comparison or sandbox commerce evidence is claimed.

References: [Payment Link API](https://docs.midtrans.com/docs/payment-link-api-reference), [webhook authenticity](https://docs.midtrans.com/docs/https-notification-webhooks), [transaction status](https://docs.midtrans.com/docs/get-status-api-requests). Payment Link API is documented as beta.

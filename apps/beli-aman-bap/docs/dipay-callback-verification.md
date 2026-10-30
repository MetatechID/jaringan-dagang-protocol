# Dipay callback verification (JD-012)

Callbacks are advisory, even when their signature is valid. QRIS and remit
handlers must obtain an authoritative provider status before changing payment
or payout state. A provider error must never fall back to callback JSON.

- Provider not-found remains a distinct 404 outcome.
- Transient provider errors, timeouts, malformed results and mismatched
  references fail closed. QRIS retains its SNAP `5005201` / HTTP 500 envelope;
  remit returns HTTP 502. Both are retryable server failures, not success ACKs.
- Verification failures roll back the session and do not authorize paid/payout
  state, ledger writes or partner notifications. A later successful provider
  query may process the callback normally.
- Existing reference/amount/currency validation and signature checks remain.

Run the bounded offline suite with Python 3.12 and the BAP dependencies plus
canonical protocol wheel installed:

```sh
python apps/beli-aman-bap/scripts/test_dipay_webhooks.py
```

The runner clears inherited configuration before imports, changes to an empty
working directory to avoid real dotenv files, disables plugin autoload and
blocks outbound socket connections/DNS. Tests use fake sessions and local
signing keys, not a provider sandbox or a real database.

This change does not add durable replay protection, timestamp freshness,
terminal-event state handling or crash-safe notification delivery (JD-028 and
related work). It does not deploy code or verify a running provider integration.

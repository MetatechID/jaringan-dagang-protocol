# Partner browser returns (JD-031)

Configure `PARTNER_RETURN_ORIGINS` as a JSON object mapping persisted Brand IDs to exact origins, for example:

```text
PARTNER_RETURN_ORIGINS={"example-brand-id":["https://merchant.example"]}
```

There are no default allowed origins. Configure reviewed partner origins before accepting new orders with `success_url`. No wildcard, subdomain inheritance, or callback-URL-derived authorization is supported. Origin entries must not contain a non-root path, query or fragment. HTTPS is required; explicit localhost/loopback HTTP origins work only in development/test. Default ports normalize; other ports require an exact match. Origin changes are operator-managed configuration, not partner request fields.

New orders reject unsafe or unapproved returns with 422 before database/provider work. Existing stored destinations are revalidated at every browser use: invalid destinations are suppressed, not grandfathered. Public status only returns an approved destination for paid orders. A missing destination is valid and leaves the buyer on the hosted page.

Provider success navigation always points to the hosted `/pay/{id}/done` route. That route issues an HTTP 303 only after persisted status is paid and the destination still matches the order's partner policy. It never interpolates the return into an inline script. Hosted-page polling navigates to that same server route; browser session storage is no longer a return authority. Provider invoice/payment URLs are a separate trust boundary, unchanged here.

Run the offline regression suite with the BAP Python 3.12 virtualenv, requirements, pytest 8, pytest-asyncio 0.24 and the canonical protocol wheel installed:

```sh
env -i PATH=/usr/bin:/bin apps/beli-aman-bap/.venv/bin/python -I apps/beli-aman-bap/scripts/test_partner_returns_offline.py
```

The runner clears inherited configuration, disables dotenv/plugin autoload and rejects unexpected network calls. Tests use fake sessions and local HTTP route clients; they do not verify live provider navigation or real browser login/payment behavior. Configuration installation and deployment remain separate operator actions.

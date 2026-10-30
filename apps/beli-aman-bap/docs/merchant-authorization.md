# Merchant resource authorization

This change depends on the JD019 identity-binding change (PR32). Review and merge the identity dependency first; the merchant change is a dependent branch, not a replacement for identity verification.

## Merchant contracts

These existing paths now require `Authorization: Bearer <Firebase ID token>`:

- `GET /api/v1/orders/{id}/admin`
- `GET /api/v1/orders/{id}/label-data`
- `POST /api/v1/orders/{id}/ship`
- `GET /api/v1/partner/admin/orders`

Dashboard proxies must forward the caller's Authorization header, not substitute `X-Admin-Token`. A shared machine token alone no longer authorizes these routes. Firebase custom tokens must first be exchanged for ID tokens by the sign-in client.

Single-order routes load the persisted order, resolve its `brand_id` to `Brand.slug`, then check `StoreMembership.profile_id` and `store_slug`. Both owner and staff roles may operate orders, following the existing brand-edit ACL pattern. Pending invitations, email matches, buyer ownership, client-supplied brand slugs and store headers do not grant merchant access. Only a persisted `is_super_admin` flag bypasses membership. Missing identity or failed verification returns 401; nonmembers receive 403; missing orders return 404. Authorization precedes state validation, payout/label reads and carrier booking.

The partner collection applies an SQL EXISTS membership predicate before pagination, including when no brand slug is requested. A requested slug or status only narrows results. Unauthorized stores yield no collection rows. Duplicate ACL rows do not duplicate orders. Super-admins may list all stores, still subject to requested filters.

Authorization helpers neither commit nor roll back. Existing route/session transaction ownership and shipment locking remain unchanged. A denied request performs no order transition, event insertion or provider call; normal identity resolution can update the caller's sign-in metadata within the caller-owned transaction.

## Adjacent machine contracts and scope

The protocol repository audit found explicit admin-token contracts and regression tests for `POST /api/v1/orders/{id}/ship/cancel` and `POST /api/v1/orders/{id}/tracking/refresh`. Those internal operations retain `require_admin_token`; merchant bearer tokens alone do not authorize them. They are deliberately not converted or exposed as merchant routes in this change. Any future merchant-facing use needs an explicit caller migration and equivalent persisted-resource ACLs, not a bearer-to-machine-token authorization shortcut.

Partner API-key order registration/status, public partner status, buyer order routes, provider webhooks and other internal admin guards are unchanged. Dashboard callers live in a separately owned repository and must migrate the four merchant proxy contracts together with this change.

## Offline verification

From the repository root, using an environment with app requirements, pytest, pytest-asyncio and aiosqlite installed:

```sh
python -I apps/beli-aman-bap/scripts/test-identity-security.py
python -I apps/beli-aman-bap/scripts/test-identity-security.py \
  tests/test_merchant_authorization_security.py \
  tests/test_routes_label_data.py tests/test_partner_integration.py \
  tests/test_routes_cancel_shipment.py tests/test_routes_tracking_refresh.py
```

The runner strips inherited application environment variables, disables dotenv reads and pytest plugin autoload, and denies external network access before importing the app. Merchant regressions use SQLite with persisted profiles, orders, brands and memberships; Firebase verification and carrier booking are stubbed. The identity-security CI workflow includes the merchant suite by default. Tests cover missing/malformed/forged authentication, machine-token rejection, staff/owner/super-admin access, cross-tenant and pending-invite denial, persisted brand authority, no denied side effects, collection filtering and caller-owned rollback. SQLite tests do not validate PostgreSQL row-lock concurrency semantics.

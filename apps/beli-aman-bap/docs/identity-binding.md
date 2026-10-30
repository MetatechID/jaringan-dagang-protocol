# Identity binding (JD-019)

Firebase subject is the primary account identity. Verified email may link an OTP-only profile, but cannot resolve a profile already bound to a different subject. Such collisions return HTTP 409 with a generic account-recovery message; automatic rebinding is not supported. Existing subject sign-ins and verified-email OTP linking remain supported.

Pending email invitations require proof of that email in the current sign-in: a verified Firebase email claim or successful email OTP verification. An unverified email claim, a phone OTP, and a stored profile email alone do not claim email invitations. Already accepted memberships are unchanged. A verified changed email claims only invitations for the newly verified address.

Identity materialization and invitation updates do not commit. The caller owns commit/rollback. Existing-profile email linking locks the row on databases that support SELECT FOR UPDATE. The focused SQLite tests cover sequential behavior and rollback, not PostgreSQL concurrency guarantees.

Run with Python 3.12 and installed app/test dependencies:

```sh
python -I apps/beli-aman-bap/scripts/test-identity-security.py
```

The runner clears inherited application settings, disables dotenv, and denies network access. Tests use synthetic identities and in-memory SQLite; OTP route tests stub delivery and Firebase token minting. They do not exercise live Firebase, send messages, provision memberships, or deploy schema changes.

This is a focused prerequisite, not full JD-019 closure. Merchant resource authorization and caller-bearer dashboard forwarding remain separate follow-up work. No account recovery UI or automatic account merge is introduced.

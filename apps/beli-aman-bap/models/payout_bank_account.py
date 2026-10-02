"""PayoutBankAccount — a brand's bank account for escrow-release payouts.

Replaces the per-PG triplet columns on ``brands``
(``{xendit,sento,dipay}_disbursement_bank_{code,account,holder_name}``).
A brand can register several bank accounts; at most ONE is active at a
time (enforced by the partial unique index below plus service-level
deactivate-others-then-activate). Disbursement services pay out to the
active account, mapping the canonical bank code to each provider's
format via :mod:`services.bank_codes`.

``bank_code`` is the canonical BI numeric code (e.g. "014" = BCA) — the
format Sento and Dipay already share. Xendit's alphabetical codes are
derived at disbursement time.
"""

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayoutBankAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A bank account a brand receives payouts on. One active per brand."""

    __tablename__ = "payout_bank_accounts"
    __table_args__ = (
        # At most one active account per brand. ``sqlite_where`` mirrors the
        # partial index on sqlite so the test harness enforces the same
        # constraint as Postgres.
        Index(
            "uq_payout_bank_accounts_active",
            "brand_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
    )

    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id"), nullable=False, index=True
    )
    # Canonical BI numeric bank code ("014" = BCA) — Sento/Dipay native,
    # translated to Xendit's alphabetical codes at disbursement time.
    bank_code: Mapped[str] = mapped_column(String(16), nullable=False)
    account_number: Mapped[str] = mapped_column(String(64), nullable=False)
    # Record/UI parity only: Sento and Dipay transfer APIs take the account
    # number, not a recipient name (Xendit's disbursement payload does).
    holder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

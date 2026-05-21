"""Beckn protocol payment models.

Represents payment information in Beckn orders. Designed with Xendit integration
in mind for the Indonesian market.

# Xendit Mapping Notes:
# ---------------------
# Payment.type "PRE-FULFILLMENT" -> Xendit Invoice / VA / eWallet charge before delivery
# Payment.type "ON-FULFILLMENT"  -> Xendit charge triggered at delivery (COD via driver app)
# Payment.type "POST-FULFILLMENT"-> Xendit delayed capture / subscription billing
#
# PaymentParams.transaction_id   -> Maps to Xendit external_id / reference_id
# PaymentParams.amount           -> Maps to Xendit amount field
# PaymentParams.currency         -> Maps to Xendit currency (IDR for Indonesia)
# PaymentParams.bank_code        -> Maps to Xendit VA bank_code (BCA, BNI, BRI, MANDIRI, etc.)
# PaymentParams.virtual_payment_address -> Maps to Xendit VA number or eWallet account
#
# Xendit payment methods commonly used in Indonesia:
#   - Virtual Account (BCA, BNI, BRI, Mandiri, Permata, CIMB)
#   - eWallet (OVO, DANA, GoPay, ShopeePay, LinkAja)
#   - QR Code (QRIS)
#   - Retail Outlet (Alfamart, Indomaret)
#   - Credit/Debit Card
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .catalog import Tag


class PaymentType(str, Enum):
    """When payment is collected relative to fulfillment."""

    PRE_FULFILLMENT = "PRE-FULFILLMENT"
    ON_FULFILLMENT = "ON-FULFILLMENT"
    POST_FULFILLMENT = "POST-FULFILLMENT"


class PaymentStatus(str, Enum):
    """Payment status values."""

    PAID = "PAID"
    NOT_PAID = "NOT-PAID"
    PENDING = "PENDING"


class PaymentCollectedBy(str, Enum):
    """Who collects the payment."""

    BAP = "BAP"
    BPP = "BPP"


class PaymentParams(BaseModel):
    """Parameters for completing a payment transaction.

    These fields map to payment gateway (e.g. Xendit) request/response fields.
    """

    model_config = {"populate_by_name": True}

    transaction_id: Optional[str] = Field(
        default=None,
        description="Payment gateway transaction ID (Xendit: external_id)",
    )
    transaction_status: Optional[str] = Field(
        default=None,
        description="Payment transaction status from gateway",
    )
    amount: Optional[str] = Field(
        default=None,
        description="Payment amount as string (Xendit: amount)",
    )
    currency: Optional[str] = Field(
        default=None,
        description="ISO 4217 currency code (e.g. 'IDR')",
    )
    bank_code: Optional[str] = Field(
        default=None,
        description="Bank code for VA payments (Xendit: bank_code, e.g. 'BCA', 'BNI')",
    )
    bank_account_number: Optional[str] = Field(
        default=None,
        description="Virtual account number (Xendit: account_number)",
    )
    virtual_payment_address: Optional[str] = Field(
        default=None,
        description="Virtual payment address (VA number, eWallet account, QRIS string)",
    )
    source_bank_code: Optional[str] = Field(
        default=None,
        description="Source bank code for the payer",
    )
    source_bank_account_number: Optional[str] = Field(
        default=None,
        description="Source bank account number for the payer",
    )


class Payment(BaseModel):
    """Payment information for a Beckn order.

    Describes how and when the buyer pays for goods/services.
    """

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(default=None, description="Payment identifier")
    type: Optional[PaymentType] = Field(
        default=None,
        description="When payment is collected (pre/on/post fulfillment)",
    )
    collected_by: Optional[PaymentCollectedBy] = Field(
        default=None,
        description="Whether BAP or BPP collects the payment",
    )
    status: Optional[PaymentStatus] = Field(
        default=None,
        description="Current payment status",
    )
    params: Optional[PaymentParams] = Field(
        default=None,
        description="Payment gateway parameters",
    )
    uri: Optional[str] = Field(
        default=None,
        description="Payment gateway URL (e.g. Xendit invoice URL, QRIS deeplink)",
    )
    tl_method: Optional[str] = Field(
        default=None,
        description="HTTP method for payment link (e.g. 'http/get', 'http/post')",
    )
    buyer_app_finder_fee_type: Optional[str] = Field(
        default=None,
        description="Fee type for buyer app (e.g. 'percent', 'amount')",
    )
    buyer_app_finder_fee_amount: Optional[str] = Field(
        default=None,
        description="Buyer app finder fee amount as string",
    )
    settlement_basis: Optional[str] = Field(
        default=None,
        description="Basis for settlement (e.g. 'delivery', 'return_window_expiry')",
    )
    settlement_window: Optional[str] = Field(
        default=None,
        description="Settlement window in ISO 8601 duration (e.g. 'P2D')",
    )
    withholding_amount: Optional[str] = Field(
        default=None,
        description="Amount withheld until settlement",
    )
    settlement_details: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Settlement account details for each party",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional payment metadata tags",
    )

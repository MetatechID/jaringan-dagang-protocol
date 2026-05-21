"""Beckn protocol order models.

Represents orders at various stages of the Beckn transaction lifecycle:
select, init, confirm, status, cancel, update.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .catalog import Descriptor, Price, Tag
from .fulfillment import Fulfillment
from .payment import Payment


class OrderState(str, Enum):
    """Standard order states in the Beckn lifecycle."""

    CREATED = "Created"
    ACCEPTED = "Accepted"
    IN_PROGRESS = "In-progress"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class Billing(BaseModel):
    """Billing details for an order."""

    model_config = {"populate_by_name": True}

    name: str = Field(..., description="Name of the billed entity/person")
    address: Optional[str] = Field(
        default=None,
        description="Billing address as a formatted string",
    )
    email: Optional[str] = Field(default=None, description="Billing email")
    phone: Optional[str] = Field(default=None, description="Billing phone number")
    state: Optional[str] = Field(
        default=None,
        description="State or province",
    )
    city: Optional[str] = Field(
        default=None,
        description="City name",
    )
    area_code: Optional[str] = Field(
        default=None,
        description="Postal/ZIP code",
    )
    tax_id: Optional[str] = Field(
        default=None,
        description="Tax identification number (NPWP in Indonesia)",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="When billing info was created",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="When billing info was last updated",
    )


class QuoteBreakupItem(BaseModel):
    """Item reference within a quote breakup line."""

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(default=None, description="Item ID")
    quantity: Optional[dict[str, Any]] = Field(
        default=None,
        description="Quantity for this breakup item",
    )
    price: Optional[Price] = Field(
        default=None,
        description="Per-unit price for this item in the breakup",
    )


class QuoteBreakup(BaseModel):
    """A line item in the quote breakdown."""

    model_config = {"populate_by_name": True}

    title: Optional[str] = Field(
        default=None,
        description="Human-readable line item title (e.g. 'Base Price', 'Delivery Fee')",
    )
    price: Optional[Price] = Field(
        default=None,
        description="Price for this line item",
    )
    item: Optional[QuoteBreakupItem] = Field(
        default=None,
        description="Reference to the item this breakup line belongs to",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional metadata (e.g. tax type, discount type)",
    )


class Quote(BaseModel):
    """A price quote for an order."""

    model_config = {"populate_by_name": True}

    price: Optional[Price] = Field(
        default=None,
        description="Total quoted price",
    )
    breakup: Optional[list[QuoteBreakup]] = Field(
        default=None,
        description="Itemized price breakdown",
    )
    ttl: Optional[str] = Field(
        default=None,
        description="Quote validity in ISO 8601 duration (e.g. 'P1D')",
    )


class CancellationTerm(BaseModel):
    """Describes the terms for cancellation."""

    model_config = {"populate_by_name": True}

    fulfillment_state: Optional[Descriptor] = Field(
        default=None,
        description="Fulfillment state at which this term applies",
    )
    reason_required: Optional[bool] = Field(
        default=None,
        description="Whether a cancellation reason is required",
    )
    cancellation_fee: Optional[Price] = Field(
        default=None,
        description="Cancellation fee",
    )
    refund_eligible: Optional[bool] = Field(
        default=None,
        description="Whether the order is eligible for refund at this point",
    )


class OrderItem(BaseModel):
    """An item within an order, linking to catalog items with selected quantities."""

    model_config = {"populate_by_name": True}

    id: str = Field(..., description="Item ID (references catalog Item.id)")
    quantity: Optional[dict[str, Any]] = Field(
        default=None,
        description="Selected quantity (e.g. {'selected': {'count': 2}})",
    )
    fulfillment_ids: Optional[list[str]] = Field(
        default=None,
        description="Fulfillment IDs selected for this item",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional item-level metadata",
    )


class Order(BaseModel):
    """A Beckn order at any stage of the transaction lifecycle.

    Used in select, init, confirm, status, cancel, and update actions.
    The fields populated depend on the action stage.
    """

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(
        default=None,
        description="Order ID (assigned after confirm)",
    )
    state: Optional[str] = Field(
        default=None,
        description="Current order state (e.g. 'Created', 'Accepted', 'Completed')",
    )
    provider: Optional[dict[str, Any]] = Field(
        default=None,
        description="Provider reference (id and locations)",
    )
    items: Optional[list[OrderItem]] = Field(
        default=None,
        description="Ordered items with quantities",
    )
    billing: Optional[Billing] = Field(
        default=None,
        description="Billing details",
    )
    fulfillments: Optional[list[Fulfillment]] = Field(
        default=None,
        description="Fulfillment details (delivery info, tracking, etc.)",
    )
    quote: Optional[Quote] = Field(
        default=None,
        description="Price quote with breakup",
    )
    payment: Optional[Payment] = Field(
        default=None,
        description="Payment information (single payment, legacy)",
    )
    payments: Optional[list[Payment]] = Field(
        default=None,
        description="Payment information (multi-payment support)",
    )
    cancellation_terms: Optional[list[CancellationTerm]] = Field(
        default=None,
        description="Cancellation terms and fees",
    )
    cancellation: Optional[dict[str, Any]] = Field(
        default=None,
        description="Cancellation details if order was cancelled",
    )
    documents: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Associated documents (invoices, receipts, etc.)",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="When the order was created",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="When the order was last updated",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional order metadata tags",
    )

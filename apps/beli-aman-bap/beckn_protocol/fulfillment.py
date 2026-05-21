"""Beckn protocol fulfillment models.

Represents how an order is fulfilled -- delivery, pickup, or other logistics.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .catalog import Descriptor, Tag


class FulfillmentType(str, Enum):
    """Standard fulfillment types."""

    DELIVERY = "Delivery"
    SELF_PICKUP = "Self-Pickup"
    SELLER_DELIVERY = "Seller-Delivery"      # common in ONDC
    BUYER_DELIVERY = "Buyer-Delivery"


class Contact(BaseModel):
    """Contact information for a person or entity."""

    model_config = {"populate_by_name": True}

    phone: Optional[str] = Field(default=None, description="Phone number")
    email: Optional[str] = Field(default=None, description="Email address")


class Gps(BaseModel):
    """GPS coordinates."""

    model_config = {"populate_by_name": True}

    lat: str = Field(..., description="Latitude as string")
    lng: str = Field(..., description="Longitude as string")


class Address(BaseModel):
    """A physical address."""

    model_config = {"populate_by_name": True}

    door: Optional[str] = Field(
        default=None,
        description="House/door number",
    )
    name: Optional[str] = Field(
        default=None,
        description="Address name or label",
    )
    building: Optional[str] = Field(
        default=None,
        description="Building or apartment name",
    )
    street: Optional[str] = Field(
        default=None,
        description="Street name",
    )
    locality: Optional[str] = Field(
        default=None,
        description="Locality or neighborhood",
    )
    ward: Optional[str] = Field(
        default=None,
        description="Ward or sub-district",
    )
    city: Optional[str] = Field(
        default=None,
        description="City name",
    )
    state: Optional[str] = Field(
        default=None,
        description="State or province",
    )
    country: Optional[str] = Field(
        default=None,
        description="Country name or code",
    )
    area_code: Optional[str] = Field(
        default=None,
        description="Postal/ZIP code",
    )


class Location(BaseModel):
    """A geographic location with optional address and coordinates."""

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(default=None, description="Location identifier")
    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Location descriptor",
    )
    gps: Optional[str] = Field(
        default=None,
        description="GPS coordinates as 'lat,lng' string",
    )
    address: Optional[Address] = Field(default=None, description="Structured address")
    city: Optional[Any] = Field(
        default=None,
        description="City information",
    )
    country: Optional[Any] = Field(
        default=None,
        description="Country information",
    )
    area_code: Optional[str] = Field(
        default=None,
        description="Postal/ZIP code (shorthand)",
    )
    circle: Optional[dict[str, Any]] = Field(
        default=None,
        description="Circular geofence with gps center and radius",
    )
    polygon: Optional[str] = Field(
        default=None,
        description="Polygon geofence encoded as string",
    )
    time: Optional[dict[str, Any]] = Field(
        default=None,
        description="Operating hours / time constraints",
    )


class Person(BaseModel):
    """A person associated with a fulfillment (driver, agent, etc.)."""

    model_config = {"populate_by_name": True}

    name: Optional[str] = Field(default=None, description="Full name")
    gender: Optional[str] = Field(default=None, description="Gender")
    image: Optional[str] = Field(default=None, description="URL to photo")
    creds: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Credentials or verifiable claims",
    )
    tags: Optional[list[Tag]] = Field(default=None, description="Additional metadata")


class FulfillmentState(BaseModel):
    """Current state of a fulfillment."""

    model_config = {"populate_by_name": True}

    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="State descriptor (code and name)",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="When this state was set",
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="Who updated this state",
    )


class FulfillmentStop(BaseModel):
    """A stop point in a fulfillment (start, end, or intermediate)."""

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(default=None, description="Stop identifier")
    type: Optional[str] = Field(
        default=None,
        description="Stop type (e.g. 'start', 'end')",
    )
    location: Optional[Location] = Field(
        default=None,
        description="Location of this stop",
    )
    contact: Optional[Contact] = Field(
        default=None,
        description="Contact at this stop",
    )
    person: Optional[Person] = Field(
        default=None,
        description="Person at this stop",
    )
    time: Optional[dict[str, Any]] = Field(
        default=None,
        description="Time window for this stop",
    )
    instructions: Optional[Descriptor] = Field(
        default=None,
        description="Instructions for this stop",
    )
    authorization: Optional[dict[str, Any]] = Field(
        default=None,
        description="Authorization token/OTP for this stop",
    )


class FulfillmentStart(BaseModel):
    """Start point of a fulfillment (pickup location)."""

    model_config = {"populate_by_name": True}

    location: Optional[Location] = Field(
        default=None,
        description="Pickup location",
    )
    contact: Optional[Contact] = Field(
        default=None,
        description="Contact at pickup point",
    )
    person: Optional[Person] = Field(
        default=None,
        description="Person at pickup point",
    )
    time: Optional[dict[str, Any]] = Field(
        default=None,
        description="Pickup time window",
    )
    instructions: Optional[Descriptor] = Field(
        default=None,
        description="Pickup instructions",
    )
    authorization: Optional[dict[str, Any]] = Field(
        default=None,
        description="Pickup authorization",
    )


class FulfillmentEnd(BaseModel):
    """End point of a fulfillment (delivery location)."""

    model_config = {"populate_by_name": True}

    location: Optional[Location] = Field(
        default=None,
        description="Delivery location",
    )
    contact: Optional[Contact] = Field(
        default=None,
        description="Contact at delivery point",
    )
    person: Optional[Person] = Field(
        default=None,
        description="Person at delivery point",
    )
    time: Optional[dict[str, Any]] = Field(
        default=None,
        description="Delivery time window",
    )
    instructions: Optional[Descriptor] = Field(
        default=None,
        description="Delivery instructions",
    )
    authorization: Optional[dict[str, Any]] = Field(
        default=None,
        description="Delivery authorization (e.g. OTP)",
    )


class Fulfillment(BaseModel):
    """A fulfillment object describing how an order is fulfilled.

    Used in catalogs (available fulfillment types), orders (selected fulfillment),
    and status updates (fulfillment tracking).
    """

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(default=None, description="Fulfillment identifier")
    type: Optional[str] = Field(
        default=None,
        description="Fulfillment type (e.g. 'Delivery', 'Self-Pickup')",
    )
    state: Optional[FulfillmentState] = Field(
        default=None,
        description="Current fulfillment state",
    )
    tracking: Optional[bool] = Field(
        default=None,
        description="Whether real-time tracking is available",
    )
    start: Optional[FulfillmentStart] = Field(
        default=None,
        description="Pickup/start details",
    )
    end: Optional[FulfillmentEnd] = Field(
        default=None,
        description="Delivery/end details",
    )
    stops: Optional[list[FulfillmentStop]] = Field(
        default=None,
        description="Ordered list of stops (alternative to start/end)",
    )
    agent: Optional[Person] = Field(
        default=None,
        description="Delivery agent or service person",
    )
    vehicle: Optional[dict[str, Any]] = Field(
        default=None,
        description="Vehicle details for logistics fulfillment",
    )
    rateable: Optional[bool] = Field(
        default=None,
        description="Whether this fulfillment can be rated",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional metadata tags",
    )

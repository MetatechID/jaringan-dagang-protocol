"""Beckn protocol context model.

Every Beckn request/response carries a BecknContext that identifies the
transaction, participants, action, and addressing information.

The ``domain`` field is a free string and intentionally accepts both raw
Beckn transport domains (e.g. ``nic2004:52110``) and ONDC localization
codes (e.g. ``ONDC:RET11``). Callers should resolve the per-store ONDC
code via ``python.domain_resolver.resolve_ondc_domain`` rather than
hardcoding it. See ``network-extension/domains/retail.yaml`` for the
ONDC retail taxonomy.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BecknAction(str, Enum):
    """All Beckn protocol actions and their callbacks."""

    # Buyer-side actions
    SEARCH = "search"
    SELECT = "select"
    INIT = "init"
    CONFIRM = "confirm"
    STATUS = "status"
    TRACK = "track"
    CANCEL = "cancel"
    UPDATE = "update"
    RATING = "rating"
    SUPPORT = "support"

    # Seller-side callbacks
    ON_SEARCH = "on_search"
    ON_SELECT = "on_select"
    ON_INIT = "on_init"
    ON_CONFIRM = "on_confirm"
    ON_STATUS = "on_status"
    ON_TRACK = "on_track"
    ON_CANCEL = "on_cancel"
    ON_UPDATE = "on_update"
    ON_RATING = "on_rating"
    ON_SUPPORT = "on_support"


class BecknCity(BaseModel):
    """City information in the context."""

    model_config = {"populate_by_name": True}

    code: str = Field(
        ...,
        description="City code (e.g. 'std:080' for Bangalore, 'ID:JKT' for Jakarta)",
    )
    name: Optional[str] = Field(
        default=None,
        description="Human-readable city name",
    )


class BecknCountry(BaseModel):
    """Country information in the context."""

    model_config = {"populate_by_name": True}

    code: str = Field(
        ...,
        description="ISO 3166-1 alpha-3 country code (e.g. 'IDN', 'IND')",
    )
    name: Optional[str] = Field(
        default=None,
        description="Human-readable country name",
    )


class BecknLocation(BaseModel):
    """Location wrapper holding city and country in the context."""

    model_config = {"populate_by_name": True}

    city: BecknCity = Field(..., description="City information")
    country: BecknCountry = Field(..., description="Country information")


class BecknContext(BaseModel):
    """The context object present in every Beckn protocol message.

    Identifies the transaction, participants (BAP/BPP), domain, and action.
    """

    model_config = {"populate_by_name": True}

    domain: str = Field(
        ...,
        description="Beckn domain (e.g. 'nic2004:52110' for retail, 'ONDC:RET10')",
    )
    action: BecknAction = Field(
        ...,
        description="The Beckn action being performed",
    )
    core_version: Optional[str] = Field(
        default=None,
        alias="core_version",
        description="Beckn core specification version (e.g. '1.1.0')",
    )
    bap_id: str = Field(
        ...,
        description="Subscriber ID of the Buyer Application Platform",
    )
    bap_uri: str = Field(
        ...,
        description="Subscriber URL of the Buyer Application Platform",
    )
    bpp_id: Optional[str] = Field(
        default=None,
        description="Subscriber ID of the Buyer Provider Platform (absent in search)",
    )
    bpp_uri: Optional[str] = Field(
        default=None,
        description="Subscriber URL of the Buyer Provider Platform (absent in search)",
    )
    transaction_id: str = Field(
        ...,
        description="Unique ID for the complete transaction lifecycle",
    )
    message_id: str = Field(
        ...,
        description="Unique ID for this specific message/request",
    )
    timestamp: datetime = Field(
        ...,
        description="ISO 8601 timestamp with timezone (e.g. '2026-04-05T12:00:00+07:00')",
    )
    ttl: Optional[str] = Field(
        default=None,
        description="Message validity duration in ISO 8601 format (e.g. 'PT30S')",
    )
    location: Optional[BecknLocation] = Field(
        default=None,
        description="Location context (city and country)",
    )

    # Convenience properties for backward-compatible city/country access
    @property
    def city(self) -> Optional[str]:
        """Return city code from location, if present."""
        if self.location and self.location.city:
            return self.location.city.code
        return None

    @property
    def country(self) -> Optional[str]:
        """Return country code from location, if present."""
        if self.location and self.location.country:
            return self.location.country.code
        return None

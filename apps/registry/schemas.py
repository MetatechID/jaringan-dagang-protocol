"""Pydantic schemas for registry API request/response validation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SubscriberType(str, Enum):
    """Beckn network participant types."""
    BAP = "BAP"
    BPP = "BPP"
    BG = "BG"


class SubscriberStatus(str, Enum):
    """Subscription lifecycle statuses."""
    INITIATED = "INITIATED"
    SUBSCRIBED = "SUBSCRIBED"
    INVALID_SSL = "INVALID_SSL"
    UNSUBSCRIBED = "UNSUBSCRIBED"


# --- Request schemas ---

class SubscribeRequest(BaseModel):
    """Request body for POST /subscribe."""
    subscriber_id: str = Field(
        ..., min_length=1, max_length=255,
        description="Unique identifier (e.g. 'bap.jaringan.id')",
        examples=["bap.jaringan.id"],
    )
    subscriber_url: str = Field(
        ..., min_length=1, max_length=512,
        description="Base URL for the subscriber's Beckn API",
        examples=["https://bap.jaringan.id/beckn"],
    )
    type: SubscriberType = Field(
        ..., description="Participant type",
    )
    domain: str = Field(
        ..., min_length=1, max_length=100,
        description="Beckn domain code",
        examples=["ONDC:RET10"],
    )
    city: str = Field(
        ..., min_length=1, max_length=50,
        description="City code",
        examples=["ID:JKT"],
    )
    signing_public_key: str = Field(
        ..., min_length=1,
        description="Ed25519 public key (base64)",
    )
    encryption_public_key: str = Field(
        ..., min_length=1,
        description="X25519 public key (base64)",
    )


class LookupRequest(BaseModel):
    """Request body for POST /lookup.

    All fields are optional filters; at least one should be provided.
    """
    subscriber_id: Optional[str] = Field(
        default=None, description="Filter by subscriber ID",
    )
    type: Optional[SubscriberType] = Field(
        default=None, description="Filter by participant type",
    )
    domain: Optional[str] = Field(
        default=None, description="Filter by Beckn domain",
    )
    city: Optional[str] = Field(
        default=None, description="Filter by city code",
    )


# --- Response schemas ---

class SubscriberResponse(BaseModel):
    """A subscriber record returned from the registry."""

    model_config = {"from_attributes": True}

    subscriber_id: str
    subscriber_url: str
    type: SubscriberType
    domain: str
    city: str
    signing_public_key: str
    encryption_public_key: str
    status: SubscriberStatus
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SubscribeResponse(BaseModel):
    """Response for a successful subscribe operation."""
    message: str = "Subscriber registered successfully"
    subscriber: SubscriberResponse


class LookupResponse(BaseModel):
    """Response for a lookup query."""
    count: int
    subscribers: list[SubscriberResponse]


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    service: str = "beckn-registry"
    version: str = "0.1.0"

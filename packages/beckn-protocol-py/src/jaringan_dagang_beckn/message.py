"""Beckn protocol message envelope models.

These are the top-level request/response types that wrap context + message/error.
Every Beckn API call uses BecknRequest as the body, and every callback uses
BecknResponse.
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .context import BecknContext
from .errors import BecknError


class AckStatus(str, Enum):
    """Acknowledgment status values."""

    ACK = "ACK"
    NACK = "NACK"


class AckMessage(BaseModel):
    """Synchronous acknowledgment returned by the receiver.

    Every Beckn endpoint responds synchronously with an ACK/NACK
    before processing the request asynchronously.
    """

    model_config = {"populate_by_name": True}

    status: AckStatus = Field(
        ...,
        description="ACK if the message was accepted for processing, NACK otherwise",
    )


class AckResponse(BaseModel):
    """The synchronous HTTP response returned by any Beckn endpoint.

    Supports both Beckn formats:
      - {"message": {"ack": {"status": "ACK"}}}  (standard Beckn)
      - {"message": {"status": "ACK"}}            (flat)
    """

    model_config = {"populate_by_name": True}

    message: Any = Field(
        ...,
        description="Acknowledgment message",
    )
    error: Optional[BecknError] = Field(
        default=None,
        description="Error details if status is NACK",
    )

    @property
    def ack_status(self) -> str:
        """Extract the ACK/NACK status from either format."""
        if isinstance(self.message, dict):
            # {"ack": {"status": "ACK"}} or {"status": "ACK"}
            ack = self.message.get("ack", self.message)
            if isinstance(ack, dict):
                return ack.get("status", "NACK")
        if isinstance(self.message, AckMessage):
            return self.message.status.value
        return "NACK"


class BecknRequest(BaseModel):
    """A Beckn protocol request (sent from BAP to BPP or vice versa).

    Structure:
        {
            "context": { ... },
            "message": { ... }  # varies by action
        }

    The `message` payload is typed as dict[str, Any] because it varies
    depending on context.action:
        - search:  {"intent": {...}}
        - select:  {"order": {...}}
        - init:    {"order": {...}}
        - confirm: {"order": {...}}
        - status:  {"order_id": "..."}
        - track:   {"order_id": "...", "callback_url": "..."}
        - cancel:  {"order_id": "...", "cancellation_reason_id": "..."}
        - update:  {"order": {...}, "update_target": "..."}
        - rating:  {"ratings": [...]}
        - support: {"ref_id": "..."}
    """

    model_config = {"populate_by_name": True}

    context: BecknContext = Field(
        ...,
        description="Beckn context identifying the transaction, action, and participants",
    )
    message: dict[str, Any] = Field(
        ...,
        description="Action-specific message payload",
    )


class BecknResponse(BaseModel):
    """A Beckn protocol callback response (on_search, on_select, etc.).

    Structure:
        {
            "context": { ... },
            "message": { ... },  # varies by callback action
            "error": { ... }     # optional
        }

    The `message` payload varies by callback action:
        - on_search:  {"catalog": {...}}
        - on_select:  {"order": {...}}
        - on_init:    {"order": {...}}
        - on_confirm: {"order": {...}}
        - on_status:  {"order": {...}}
        - on_track:   {"tracking": {...}}
        - on_cancel:  {"order": {...}}
        - on_update:  {"order": {...}}
        - on_rating:  {"feedback_ack": true, ...}
        - on_support: {"phone": "...", "email": "...", "uri": "..."}
    """

    model_config = {"populate_by_name": True}

    context: BecknContext = Field(
        ...,
        description="Beckn context (mirrors the request context with callback action)",
    )
    message: Optional[dict[str, Any]] = Field(
        default=None,
        description="Callback-specific message payload (absent on error-only responses)",
    )
    error: Optional[BecknError] = Field(
        default=None,
        description="Error details if the action failed",
    )

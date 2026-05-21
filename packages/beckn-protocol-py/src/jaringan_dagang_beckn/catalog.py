"""Beckn protocol catalog models.

Models for representing catalogs, providers, items, and their associated
metadata as returned in on_search responses.
"""

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class Image(BaseModel):
    """An image reference in the Beckn protocol."""

    model_config = {"populate_by_name": True}

    url: str = Field(..., description="Fully qualified URL of the image")
    size_type: Optional[str] = Field(
        default=None,
        description="Size variant (e.g. 'xs', 'sm', 'md', 'lg')",
    )
    width: Optional[str] = Field(default=None, description="Image width in pixels")
    height: Optional[str] = Field(default=None, description="Image height in pixels")


class Descriptor(BaseModel):
    """Describes a Beckn entity (provider, item, fulfillment, etc.)."""

    model_config = {"populate_by_name": True}

    name: Optional[str] = Field(default=None, description="Display name")
    code: Optional[str] = Field(default=None, description="Machine-readable code")
    short_desc: Optional[str] = Field(default=None, description="Short description")
    long_desc: Optional[str] = Field(default=None, description="Long description")
    symbol: Optional[str] = Field(
        default=None,
        description="URL to a symbol/icon image",
    )
    images: Optional[list[Image]] = Field(
        default=None,
        description="List of associated images",
    )
    additional_desc: Optional[dict[str, Any]] = Field(
        default=None,
        description="Additional descriptive information as key-value pairs",
    )


class TagValue(BaseModel):
    """A single key-value tag entry.

    Beckn/ONDC tag lists use the flat ``{code, value}`` shape (e.g. ONDC
    ``@ondc/org/...`` groups). ``descriptor`` is retained for the older
    in-repo convention (e.g. variant tags emitted as
    ``{descriptor:{code},value}``); ``code`` is the canonical ONDC tag
    list key. Both are optional so existing payloads keep validating.
    """

    model_config = {"populate_by_name": True}

    code: Optional[str] = Field(
        default=None,
        description="Machine-readable tag key (ONDC/Beckn tag list code)",
    )
    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Descriptor for this tag value",
    )
    value: str = Field(..., description="Tag value")


class Tag(BaseModel):
    """A group of tags for categorized metadata on Beckn objects."""

    model_config = {"populate_by_name": True}

    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Descriptor for this tag group",
    )
    code: Optional[str] = Field(
        default=None,
        description="Machine-readable tag group code",
    )
    name: Optional[str] = Field(
        default=None,
        description="Human-readable tag group name",
    )
    display: Optional[bool] = Field(
        default=None,
        description="Whether this tag group should be displayed to the user",
    )
    list: Optional[List[TagValue]] = Field(
        default=None,
        description="List of tag values within this group",
    )


class Price(BaseModel):
    """Price information. Amounts are strings per Beckn convention."""

    model_config = {"populate_by_name": True}

    currency: str = Field(
        ...,
        description="ISO 4217 currency code (e.g. 'IDR', 'INR')",
    )
    value: str = Field(
        ...,
        description="Price amount as a string (e.g. '150000')",
    )
    estimated_value: Optional[str] = Field(
        default=None,
        description="Estimated price if exact is unknown",
    )
    computed_value: Optional[str] = Field(
        default=None,
        description="System-computed price",
    )
    listed_value: Optional[str] = Field(
        default=None,
        description="Original listed/MRP price",
    )
    offered_value: Optional[str] = Field(
        default=None,
        description="Offered/discounted price",
    )
    minimum_value: Optional[str] = Field(
        default=None,
        description="Minimum price in a range",
    )
    maximum_value: Optional[str] = Field(
        default=None,
        description="Maximum price in a range",
    )


class QuantityMeasure(BaseModel):
    """A measured quantity with a unit."""

    model_config = {"populate_by_name": True}

    value: str = Field(..., description="Numeric value as string")
    unit: str = Field(..., description="Unit of measurement (e.g. 'kilogram', 'litre')")


class QuantityDetail(BaseModel):
    """Detail for a quantity measure."""

    model_config = {"populate_by_name": True}

    count: Optional[int] = Field(default=None, description="Discrete count")
    measure: Optional[QuantityMeasure] = Field(
        default=None,
        description="Continuous measure with unit",
    )


class Quantity(BaseModel):
    """Quantity information for an item."""

    model_config = {"populate_by_name": True}

    available: Optional[QuantityDetail] = Field(
        default=None,
        description="Available quantity",
    )
    allocated: Optional[QuantityDetail] = Field(
        default=None,
        description="Allocated/reserved quantity",
    )
    selected: Optional[QuantityDetail] = Field(
        default=None,
        description="Quantity selected by buyer",
    )
    unitized: Optional[QuantityDetail] = Field(
        default=None,
        description="Per-unit quantity (e.g. price per kg)",
    )
    maximum: Optional[QuantityDetail] = Field(
        default=None,
        description="Maximum orderable quantity",
    )
    minimum: Optional[QuantityDetail] = Field(
        default=None,
        description="Minimum orderable quantity",
    )


class CategoryId(BaseModel):
    """Category reference."""

    model_config = {"populate_by_name": True}

    id: str = Field(..., description="Unique category identifier")
    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Category descriptor",
    )
    parent_category_id: Optional[str] = Field(
        default=None,
        description="Parent category for hierarchical categorization",
    )
    tags: Optional[list[Tag]] = Field(default=None, description="Category tags")


class Item(BaseModel):
    """An item in the Beckn catalog (product or service)."""

    model_config = {"populate_by_name": True}

    id: str = Field(..., description="Unique item identifier")
    parent_item_id: Optional[str] = Field(
        default=None,
        description="Parent item id, for grouping variants under one product",
    )
    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Item descriptor with name, description, images",
    )
    price: Optional[Price] = Field(default=None, description="Item price")
    quantity: Optional[Quantity] = Field(
        default=None,
        description="Quantity information",
    )
    category_ids: Optional[list[str]] = Field(
        default=None,
        description="List of category IDs this item belongs to",
    )
    category_id: Optional[str] = Field(
        default=None,
        description="Primary category ID (legacy field)",
    )
    fulfillment_ids: Optional[list[str]] = Field(
        default=None,
        description="Fulfillment options available for this item",
    )
    location_ids: Optional[list[str]] = Field(
        default=None,
        description="Location IDs where this item is available",
    )
    payment_ids: Optional[list[str]] = Field(
        default=None,
        description="Payment options available for this item",
    )
    rateable: Optional[bool] = Field(
        default=None,
        description="Whether this item can be rated",
    )
    matched: Optional[bool] = Field(
        default=None,
        description="Whether this item matched the search criteria",
    )
    related: Optional[bool] = Field(
        default=None,
        description="Whether this item is related to search criteria",
    )
    recommended: Optional[bool] = Field(
        default=None,
        description="Whether this item is recommended",
    )
    time: Optional[dict[str, Any]] = Field(
        default=None,
        description="Time constraints for availability",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional metadata tags",
    )


class Provider(BaseModel):
    """A provider (seller/merchant) in the Beckn network."""

    model_config = {"populate_by_name": True}

    id: str = Field(..., description="Unique provider identifier")
    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Provider descriptor with name, images, etc.",
    )
    categories: Optional[list[CategoryId]] = Field(
        default=None,
        description="Categories offered by this provider",
    )
    items: Optional[list[Item]] = Field(
        default=None,
        description="Items offered by this provider",
    )
    fulfillments: Optional[list[Any]] = Field(
        default=None,
        description="Fulfillment options offered (typed in fulfillment.py)",
    )
    payments: Optional[list[Any]] = Field(
        default=None,
        description="Payment options accepted (typed in payment.py)",
    )
    locations: Optional[list[Any]] = Field(
        default=None,
        description="Provider locations",
    )
    rateable: Optional[bool] = Field(
        default=None,
        description="Whether this provider can be rated",
    )
    ttl: Optional[str] = Field(
        default=None,
        description="Catalog validity in ISO 8601 duration format",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional provider metadata tags",
    )


class Catalog(BaseModel):
    """The top-level catalog returned in an on_search response."""

    model_config = {"populate_by_name": True}

    descriptor: Optional[Descriptor] = Field(
        default=None,
        description="Catalog-level descriptor (e.g. BPP name)",
    )
    providers: Optional[list[Provider]] = Field(
        default=None,
        description="List of providers in this catalog",
    )
    fulfillments: Optional[list[Any]] = Field(
        default=None,
        description="Catalog-level fulfillment types",
    )
    payments: Optional[list[Any]] = Field(
        default=None,
        description="Catalog-level payment options",
    )

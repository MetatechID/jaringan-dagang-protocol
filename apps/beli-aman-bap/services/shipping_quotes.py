"""Server-authoritative checkout shipping, bound to tenant, cart and destination."""
from copy import deepcopy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from config import settings
from services import carriers, catalog
from services import shipping, jubelio


def postal_code(value) -> str:
    if not isinstance(value, str) or len(value.strip()) != 5 or not value.strip().isascii() or not value.strip().isdigit():
        raise HTTPException(422, "A five-digit destination postal code is required")
    return value.strip()


def free_shipping(brand) -> bool:
    # Operator-controlled policy only; never inferred from missing client fields.
    return brand.slug in {s.strip() for s in settings.shipping_free_brand_slugs.split(",") if s.strip()}


def resolve_cart(products, items):
    index = {}
    for product in products:
        index[product["sku"]] = product
        for variant in product.get("variants") or []:
            index[variant["sku"]] = {
                **product, **variant,
                "name": f'{product["name"]} - {variant.get("label", "")}',
                "price_idr": variant.get("price_idr") if variant.get("price_idr") is not None else product.get("price_idr"),
                "weight_grams": variant.get("weight_grams") or product.get("weight_grams"),
                "image": variant.get("image") or product.get("image"),
            }
    if not items:
        raise HTTPException(422, "Cart must contain items")
    snapshots, carrier_items = [], []
    for item in items:
        sku = item.sku if hasattr(item, "sku") else item.get("sku")
        qty = item.qty if hasattr(item, "qty") else item.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise HTTPException(422, "Invalid cart quantity")
        product = index.get(sku)
        if product is None:
            raise HTTPException(400, "SKU is not available for this brand")
        try:
            raw_price = Decimal(str(product["price_idr"]))
            raw_weight = Decimal(str(product.get("weight_grams") or 500))
            if (not raw_price.is_finite() or not raw_weight.is_finite()
                    or raw_price < 0 or raw_weight <= 0
                    or raw_price != raw_price.to_integral_value()
                    or raw_weight != raw_weight.to_integral_value()):
                raise ValueError()
            price, weight = int(raw_price), int(raw_weight)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise HTTPException(503, "Catalog shipping data is unavailable")
        snapshots.append({"sku": sku, "name": product["name"], "qty": qty,
                          "unit_price_idr": price, "weight_grams": weight, "image": product.get("image")})
        carrier_items.append({"name": product["name"], "quantity": qty, "value": price, "weight": weight})
    return snapshots, carrier_items


async def rates_for_cart(brand, destination, items, products=None, service_category_id=None):
    destination = postal_code(destination)
    products = products if products is not None else await catalog.list_products(brand.slug)
    snapshots, carrier_items = resolve_cart(products, items)
    try:
        rates = await carriers.get_rates(
            brand=brand, destination_postal_code=destination, items=carrier_items,
            total_value=sum(line["value"] * line["quantity"] for line in carrier_items),
            service_category_id=service_category_id,
        )
    except (carriers.ShippingError, shipping.ShippingError, jubelio.ShippingError):
        raise HTTPException(503, "Shipping rates are temporarily unavailable")
    if not isinstance(rates, list) or not rates or any(not isinstance(rate, dict) for rate in rates):
        raise HTTPException(503, "No shipping rates available")
    return snapshots, rates


async def select_shipping(brand, destination, items, choice, products=None):
    destination = postal_code(destination)
    if free_shipping(brand):
        snapshots, _ = resolve_cart(products if products is not None else await catalog.list_products(brand.slug), items)
        return snapshots, {"server_free_shipping": True, "price_idr": 0}, 0
    if choice is None:
        raise HTTPException(422, "A shipping service is required")
    snapshots, rates = await rates_for_cart(brand, destination, items, products)
    matches = [r for r in rates if r.get("courier_code") not in (None, "", "None")
               and r.get("courier_service_code") not in (None, "", "None")
               and str(r.get("courier_code")) == choice.courier_code
               and str(r.get("courier_service_code")) == choice.courier_service_code]
    if len(matches) != 1:
        raise HTTPException(409, "Selected shipping service is unavailable; refresh rates")
    rate = matches[0]
    try:
        amount = Decimal(str(rate.get("price")))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError()
        price = int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(503, "Carrier returned an invalid shipping price")
    if choice.price_idr != price:
        raise HTTPException(409, "Shipping price changed; refresh rates")
    return snapshots, {"courier_code": choice.courier_code,
                       "courier_service_code": choice.courier_service_code,
                       "courier_service_name": rate.get("courier_service_name"),
                       "duration": rate.get("duration"), "carrier": carriers.active_carrier(brand),
                       "price_idr": price}, price


def origin_context(brand):
    carrier = carriers.active_carrier(brand)
    address = getattr(brand, "jubelio_origin_address" if carrier == carriers.JUBELIO else "biteship_origin_address", None)
    if carrier == carriers.JUBELIO:
        # Include the effective ZIP when dispatch uses the legacy origin fallback.
        address = {"address": address, "effective_zip": (address or {}).get("zipcode") or carriers._biteship_origin_zip(brand)}
    # The binding is returned with the order; do not expose origin contact data.
    fingerprint = hashlib.sha256(json.dumps(address or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"carrier": carrier, "address_sha256": fingerprint}


def binding(brand, destination, snapshots, courier) -> dict[str, Any]:
    return deepcopy({"brand_id": str(brand.id), "bpp_id": brand.bpp_id,
                     "destination_postal_code": postal_code(destination),
                     "origin": origin_context(brand), "items": snapshots, "courier": courier})

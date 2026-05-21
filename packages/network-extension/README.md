# Indonesia Network Extension Layer

This directory defines Indonesia-specific standards, taxonomies, and configurations for the Beckn commerce network operating under the **Jaringan Dagang** project.

## Canonical Subscriber ID Scheme (Task A3)

Every network participant identifies itself by a stable Beckn `subscriber_id` that is independent of any DNS host or service deployment. The canonical scheme is:

| Role | Canonical `subscriber_id` |
|---|---|
| Buyer-protection BAP (Beli Aman) | `beli-aman.bap.jaringan-dagang.id` |
| Per-store BPP | `<slug>.jaringan-dagang.id` (e.g. `safiyafood.jaringan-dagang.id`) |
| Single-tenant fallback BPP | `bpp.jaringan-dagang.id` |
| Gateway | `gateway.jaringan-dagang.id` |
| Registry | `registry.jaringan-dagang.id` |

Currently-registered BPPs (see `../subscribers.yaml`):

| `subscriber_id` | Slug | ONDC sub-domain |
|---|---|---|
| `safiyafood.jaringan-dagang.id` | `safiyafood` | `ONDC:RET11` Packaged F&B |
| `matchamu.jaringan-dagang.id` | `matchamu` | `ONDC:RET11` Packaged F&B |
| `antarestar.jaringan-dagang.id` | `antarestar` | `ONDC:RET12` Fashion |
| `gendes.jaringan-dagang.id` | `gendes` | `ONDC:RET15` Health & Beauty |
| `optimumnutrition.jaringan-dagang.id` | `optimumnutrition` | `ONDC:RET15` Health & Beauty |
| `yourbrand.jaringan-dagang.id` | `yourbrand` | `ONDC:RET` (demo) |
| `bpp.jaringan-dagang.id` | (fallback) | `ONDC:RET` |

### Migration from legacy identifiers

Pre-A3 the seller DB used `bpp.<slug>.local` for some stores and `*.bpp.metatech.id` was the original network identity. The live `stores` table on the seller (Neon) has three rows still on the legacy form (`antarestar`, `gendes`, `yourbrand`); they are migrated to canonical via an idempotent dry-run-default script:

```bash
# Print the SQL only (no DB connection):
cd ~/Code/jaringan-dagang-seller
python scripts/migrate-subscriber-ids.py

# Apply against the live DB:
DATABASE_URL=postgresql+asyncpg://... \
    python scripts/migrate-subscriber-ids.py --apply
```

After running, every Store row carries its canonical `<slug>.jaringan-dagang.id`. The per-store signing keys (`dev/keys/<slug>.private.b64`) are unchanged — only the subscriber identifier is renamed.

## Purpose

The Beckn protocol is domain-agnostic and country-agnostic by design. This extension layer localizes the protocol for the Indonesian market by providing:

- **Domain definitions** with sub-domains tailored to Indonesian commerce patterns
- **City codes** using the Indonesian telephone area code standard (`std:XXX`)
- **Payment methods** covering QRIS, virtual accounts, e-wallets, cards, and retail counters
- **Logistics providers** mapping Indonesian couriers to the Beckn Fulfillment schema
- **Category taxonomies** with Bahasa Indonesia labels and local product classifications

## Directory Structure

```
infra/network-extension/
├── README.md                    # This file
├── cities.yaml                  # 30 major Indonesian cities with Beckn city codes
├── payment-methods.yaml         # All payment methods mapped to Beckn Payment schema
├── logistics-providers.yaml     # Courier services mapped to Beckn Fulfillment schema
├── domains/
│   ├── retail.yaml              # Retail domain (ONDC:RET) with sub-domains
│   ├── food-beverage.yaml       # F&B domain (ONDC:FNB) with fulfillment types
│   └── logistics.yaml           # Logistics domain (ONDC:LOG) with shipping zones
└── categories/
    ├── food.yaml                # Food & grocery taxonomy (bumbu, minuman, mie, etc.)
    ├── fashion.yaml             # Fashion taxonomy (batik, hijab, tenun, etc.)
    └── electronics.yaml         # Electronics taxonomy (HP, komputer, TV, etc.)
```

## Schema Conventions

Every entry in the YAML files follows a consistent structure:

| Field | Description |
|---|---|
| `code` | Unique machine-readable identifier |
| `name` | English display name |
| `name_id` | Bahasa Indonesia display name |
| `description` | Human-readable explanation of the entry |
| `beckn_*` | Mapping fields to Beckn protocol schemas (e.g., `beckn_domain`, `beckn_category_id`, `beckn_payment_type`) |

## How to Use

### 1. City Codes in Beckn Context

When constructing a Beckn `Context` object, use the city code from `cities.yaml`:

```json
{
  "context": {
    "domain": "ONDC:RET10",
    "country": "IND",
    "city": "std:031",
    "action": "search"
  }
}
```

### 2. Payment Methods in Beckn Order

Reference payment method codes from `payment-methods.yaml` when building a Beckn `Payment` object:

```json
{
  "payment": {
    "type": "ON-ORDER",
    "collected_by": "BAP",
    "params": {
      "payment_method": "QRIS",
      "currency": "IDR"
    }
  }
}
```

### 3. Fulfillment with Logistics Providers

Map logistics service codes from `logistics-providers.yaml` to Beckn `Fulfillment`:

```json
{
  "fulfillment": {
    "type": "Delivery",
    "provider_id": "jne.co.id",
    "category": "Express Delivery",
    "tags": {
      "service_code": "JNE_YES"
    }
  }
}
```

### 4. Category Taxonomy in Catalog

Use category codes from the `categories/` files when publishing items to a Beckn catalog:

```json
{
  "item": {
    "category_id": "FOOD_BUMBU_SAMBAL",
    "descriptor": {
      "name": "Sambal Terasi Bu Rudy",
      "short_desc": "Sambal terasi khas Surabaya"
    }
  }
}
```

## Data Sources

- City area codes: Indonesian telecommunications numbering plan
- Payment methods: Bank Indonesia QRIS standard, major payment gateway integrations
- Logistics providers: Active Indonesian courier and delivery services
- Categories: Common product taxonomy used by Indonesian e-commerce platforms

## Updating

When adding new entries:

1. Follow the existing YAML structure and field naming conventions
2. Always include both `name` (English) and `name_id` (Bahasa Indonesia) fields
3. Provide a `code` that is unique within its file
4. Include relevant `beckn_*` mapping fields for protocol interoperability
5. Add `description` to clarify scope and purpose

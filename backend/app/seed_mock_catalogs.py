from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from backend.app.db import SessionLocal
from backend.app.models import CatalogEmail, CatalogItem, Supplier


TENANT_ID = UUID(os.getenv("MOCK_TENANT_ID", "11111111-1111-4111-8111-111111111111"))
NAMESPACE = UUID("22222222-2222-4222-8222-222222222222")

SUPPLIERS = [
    ("Aarav Pharma Excipients", "aaravpharma.example", "India"),
    ("BioNova Ingredients", "bionova.example", "Germany"),
    ("Cedar API Traders", "cedarapi.example", "China"),
    ("Dhanvantari Nutraceuticals", "dhanvantari.example", "India"),
    ("Evergreen Fine Chemicals", "evergreenfine.example", "USA"),
    ("Fusion Life Sciences", "fusionlife.example", "Switzerland"),
    ("Galaxy Medisource", "galaxymedi.example", "India"),
    ("Horizon Bulk Actives", "horizonbulk.example", "China"),
    ("Indigo Healthcare Inputs", "indigohealth.example", "India"),
    ("Jasmine Specialty Pharma", "jasminespecialty.example", "Germany"),
]

ITEMS = [
    ("Ascorbic Acid BP", "ascorbic acid", "kg", 395, 620),
    ("Paracetamol 500mg API", "paracetamol", "kg", 520, 790),
    ("Citric Acid Anhydrous", "citric acid", "kg", 115, 210),
    ("Sodium Benzoate Food/Pharma", "sodium benzoate", "kg", 155, 260),
    ("Magnesium Stearate USP", "magnesium stearate", "kg", 190, 330),
    ("Lactose Monohydrate", "lactose monohydrate", "kg", 145, 245),
    ("Microcrystalline Cellulose PH102", "microcrystalline cellulose", "kg", 225, 390),
    ("Povidone K30", "povidone k30", "kg", 680, 980),
    ("Ibuprofen API", "ibuprofen", "kg", 960, 1320),
    ("Caffeine Anhydrous", "caffeine anhydrous", "kg", 820, 1180),
    ("Zinc Sulphate Monohydrate", "zinc sulphate", "kg", 125, 235),
    ("Calcium Carbonate DC Grade", "calcium carbonate", "kg", 75, 160),
]


def stable_uuid(value: str) -> UUID:
    return uuid5(NAMESPACE, value)


def build_catalogs() -> tuple[list[Supplier], list[CatalogEmail], list[CatalogItem]]:
    rng = random.Random(42)
    today = datetime.now(UTC).replace(microsecond=0)
    suppliers: list[Supplier] = []
    emails: list[CatalogEmail] = []
    catalog_items: list[CatalogItem] = []

    cert_list = [
        "ISO, GMP, Halal",
        "ISO, GMP",
        "ISO, GMP",
        "cGMP, FDA",
        "ISO, GMP, Halal",
        "ISO, GMP",
        "ISO, GMP",
        "cGMP, FDA",
        "ISO, GMP",
        "ISO, GMP, Halal",
    ]

    for catalog_no, (supplier_name, domain, country) in enumerate(SUPPLIERS, start=1):
        supplier_id = stable_uuid(f"supplier:{domain}")
        received_at = today - timedelta(days=rng.randint(0, 21), hours=rng.randint(1, 9))
        certs = cert_list[(catalog_no - 1) % len(cert_list)]
        suppliers.append(
            Supplier(
                id=supplier_id,
                tenant_id=TENANT_ID,
                name=supplier_name,
                email_domain=domain,
                country=country,
                last_email_date=received_at,
                certifications=certs,
            )
        )

        email_id = stable_uuid(f"catalog-email:{domain}:2026-q2")
        emails.append(
            CatalogEmail(
                id=email_id,
                tenant_id=TENANT_ID,
                supplier_id=supplier_id,
                received_at=received_at,
                raw_email_id=f"core-mock-catalog-{catalog_no:02d}",
                subject=f"Q2 2026 bulk ingredient catalogue - {supplier_name}",
                pdf_url=f"mock://catalogues/{domain}/q2-2026.pdf",
                processing_status="completed",
            )
        )

        sampled_items = rng.sample(ITEMS, 8)
        for item_no, (display_name, canonical_name, unit, min_price, max_price) in enumerate(sampled_items, start=1):
            qty = rng.randrange(40, 420) * 100
            price = round(rng.uniform(min_price, max_price) * rng.uniform(0.94, 1.08), 2)
            lead_time_days = rng.choice([2, 3, 5, 7, 10, 14])
            moq_val = rng.choice([100, 250, 500, 1000])
            valid_until = today + timedelta(days=rng.randint(14, 75))
            catalog_items.append(
                CatalogItem(
                    id=stable_uuid(f"catalog-item:{domain}:{canonical_name}"),
                    tenant_id=TENANT_ID,
                    catalog_email_id=email_id,
                    supplier_id=supplier_id,
                    ingredient_name=display_name,
                    price_per_unit=price,
                    currency="INR",
                    available_qty=qty,
                    unit=unit,
                    valid_until=valid_until,
                    lead_time_days=lead_time_days,
                    moq=moq_val,
                    raw_payload={
                        "source": "mock_extracted_catalogue",
                        "source_catalogue": f"MediCORE mock catalogue {catalog_no:02d}",
                        "page": rng.randint(1, 7),
                        "supplier_sku": f"{domain.split('.')[0].upper()[:4]}-{catalog_no:02d}-{item_no:03d}",
                        "pack_size": rng.choice(["25 kg bag", "50 kg drum", "5 kg carton", "10 kg fibre drum"]),
                        "grade": rng.choice(["IP", "BP", "USP", "EP", "Food/Pharma"]),
                        "moq": moq_val,
                        "lead_time_days": lead_time_days,
                        "payment_terms": rng.choice(["Net 15", "Net 30", "50% advance", "Against proforma invoice"]),
                        "extraction_confidence": round(rng.uniform(0.86, 0.98), 2),
                        "notes": rng.choice(
                            [
                                "COA available on request",
                                "Batch reserved for May dispatch",
                                "Price valid while stock lasts",
                                "Temperature controlled storage recommended",
                            ]
                        ),
                    },
                )
            )

    return suppliers, emails, catalog_items


def seed() -> None:
    global TENANT_ID
    env_tenant = os.getenv("MOCK_TENANT_ID")
    if env_tenant:
        try:
            TENANT_ID = UUID(env_tenant)
            print(f"Using MOCK_TENANT_ID from environment: {TENANT_ID}")
        except ValueError:
            print(f"Invalid MOCK_TENANT_ID environment variable: {env_tenant}")
    else:
        with SessionLocal() as db:
            from backend.app.models import Profile
            first_profile = db.query(Profile).first()
            if first_profile:
                TENANT_ID = first_profile.id
                print(f"Dynamically selected tenant ID from registered user profile: {TENANT_ID}")
            else:
                print("No registered user profiles found in database. Using default fallback tenant ID: 11111111-1111-4111-8111-111111111111")
                print("Tip: Register a user in the UI first, then run this seeder to see mock data on your dashboard.")

    suppliers, emails, catalog_items = build_catalogs()
    with SessionLocal() as db:
        for row in suppliers:
            db.merge(row)
        db.flush()

        for row in emails:
            db.merge(row)
        db.flush()

        for row in catalog_items:
            db.merge(row)
        db.commit()

    print(
        "Seeded MediCORE mock catalogue data: "
        f"{len(suppliers)} suppliers, {len(emails)} catalogues, {len(catalog_items)} items."
    )
    print(f"Tenant id: {TENANT_ID}")


if __name__ == "__main__":
    seed()

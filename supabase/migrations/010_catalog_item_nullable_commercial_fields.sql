alter table catalog_items
    alter column price_per_unit drop not null,
    alter column available_qty drop not null,
    alter column unit drop not null;

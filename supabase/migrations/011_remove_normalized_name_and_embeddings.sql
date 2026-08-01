drop index if exists idx_catalog_items_embedding;
drop index if exists idx_catalog_items_name_qty;

alter table catalog_items
    drop column if exists normalized_name,
    drop column if exists embedding;

create index if not exists idx_catalog_items_ingredient_qty
    on catalog_items(ingredient_name, available_qty);

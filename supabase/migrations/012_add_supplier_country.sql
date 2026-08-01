alter table suppliers
    add column if not exists country text not null default 'Unknown';

update suppliers
set country = 'Unknown'
where country is null or btrim(country) = '';

create index if not exists idx_suppliers_tenant_country
    on suppliers (tenant_id, country);

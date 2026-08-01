create extension if not exists "uuid-ossp";
create extension if not exists vector;

create table if not exists suppliers (
    id uuid primary key default uuid_generate_v4(),
    tenant_id uuid not null,
    name text not null,
    email_domain text not null,
    country text not null default 'Unknown',
    last_email_date timestamptz,
    certifications text,
    created_at timestamptz not null default now(),
    unique (tenant_id, email_domain)
);

create table if not exists catalog_emails (
    id uuid primary key default uuid_generate_v4(),
    tenant_id uuid not null,
    supplier_id uuid not null references suppliers(id),
    received_at timestamptz not null default now(),
    raw_email_id text not null unique,
    subject text,
    pdf_url text,
    processing_status text not null default 'queued',
    created_at timestamptz not null default now()
);

create table if not exists catalog_items (
    id uuid primary key default uuid_generate_v4(),
    tenant_id uuid not null,
    catalog_email_id uuid not null references catalog_emails(id),
    supplier_id uuid not null references suppliers(id),
    ingredient_name text not null,
    price_per_unit numeric(14,4) not null,
    currency text not null default 'INR',
    available_qty numeric(14,2) not null,
    unit text not null,
    valid_until timestamptz,
    embedding vector(384),
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists purchase_history (
    id uuid primary key default uuid_generate_v4(),
    tenant_id uuid not null,
    supplier_id uuid not null references suppliers(id),
    item_id uuid references catalog_items(id),
    purchased_at timestamptz not null default now(),
    quantity numeric(14,2) not null,
    price_paid numeric(14,4) not null
);

create table if not exists chat_sessions (
    id uuid primary key default uuid_generate_v4(),
    tenant_id uuid not null,
    user_id uuid not null,
    messages jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_catalog_items_ingredient_qty on catalog_items(ingredient_name, available_qty);
create index if not exists idx_catalog_items_price on catalog_items(price_per_unit);
create index if not exists idx_catalog_items_embedding on catalog_items using ivfflat (embedding vector_cosine_ops);

alter table suppliers enable row level security;
alter table catalog_emails enable row level security;
alter table catalog_items enable row level security;
alter table purchase_history enable row level security;
alter table chat_sessions enable row level security;

drop policy if exists tenant_suppliers on suppliers;
drop policy if exists tenant_catalog_emails on catalog_emails;
drop policy if exists tenant_catalog_items on catalog_items;
drop policy if exists tenant_purchase_history on purchase_history;
drop policy if exists tenant_chat_sessions on chat_sessions;

create policy tenant_suppliers on suppliers
    using (tenant_id::text = current_setting('app.current_tenant_id', true));
create policy tenant_catalog_emails on catalog_emails
    using (tenant_id::text = current_setting('app.current_tenant_id', true));
create policy tenant_catalog_items on catalog_items
    using (tenant_id::text = current_setting('app.current_tenant_id', true));
create policy tenant_purchase_history on purchase_history
    using (tenant_id::text = current_setting('app.current_tenant_id', true));
create policy tenant_chat_sessions on chat_sessions
    using (tenant_id::text = current_setting('app.current_tenant_id', true));

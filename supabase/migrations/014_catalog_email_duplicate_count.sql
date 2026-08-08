alter table catalog_emails
    add column if not exists duplicate_count integer not null default 0;

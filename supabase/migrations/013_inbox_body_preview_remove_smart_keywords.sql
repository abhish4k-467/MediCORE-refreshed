alter table public.catalog_emails
  add column if not exists body_preview text;

alter table public.email_sync_settings
  drop column if exists keyword_filters;

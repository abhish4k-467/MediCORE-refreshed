-- Login & Registration Tables and RLS policies

-- 1. PROFILES table (tied to auth.users)
create table if not exists profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    full_name text,
    organisation text,
    role text,
    created_at timestamptz not null default now()
);

-- 2. EMAIL_ACCOUNTS table
create table if not exists email_accounts (
    id uuid primary key default uuid_generate_v4(),
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null,
    email_address text not null,
    imap_host text not null,
    imap_port integer not null,
    encrypted_password text not null,
    sync_status text not null default 'pending',
    sync_error_msg text,
    last_synced_at timestamptz,
    created_at timestamptz not null default now()
);

-- 3. EMAIL_FILTERS table
create table if not exists email_filters (
    id uuid primary key default uuid_generate_v4(),
    email_account_id uuid not null references email_accounts(id) on delete cascade,
    require_attachment boolean not null default false,
    sender_keywords text,
    subject_keywords text,
    skip_promotions_tab boolean not null default false,
    created_at timestamptz not null default now()
);

-- 4. EMAIL_SYNC_SETTINGS table
create table if not exists email_sync_settings (
    id uuid primary key default uuid_generate_v4(),
    user_id uuid not null references auth.users(id) on delete cascade unique,
    poll_interval_minutes integer not null default 15,
    auto_extract_catalog boolean not null default true,
    notify_on_new_catalog boolean not null default true,
    ingestion_approach text not null default 'approach_2',
    trusted_suppliers text not null default '',
    keyword_filters text not null default 'catalog, catalogue, price, offer, quote',
    pending_approvals text not null default '[]',
    created_at timestamptz not null default now()
);

-- RLS Enablement
alter table profiles enable row level security;
alter table email_accounts enable row level security;
alter table email_filters enable row level security;
alter table email_sync_settings enable row level security;

-- Policies for profiles
drop policy if exists "Users can view own profile" on profiles;
create policy "Users can view own profile" on profiles
    for select using (auth.uid() = id);

drop policy if exists "Users can update own profile" on profiles;
create policy "Users can update own profile" on profiles
    for update using (auth.uid() = id);

-- Policies for email_accounts
drop policy if exists "Users can manage own email accounts" on email_accounts;
create policy "Users can manage own email accounts" on email_accounts
    for all using (auth.uid() = user_id);

-- Policies for email_filters
drop policy if exists "Users can manage own filters" on email_filters;
create policy "Users can manage own filters" on email_filters
    for all using (
        exists (
            select 1 from email_accounts
            where email_accounts.id = email_filters.email_account_id
            and email_accounts.user_id = auth.uid()
        )
    );

-- Policies for email_sync_settings
drop policy if exists "Users can manage own sync settings" on email_sync_settings;
create policy "Users can manage own sync settings" on email_sync_settings
    for all using (auth.uid() = user_id);

-- Trigger function for automatic profile and sync settings creation on new user signup
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, full_name, organisation, role)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'full_name', ''),
    coalesce(new.raw_user_meta_data->>'organisation', ''),
    coalesce(new.raw_user_meta_data->>'role', 'member')
  );

  insert into public.email_sync_settings (user_id)
  values (new.id)
  on conflict (user_id) do nothing;

  return new;
end;
$$ language plpgsql security definer;

-- Trigger mapping
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

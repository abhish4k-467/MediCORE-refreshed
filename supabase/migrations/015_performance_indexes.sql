-- Migration 015: Add performance indexes for inbox and catalog queries

CREATE INDEX IF NOT EXISTS idx_catalog_emails_tenant_received ON catalog_emails (tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_emails_supplier_id ON catalog_emails (supplier_id);
CREATE INDEX IF NOT EXISTS idx_catalog_emails_status ON catalog_emails (tenant_id, processing_status);

CREATE INDEX IF NOT EXISTS idx_catalog_items_catalog_email_id ON catalog_items (catalog_email_id);
CREATE INDEX IF NOT EXISTS idx_catalog_items_tenant_supplier ON catalog_items (tenant_id, supplier_id);
CREATE INDEX IF NOT EXISTS idx_catalog_items_ingredient ON catalog_items (tenant_id, ingredient_name);

CREATE INDEX IF NOT EXISTS idx_email_accounts_user_id ON email_accounts (user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_tenant_id ON profiles (tenant_id);
CREATE INDEX IF NOT EXISTS idx_suppliers_tenant_email_domain ON suppliers (tenant_id, email_domain);

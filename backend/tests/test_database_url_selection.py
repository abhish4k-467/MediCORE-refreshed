from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import URL

from backend.app import db
from backend.app.auth import data_tenant_id_for_user


def test_employee_catalogue_data_does_not_use_organisation_tenant():
    employee_id = uuid4()
    organisation_id = uuid4()
    profile = SimpleNamespace(tenant_id=organisation_id)

    assert data_tenant_id_for_user(employee_id, profile) == employee_id


def make_settings(**overrides):
    values = {
        "database_url": db.DEFAULT_DATABASE_URL,
        "supabase_db_host": "",
        "supabase_db_port": 5432,
        "supabase_db_name": "postgres",
        "supabase_db_user": "postgres",
        "supabase_db_password": "",
        "supabase_pooler_host": "",
        "supabase_pooler_port": 6543,
        "supabase_pooler_user": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pooler_is_used_before_direct_supabase_host(monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        make_settings(
            supabase_db_host="db.example.supabase.co",
            supabase_db_password="secret",
            supabase_pooler_host="aws-1-ap-southeast-1.pooler.supabase.com",
            supabase_pooler_user="postgres.example",
        ),
    )

    url = db.build_database_url()

    assert isinstance(url, URL)
    assert url.host == "aws-1-ap-southeast-1.pooler.supabase.com"
    assert url.port == 6543
    assert url.username == "postgres.example"


def test_pooler_overrides_direct_supabase_database_url(monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        make_settings(
            database_url="postgresql://postgres:secret@db.example.supabase.co:5432/postgres",
            supabase_db_password="secret",
            supabase_pooler_host="aws-1-ap-southeast-1.pooler.supabase.com",
            supabase_pooler_user="postgres.example",
        ),
    )

    url = db.build_database_url()

    assert isinstance(url, URL)
    assert url.host == "aws-1-ap-southeast-1.pooler.supabase.com"


def test_non_supabase_database_url_still_takes_priority(monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        make_settings(
            database_url="postgresql://app:secret@railway.internal:5432/railway",
            supabase_db_password="secret",
            supabase_pooler_host="aws-1-ap-southeast-1.pooler.supabase.com",
            supabase_pooler_user="postgres.example",
        ),
    )

    url = db.build_database_url()

    assert isinstance(url, str)
    assert "railway.internal:5432/railway" in url


def test_direct_supabase_host_is_used_when_pooler_is_not_configured(monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        make_settings(
            supabase_db_host="db.example.supabase.co",
            supabase_db_password="secret",
        ),
    )

    url = db.build_database_url()

    assert isinstance(url, URL)
    assert url.host == "db.example.supabase.co"
    assert url.port == 5432


def test_transaction_pooler_disables_psycopg_prepared_statements():
    url = URL.create(
        "postgresql+psycopg",
        username="postgres.example",
        password="secret",
        host="aws-1-ap-southeast-1.pooler.supabase.com",
        port=6543,
        database="postgres",
    )

    assert db.database_connect_args(url) == {
        "sslmode": "require",
        "prepare_threshold": None,
    }


def test_session_pooler_keeps_default_psycopg_prepared_statement_behavior():
    url = URL.create(
        "postgresql+psycopg",
        username="postgres.example",
        password="secret",
        host="aws-1-ap-southeast-1.pooler.supabase.com",
        port=5432,
        database="postgres",
    )

    assert db.database_connect_args(url) == {"sslmode": "require"}

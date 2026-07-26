from collections.abc import Iterator
import logging
import urllib.parse

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from supabase import Client, create_client

from backend.app.config import get_settings


settings = get_settings()
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
logger = logging.getLogger(__name__)


def repair_database_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url

    scheme = "postgresql+psycopg"
    if url.startswith("postgresql+psycopg://"):
        url_payload = url[len("postgresql+psycopg://"):]
    elif url.startswith("postgresql://"):
        url_payload = url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url_payload = url[len("postgres://"):]
    else:
        return url

    if "@" not in url_payload:
        return f"{scheme}://{url_payload}"

    creds, conn = url_payload.rsplit("@", 1)
    if ":" in creds:
        user, password = creds.split(":", 1)
        decoded = urllib.parse.unquote(password)
        password = urllib.parse.quote_plus(decoded)
        creds = f"{user}:{password}"

    return f"{scheme}://{creds}@{conn}"


def build_pooler_url() -> URL | None:
    if not settings.supabase_pooler_host or not settings.supabase_db_password:
        return None

    return URL.create(
        "postgresql+psycopg",
        username=settings.supabase_pooler_user or settings.supabase_db_user,
        password=settings.supabase_db_password,
        host=settings.supabase_pooler_host,
        port=settings.supabase_pooler_port,
        database=settings.supabase_db_name,
    )


def is_direct_supabase_url(url: str) -> bool:
    return ".supabase.co:5432" in url and "pooler.supabase.com" not in url


def build_database_url() -> URL | str:
    # First, if direct Supabase host is provided, prioritize it for stable direct connection
    if settings.supabase_db_host and settings.supabase_db_password:
        return URL.create(
            "postgresql+psycopg",
            username=settings.supabase_db_user,
            password=settings.supabase_db_password,
            host=settings.supabase_db_host,
            port=settings.supabase_db_port,
            database=settings.supabase_db_name,
        )

    raw_url = settings.database_url.strip()
    if raw_url and raw_url != DEFAULT_DATABASE_URL and not is_direct_supabase_url(raw_url):
        return repair_database_url(raw_url)

    pooler_url = build_pooler_url()
    if pooler_url is not None:
        return pooler_url

    return repair_database_url(raw_url)


def database_url_summary() -> str:
    url = build_database_url()
    if isinstance(url, URL):
        return f"{url.host}:{url.port or 'default'}/{url.database}"

    parsed = urllib.parse.urlparse(str(url))
    return f"{parsed.hostname}:{parsed.port or 'default'}{parsed.path or ''}"



def create_app_engine():
    try:
        logger.info("Using database host: %s", database_url_summary())
        return create_engine(build_database_url(), pool_pre_ping=True, connect_args={"sslmode": "require"})
    except Exception:
        if settings.environment == "production":
            raise
        return create_engine("sqlite+pysqlite:///:memory:")


_engine = None
_SessionLocal = None
startup_error = None

try:
    _engine = create_app_engine()
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
except Exception as e:
    startup_error = e

engine = _engine


def SessionLocal(*args, **kwargs):
    if startup_error:
        raise RuntimeError(
            f"Database initialization failed at startup. Error: {startup_error}"
        ) from startup_error
    return _SessionLocal(*args, **kwargs)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    if startup_error:
        raise RuntimeError(
            "Database connection failed due to startup error. "
            f"Please check DATABASE_URL or Supabase pooler variables. Details: {startup_error}"
        ) from startup_error

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_supabase() -> Client:
    return create_client(str(settings.supabase_url), settings.supabase_service_role_key)

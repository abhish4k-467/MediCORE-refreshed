import re
import logging
from typing import Any
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

FORBIDDEN_SQL_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
    "GRANT", "REVOKE", "EXEC", "EXECUTE", "PG_SLEEP", "INTO", "COPY",
    "VACUUM", "REINDEX", "COMMENT", "RENAME", "SET", "RESET", "LOCK"
}


def validate_readonly_sql(sql_query: str) -> str:
    """
    Validates that the provided SQL query is strictly read-only (SELECT/WITH).
    Raises ValueError if query contains forbidden mutation keywords or unsafe syntax.
    """
    raw = (sql_query or "").strip()
    if not raw:
        raise ValueError("Empty SQL query provided.")

    # Remove single line comments '-- ...' and block comments '/* ... */'
    no_comments = re.sub(r"--.*$", "", raw, flags=re.MULTILINE)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL).strip().rstrip(";")

    if ";" in no_comments:
        raise ValueError("Multiple SQL statements separated by semicolons are not permitted.")

    first_word = no_comments.split()[0].upper() if no_comments else ""
    if first_word not in {"SELECT", "WITH"}:
        raise ValueError(f"Only SELECT or WITH read-only queries are permitted. Query started with '{first_word}'.")

    # Tokenize to check for forbidden keywords
    tokens = set(re.findall(r"\b[A-Za-z_]+\b", no_comments.upper()))
    forbidden_found = tokens.intersection(FORBIDDEN_SQL_KEYWORDS)
    if forbidden_found:
        raise ValueError(f"Forbidden SQL operation detected: {', '.join(sorted(forbidden_found))}.")

    # Append default limit if not present to prevent catastrophic un-bounded result sets
    if "LIMIT" not in tokens:
        no_comments = f"{no_comments} LIMIT 100"

    return no_comments


def execute_readonly_sql(
    db: Session,
    sql_query: str,
    tenant_id: UUID | str | None = None
) -> list[dict[str, Any]]:
    """
    Validates and executes a read-only SQL query against Supabase Cloud Postgres.
    Enforces tenant isolation if tenant_id is provided.
    Returns list of dict rows.
    """
    try:
        validated_sql = validate_readonly_sql(sql_query)
    except ValueError as err:
        logger.warning("SQL validation rejected query: %s. Error: %s", sql_query, err)
        return []

    params: dict[str, Any] = {}
    if tenant_id:
        tenant_str = str(tenant_id)
        # Raw model-generated SQL cannot be safely scoped after the fact.
        # Require an explicit bound tenant parameter before execution.
        tenant_predicate = re.search(
            r"\b(?:[a-z_][a-z0-9_]*\.)?tenant_id\b\s*=\s*:tenant_id\b|:tenant_id\b\s*=\s*\b(?:[a-z_][a-z0-9_]*\.)?tenant_id\b",
            validated_sql,
            flags=re.IGNORECASE,
        )
        if not tenant_predicate:
            logger.warning("Rejected unscoped SQL for tenant %s", tenant_str)
            return []
        params["tenant_id"] = tenant_str

    try:
        result = db.execute(text(validated_sql), params)
        if result.returns_rows:
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]
        return []
    except SQLAlchemyError as err:
        logger.error("Failed to execute read-only SQL: %s. Error: %s", validated_sql, err)
        return []

"""The run_sql guard: parse with sqlglot, allow one read-only SELECT, and
confine it to the tenant's schema scope.

The guard is a policy check, not a sandbox. The warehouse connection is
read-only and the row cap and timeout are enforced there too. The guard's job
is to refuse early with a message the chatbot can relay, and to report the
relations a query touches for provenance.

Rules, in order of checking:

1. The text must parse in the target dialect and contain exactly one
   statement.
2. The statement must be a SELECT, a WITH ... SELECT, or a set operation of
   SELECTs. Everything else (DDL, DML, COPY, ATTACH, PRAGMA, SET, CALL,
   DESCRIBE, SHOW, EXPLAIN, and anything sqlglot cannot classify) is refused.
3. `SELECT ... INTO` is refused.
4. Functions on the deny list are refused. The list covers DuckDB functions
   that read files, open network connections, or execute SQL from a string.
5. Every table reference must have a schema part (CTE names excepted) and the
   schema must be in scope. `information_schema` is always allowed. Scope is
   `scope.schemas`, or when that is empty, the schemas of `catalog_relations`.
   When both are empty there is no schema restriction and unqualified names
   are allowed.
6. Relations in `scope.deny_relations` are refused. Entries match on the
   trailing dotted parts, so `main_marts.fct_orders` denies
   `alpenglow.main_marts.fct_orders` too.
7. A LIMIT of `row_cap` is added when the statement has none.

The returned `sql` is the statement re-rendered in the target dialect.
"""

from __future__ import annotations

import re

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from understory.tenant import SqlScope

ALWAYS_ALLOWED_SCHEMAS = frozenset({"information_schema"})

# Exact function names (lowercase) that are never allowed.
DENIED_FUNCTIONS = frozenset(
    {
        "glob",
        "sniff_csv",
        "query",
        "query_table",
        "getenv",
        "load",
        "install",
        "from_file",
        "sql",
        "duckdb_secrets",
        "which_secret",
        "st_read",
        "st_read_meta",
        "arrow_scan",
        "delta_scan",
        "iceberg_scan",
        "iceberg_metadata",
        "iceberg_snapshots",
        "shred_json",
    }
)

# Function name prefixes (lowercase) that are never allowed.
DENIED_FUNCTION_PREFIXES = (
    "read_",
    "http",
    "parquet_",
    "postgres_",
    "sqlite_",
    "mysql_",
    "iceberg_",
    "delta_",
    "azure_",
    "s3_",
    "gcs_",
)

# A "table" whose name looks like a file path or URL. DuckDB lets you write
# `SELECT * FROM 'data.parquet'` and sqlglot parses that as a table.
_PATH_LIKE = re.compile(
    r"(://|[/\\]|\.(parquet|csv|tsv|json|jsonl|ndjson|xlsx|db|duckdb|gz|zst)$)", re.I
)


class GuardResult(BaseModel):
    ok: bool
    sql: str
    """The normalised single statement in the target dialect. Empty when refused."""
    relations: list[str] = Field(default_factory=list)
    """Tables the statement reads, dotted and unquoted, in order of appearance."""
    reason: str | None = None
    """User-facing refusal reason. None when `ok`."""


def guard_sql(
    sql: str,
    *,
    dialect: str,
    scope: SqlScope,
    catalog_relations: list[str],
    row_cap: int = 200,
) -> GuardResult:
    """Check `sql` against the tenant's policy. Never raises on bad input."""
    if not scope.enabled:
        return _refuse("run_sql is disabled for this tenant")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, TokenError) as e:
        return _refuse(f"could not parse SQL: {_first_line(str(e))}")
    except Exception as e:  # sqlglot occasionally raises other errors on junk
        return _refuse(f"could not parse SQL: {_first_line(str(e))}")

    if not statements:
        return _refuse("no SQL statement found")
    if len(statements) > 1:
        return _refuse("only one statement is allowed")
    stmt = statements[0]

    if not isinstance(stmt, (exp.Select, exp.SetOperation)):
        return _refuse(f"only SELECT statements are allowed, got {_kind(stmt)}")
    if any(isinstance(node, exp.Command) for node in stmt.walk()):
        return _refuse("statement contains unsupported syntax")
    for sel in stmt.find_all(exp.Select):
        if sel.args.get("into") is not None:
            return _refuse("SELECT INTO is not allowed")

    denied_fn = _denied_function(stmt)
    if denied_fn:
        return _refuse(f"function {denied_fn}() is not allowed in run_sql")

    allowed_schemas = _allowed_schemas(scope, catalog_relations)
    deny = [_parts(r) for r in scope.deny_relations]
    cte_names = {c.alias.lower() for c in stmt.find_all(exp.CTE) if c.alias}

    relations: list[str] = []
    seen: set[str] = set()
    for table in stmt.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # Table function or subquery in FROM; functions are checked above.
            continue
        name = table.name
        schema = table.db
        catalog = table.catalog
        if not schema and not catalog and name.lower() in cte_names:
            continue
        if _PATH_LIKE.search(name):
            return _refuse(f"reading from {name!r} is not allowed")
        if allowed_schemas is not None:
            if not schema:
                return _refuse(
                    f"table {name!r} must be schema-qualified, e.g. "
                    f"{sorted(allowed_schemas)[0]}.{name}"
                )
            if (
                schema.lower() not in allowed_schemas
                and schema.lower() not in ALWAYS_ALLOWED_SCHEMAS
            ):
                return _refuse(
                    f"schema {schema!r} is outside the run_sql scope "
                    f"({', '.join(sorted(allowed_schemas))})"
                )
        parts = [p for p in (catalog, schema, name) if p]
        if any(_suffix_match(parts, d) for d in deny):
            return _refuse(f"relation {'.'.join(parts)} is not available to run_sql")
        dotted = ".".join(parts)
        if dotted.lower() not in seen:
            seen.add(dotted.lower())
            relations.append(dotted)

    if stmt.args.get("limit") is None:
        stmt = stmt.limit(row_cap)

    return GuardResult(ok=True, sql=stmt.sql(dialect=dialect), relations=relations)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _refuse(reason: str) -> GuardResult:
    return GuardResult(ok=False, sql="", reason=reason)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text


def _kind(stmt: exp.Expression) -> str:
    return type(stmt).__name__.upper()


def _function_name(fn: exp.Func) -> str:
    if isinstance(fn, exp.Anonymous):
        return fn.name.lower()
    return fn.sql_name().lower()


def _denied_function(stmt: exp.Expression) -> str | None:
    for fn in stmt.find_all(exp.Func):
        name = _function_name(fn)
        if not name:
            continue
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES):
            return name
    return None


def _parts(relation: str) -> list[str]:
    """Split a relation string on dots, dropping quotes and lowercasing."""
    cleaned = relation.replace('"', "").replace("`", "")
    return [p.strip().lower() for p in cleaned.split(".") if p.strip()]


def _suffix_match(parts: list[str], deny: list[str]) -> bool:
    if not deny or len(deny) > len(parts):
        return False
    return [p.lower() for p in parts[-len(deny) :]] == deny


def _allowed_schemas(scope: SqlScope, catalog_relations: list[str]) -> set[str] | None:
    """Schemas run_sql may read. None means no restriction."""
    if scope.schemas:
        return {s.lower() for s in scope.schemas}
    derived: set[str] = set()
    for rel in catalog_relations:
        parts = _parts(rel)
        if len(parts) >= 2:
            derived.add(parts[-2])
    return derived or None


__all__ = ["GuardResult", "guard_sql"]

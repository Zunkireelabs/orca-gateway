"""P2 brief D2/A1b: prod and stage share one Supabase project but must never share tenant
config, kill switches or call records -- so they use different schemas (`orca_gw` for stage,
`orca_gw_prod` for prod), set by `ORCA_DB_SCHEMA`.

Every repository (tenant_repo.py, calls_repo.py, reporting.py) and the migration runner write
`orca_gw.<table>` as a literal, deliberately -- see migrations/0001_tenants.sql: this gateway
never reads or writes anything outside its own schema, and that must stay explicit in every
query, never implicit via `search_path`. Rather than edit each of those ~60 call sites, the
literal is rewritten once, at the connection, via `connection_class()` below. When
`schema == "orca_gw"` (stage, the default) this is a no-op, so stage's queries -- and their
`EXPLAIN` plans -- are byte-for-byte what they were before this module existed.
"""

from __future__ import annotations

import re

import psycopg

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_ORCA_GW_RE = re.compile(r"\borca_gw\b")


def validate_schema(schema: str) -> str:
    """A plain lowercase identifier only -- this is interpolated into SQL text (there is no
    parameterised-identifier syntax for a schema name), so it is validated, never quoted."""
    if not _IDENT_RE.match(schema):
        raise ValueError(f"ORCA_DB_SCHEMA is not a plain lowercase identifier: {schema!r}")
    return schema


def rewrite(sql: str, schema: str) -> str:
    """Substitute the `orca_gw` schema literal for `schema` in a query or migration's SQL text.
    Word-bounded, so it never touches `orca_gw_prod` etc. -- only whole-word `orca_gw`."""
    return sql if schema == "orca_gw" else _ORCA_GW_RE.sub(schema, sql)


def connection_class(schema: str) -> type[psycopg.AsyncConnection]:
    """An `AsyncConnection` subclass whose `.execute()` rewrites the schema literal before
    sending the query. Returns the plain class unchanged for the default schema, so stage never
    runs through the subclass at all."""
    validate_schema(schema)
    if schema == "orca_gw":
        return psycopg.AsyncConnection

    class _SchemaConnection(psycopg.AsyncConnection):
        async def execute(self, query, *args, **kwargs):
            if isinstance(query, str):
                query = rewrite(query, schema)
            # psycopg's own AsyncConnection.execute() opens its cursor via self.cursor(), so the
            # override below must let that internal call through -- only a *direct* .cursor()
            # call from outside this method is refused.
            self._schema_cursor_ok = True
            try:
                return await super().execute(query, *args, **kwargs)
            finally:
                self._schema_cursor_ok = False

        def cursor(self, *args, **kwargs):
            # A cursor's own .execute() never goes through the override above, so a future
            # cursor-based query would send the literal `orca_gw.<table>` straight to prod's
            # `orca_gw` schema instead of `orca_gw_prod`. Refuse outright rather than let that
            # happen quietly -- callers use connection.execute(...) instead.
            if not getattr(self, "_schema_cursor_ok", False):
                raise NotImplementedError(
                    "connection_class(...).cursor() is refused: cursor-based queries bypass "
                    "this class's schema rewrite. Use connection.execute(...) instead."
                )
            return super().cursor(*args, **kwargs)

    return _SchemaConnection

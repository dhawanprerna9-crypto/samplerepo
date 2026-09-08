"""
repositories.py
---------------
Data access layer for the tool_gateway.

Three repository classes — one per aggregate root:
  - ServiceRepository      → agentbuilder.services
  - ToolRepository         → agentbuilder.tools
  - AuthProfileRepository  → agentbuilder.auth_profiles

Each repository takes a psycopg cursor in its constructor. The handler owns
the connection lifecycle (via src.db_utils._get_db_connection).

Schema name is read from llmConfig.ini [database].schema (defaults to
"agentbuilder"). Only rows with row_status_uid = ACTIVE_UID are returned.
"""

import logging
from configparser import ConfigParser
from pathlib import Path

logger = logging.getLogger("tool_gateway.repositories")

# ─────────────────────────────────────────────
# Configuration — schema name from llmConfig.ini
# ─────────────────────────────────────────────

_LLM_CFG_PATH = Path(__file__).resolve().parents[2] / "llmConfig.ini"
_cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
if _LLM_CFG_PATH.exists():
    _cfg.read(_LLM_CFG_PATH)
_SCHEMA = _cfg.get("database", "schema", fallback="agentbuilder") if _cfg.has_section("database") else "agentbuilder"

# Active row-status UUID (matches tool_gateway constants.STATUS_GUIDS["active"])
ACTIVE_UID = "00100000-0000-0000-0000-000000000000"


# ─────────────────────────────────────────────
# Row wrapper
# ─────────────────────────────────────────────

class ServiceRow:
    """Lightweight DTO for a row from the services table."""

    __slots__ = ("service_uid", "name", "endpoint", "transport", "config", "auth_profile_uid")

    def __init__(self, service_uid, name, endpoint, transport, config, auth_profile_uid):
        self.service_uid      = service_uid
        self.name             = name
        self.endpoint         = endpoint
        self.transport        = transport
        self.config           = config or {}
        self.auth_profile_uid = auth_profile_uid


# ─────────────────────────────────────────────
# Repositories
# ─────────────────────────────────────────────

class ServiceRepository:
    """Queries for the services aggregate."""

    _COLUMNS = '"service_uid", "name", "endpoint", "transport", "config", "auth_profile_uid"'

    def __init__(self, cursor):
        self._cursor = cursor

    def get_by_name(self, name: str) -> ServiceRow | None:
        if not name:
            return None
        self._cursor.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM "{_SCHEMA}"."services"
            WHERE LOWER("name") = LOWER(%s) AND "row_status_uid" = %s
            LIMIT 1
            """,
            (name, ACTIVE_UID),
        )
        row = self._cursor.fetchone()
        if row is None:
            return None
        return ServiceRow(*row)

    def get_all(self) -> list:
        self._cursor.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM "{_SCHEMA}"."services"
            WHERE "row_status_uid" = %s
            """,
            (ACTIVE_UID,),
        )
        return [ServiceRow(*row) for row in self._cursor.fetchall()]


class ToolRepository:
    """Queries for the tools aggregate."""

    def __init__(self, cursor):
        self._cursor = cursor

    def get_by_service(self, service_uid) -> list[dict]:
        """Returns tool dicts: tool_definition JSONB merged with the tool name."""
        self._cursor.execute(
            f"""
            SELECT "name", "tool_definition"
            FROM "{_SCHEMA}"."tools"
            WHERE "service_uid" = %s AND "row_status_uid" = %s
            """,
            (service_uid, ACTIVE_UID),
        )
        return [
            {"name": row[0], **(row[1] or {})}
            for row in self._cursor.fetchall()
        ]


class AuthProfileRepository:
    """Queries for the auth_profiles aggregate."""

    _COLUMNS = '"config", "auth_config", "is_secret_store_enabled"'

    def __init__(self, cursor):
        self._cursor = cursor

    def _row_to_dict(self, row) -> dict | None:
        if row is None:
            return None
        config, auth_config, is_secret_store_enabled = row
        return {
            **(config or {}),
            "is_secret_store_enabled": is_secret_store_enabled,
            "auth_config":             auth_config or {},
        }

    def get_by_name(self, name: str) -> dict | None:
        if not name:
            return None
        self._cursor.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM "{_SCHEMA}"."auth_profiles"
            WHERE LOWER("name") = LOWER(%s) AND "row_status_uid" = %s
            LIMIT 1
            """,
            (name, ACTIVE_UID),
        )
        return self._row_to_dict(self._cursor.fetchone())

    def get_by_uid(self, uid) -> dict | None:
        if uid is None:
            return None
        self._cursor.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM "{_SCHEMA}"."auth_profiles"
            WHERE "auth_profile_uid" = %s AND "row_status_uid" = %s
            LIMIT 1
            """,
            (uid, ACTIVE_UID),
        )
        return self._row_to_dict(self._cursor.fetchone())

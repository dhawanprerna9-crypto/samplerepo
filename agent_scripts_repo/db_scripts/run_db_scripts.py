"""
DB Script Runner — executes SQL migration scripts in order.
Tracks executed scripts in the ScriptList table to ensure idempotency.

Usage:
    python db_scripts/run_db_scripts.py

Reads database connection from ../llmConfig.ini [database] section.
"""

import json
import logging
import sys
from configparser import ConfigParser
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "llmConfig.ini"
SCRIPTS_DIR = SCRIPT_DIR / "scripts"
SCRIPTLIST_PATH = SCRIPT_DIR / "scriptlist.json"


def get_connection():
    """Create a psycopg connection from llmConfig.ini [database] section."""
    import psycopg

    config = ConfigParser(inline_comment_prefixes=("#", ";"))
    if not CONFIG_PATH.exists():
        logger.error(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)
    config.read(CONFIG_PATH)

    section = "database"
    if not config.has_section(section):
        logger.error("No [database] section in llmConfig.ini")
        sys.exit(1)

    host = config.get(section, "host", fallback="").strip()
    port = config.get(section, "port", fallback="5432").strip()
    name = config.get(section, "name", fallback="").strip()
    user = config.get(section, "user", fallback="").strip()
    password = config.get(section, "password", fallback="").strip()

    if not all([host, name, user, password]):
        logger.error("Incomplete [database] config in llmConfig.ini")
        sys.exit(1)

    conninfo = f"host={host} port={port} dbname={name} user={user} password={password} connect_timeout=10"
    logger.info(f"Connecting to database '{name}' on {host}:{port}")
    return psycopg.connect(conninfo, autocommit=False)


def get_schema():
    """Read the schema name from llmConfig.ini."""
    config = ConfigParser(inline_comment_prefixes=("#", ";"))
    config.read(CONFIG_PATH)
    return config.get("database", "schema", fallback="agentbuilder")


def get_database_name():
    """Read the database name from llmConfig.ini."""
    config = ConfigParser(inline_comment_prefixes=("#", ";"))
    config.read(CONFIG_PATH)
    return config.get("database", "name", fallback="")


def resolve_sql_placeholders(sql_content: str, schema: str, db_name: str) -> str:
    """Replace {SCHEMA_NAME} and {DATABASE_NAME} placeholders in SQL with actual values."""
    sql_content = sql_content.replace("{SCHEMA_NAME}", schema)
    sql_content = sql_content.replace("{DATABASE_NAME}", db_name)
    return sql_content


def ensure_script_list_table(conn, schema: str):
    """Create the ScriptList tracking table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}";')
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS "{schema}"."ScriptList" (
                "Id"            SERIAL        NOT NULL,
                "ScriptName"    VARCHAR(255)  NOT NULL UNIQUE,
                "ExecutedBy"    VARCHAR(100)  NOT NULL DEFAULT 'System',
                "ExecutedOn"    TIMESTAMP     NOT NULL DEFAULT NOW(),
                CONSTRAINT "PK_ScriptList" PRIMARY KEY ("Id")
            );
        """)
    conn.commit()


def is_script_executed(conn, schema: str, script_name: str) -> bool:
    """Check if a script has already been executed."""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT 1 FROM "{schema}"."ScriptList" WHERE "ScriptName" = %s',
            (script_name,),
        )
        return cur.fetchone() is not None


def mark_script_executed(conn, schema: str, script_name: str):
    """Record that a script has been executed."""
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}"."ScriptList" ("ScriptName", "ExecutedBy") VALUES (%s, %s)',
            (script_name, "run_db_scripts.py"),
        )


def run_db_scripts():
    """Main entry point: execute all pending SQL scripts."""
    schema = get_schema()
    db_name = get_database_name()

    # Load script list (ordered)
    if not SCRIPTLIST_PATH.exists():
        logger.error(f"Script list not found: {SCRIPTLIST_PATH}")
        sys.exit(1)

    with open(SCRIPTLIST_PATH, "r", encoding="utf-8") as f:
        script_list = json.load(f)

    if not script_list:
        logger.info("No scripts to execute.")
        return

    conn = get_connection()
    try:
        ensure_script_list_table(conn, schema)

        executed_count = 0
        skipped_count = 0
        for script_name in script_list:
            if is_script_executed(conn, schema, script_name):
                logger.info(f"  SKIP (already executed): {script_name}")
                skipped_count += 1
                continue

            script_path = SCRIPTS_DIR / script_name
            if not script_path.exists():
                logger.warning(f"  NOT FOUND: {script_name} — skipping")
                continue

            logger.info(f"  EXECUTING: {script_name}")
            with open(script_path, "r", encoding="utf-8") as f:
                sql_content = f.read()

            # Resolve placeholders from config
            sql_content = resolve_sql_placeholders(sql_content, schema, db_name)

            try:
                with conn.cursor() as cur:
                    cur.execute(sql_content)
                mark_script_executed(conn, schema, script_name)
                conn.commit()
                executed_count += 1
                logger.info(f"  SUCCESS: {script_name}")
            except Exception as e:
                conn.rollback()
                logger.error(f"  FAILED: {script_name} — {e}")
                logger.error("Aborting remaining scripts.")
                sys.exit(1)

        logger.info(f"\nDone. Executed: {executed_count}, Skipped: {skipped_count}")

    finally:
        conn.close()


if __name__ == "__main__":
    run_db_scripts()

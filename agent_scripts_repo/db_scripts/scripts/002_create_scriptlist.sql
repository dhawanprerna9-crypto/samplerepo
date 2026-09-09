-- ============================================================
-- 002_create_scriptlist.sql
-- ScriptList tracking table for idempotent migrations
-- ============================================================

CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}"."ScriptList" (
    "Id"            SERIAL        NOT NULL,
    "ScriptName"    VARCHAR(255)  NOT NULL UNIQUE,
    "ExecutedBy"    VARCHAR(100)  NOT NULL DEFAULT 'System',
    "ExecutedOn"    TIMESTAMP     NOT NULL DEFAULT NOW(),

    CONSTRAINT "PK_ScriptList" PRIMARY KEY ("Id")
);

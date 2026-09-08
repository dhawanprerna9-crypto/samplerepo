-- ============================================================================
-- RAG Tool Gateway Schema
-- ============================================================================
-- Creates tables for tool_gateway RAG integration:
--   - auth_profiles: Authentication configurations (qpp-token, api-key)
--   - services: RAG service endpoints (Quasar, Vertex, etc.)
--   - tools: Tool definitions for each service
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS agentbuilder;

-- ---------------------------------------------------------------------------
-- 1. AUTH_PROFILES
-- ---------------------------------------------------------------------------
-- Stores authentication configurations for RAG services
-- Supports both secret store and inline auth_config storage

CREATE TABLE IF NOT EXISTS agentbuilder."auth_profiles" (
    auth_profile_uid          UUID    NOT NULL DEFAULT gen_random_uuid(),
    name                      TEXT    NOT NULL,
    config                    JSONB   NOT NULL,
    auth_config               JSONB   NULL,
    is_secret_store_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    row_status_uid            UUID    NOT NULL,

    CONSTRAINT pk_auth_profiles PRIMARY KEY (auth_profile_uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_profiles_name
    ON agentbuilder."auth_profiles" (lower(name));

COMMENT ON TABLE agentbuilder."auth_profiles" IS 'Authentication profiles for RAG services (qpp-token, api-key, etc.)';
COMMENT ON COLUMN agentbuilder."auth_profiles".config IS 'Type and injection config: {"type": "qpp-token|api-key", "inject": [...]}';
COMMENT ON COLUMN agentbuilder."auth_profiles".auth_config IS 'Inline credentials when is_secret_store_enabled=false';
COMMENT ON COLUMN agentbuilder."auth_profiles".is_secret_store_enabled IS 'If true, credentials fetched from secret store; if false, use auth_config';

-- ---------------------------------------------------------------------------
-- 2. SERVICES
-- ---------------------------------------------------------------------------
-- Defines RAG service endpoints and their transport protocols

CREATE TABLE IF NOT EXISTS agentbuilder."services" (
    service_uid       UUID    NOT NULL DEFAULT gen_random_uuid(),
    name              TEXT    NOT NULL,
    endpoint          TEXT    NOT NULL,
    transport         TEXT    NOT NULL DEFAULT 'api',
    config            JSONB,
    auth_profile_uid  UUID,
    row_status_uid    UUID    NOT NULL,

    CONSTRAINT pk_services            PRIMARY KEY (service_uid),
    CONSTRAINT fk_services_auth       FOREIGN KEY (auth_profile_uid)
                                          REFERENCES agentbuilder."auth_profiles" (auth_profile_uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_services_name
    ON agentbuilder."services" (lower(name));

COMMENT ON TABLE agentbuilder."services" IS 'RAG service definitions (Quasar, Vertex, Backstage)';
COMMENT ON COLUMN agentbuilder."services".transport IS 'Transport protocol: "api" (REST/HTTP) or "streamable-http" (MCP)';
COMMENT ON COLUMN agentbuilder."services".config IS 'Service-specific configuration (project_id, description, etc.)';

-- ---------------------------------------------------------------------------
-- 3. TOOLS
-- ---------------------------------------------------------------------------
-- Tool definitions for API-based services (transport=api)
-- MCP services discover tools dynamically at runtime

CREATE TABLE IF NOT EXISTS agentbuilder."tools" (
    tool_uid          UUID    NOT NULL DEFAULT gen_random_uuid(),
    name              TEXT    NOT NULL,
    tool_definition   JSONB   NOT NULL,
    service_uid       UUID    NOT NULL,
    row_status_uid    UUID    NOT NULL,

    CONSTRAINT pk_tools               PRIMARY KEY (tool_uid),
    CONSTRAINT fk_tools_service       FOREIGN KEY (service_uid)
                                          REFERENCES agentbuilder."services" (service_uid)
);

CREATE INDEX IF NOT EXISTS idx_tools_service_uid
    ON agentbuilder."tools" (service_uid);

COMMENT ON TABLE agentbuilder."tools" IS 'Tool definitions for API-based RAG services (not needed for MCP services)';
COMMENT ON COLUMN agentbuilder."tools".tool_definition IS 'Full tool spec: description, path, method, bodyTemplate, inputSchema, instructions';

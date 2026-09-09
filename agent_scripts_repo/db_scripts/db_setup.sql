-- ============================================================
-- Master DB Setup Script for LiteLLM Model Configuration
-- Run this against your PostgreSQL database to create all
-- required objects for DB-based LLM routing.
--
-- Usage:
--   1. Create the database first (see Step 0 below)
--   2. Replace {DATABASE_NAME} and {SCHEMA_NAME} with your values
--   3. Connect to the target database and run this script:
--      psql -U <user> -d <your_database> -f db_scripts/db_setup.sql
--
-- Alternatively, use the script runner (reads from llmConfig.ini automatically):
--   python db_scripts/run_db_scripts.py
-- ============================================================


-- ============================================================
-- Step 0: Database Creation (run separately against 'postgres' db)
-- Uncomment and execute manually if the database doesn't exist:
-- ============================================================
-- CREATE DATABASE "{DATABASE_NAME}"
--     WITH OWNER = postgres
--          ENCODING = 'UTF8'
--          LC_COLLATE = 'en_US.UTF-8'
--          LC_CTYPE = 'en_US.UTF-8'
--          TEMPLATE = template0;


-- ============================================================
-- Step 1: Schema Creation
-- ============================================================
CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}";


-- ============================================================
-- Step 2: ScriptList tracking table (for idempotent migrations)
-- ============================================================
CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}"."ScriptList" (
    "Id"            SERIAL        NOT NULL,
    "ScriptName"    VARCHAR(255)  NOT NULL UNIQUE,
    "ExecutedBy"    VARCHAR(100)  NOT NULL DEFAULT 'System',
    "ExecutedOn"    TIMESTAMP     NOT NULL DEFAULT NOW(),

    CONSTRAINT "PK_ScriptList" PRIMARY KEY ("Id")
);


-- ============================================================
-- Step 3: AIModelConfig — Agent-to-model routing table
-- ============================================================
CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}"."AIModelConfig" (
    "Id"          SERIAL         NOT NULL,
    "AgentName"   VARCHAR(150)   NOT NULL,
    "UseCase"     VARCHAR(100)   NOT NULL,
    "ModelName"   VARCHAR(100)   NOT NULL,
    "Priority"    INT            NOT NULL,
    "IsActive"    BOOLEAN        NOT NULL   DEFAULT TRUE,
    "CreatedOn"   TIMESTAMP      NOT NULL   DEFAULT NOW(),
    "UpdatedOn"   TIMESTAMP      NOT NULL   DEFAULT NOW(),

    CONSTRAINT "PK_AIModelConfig"
        PRIMARY KEY ("Id"),

    CONSTRAINT "UQ_AIModelConfig_AgentUseCase_Priority"
        UNIQUE ("AgentName", "UseCase", "Priority")
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS "IX_AIModelConfig_AgentName_UseCase"
    ON "{SCHEMA_NAME}"."AIModelConfig" ("AgentName", "UseCase")
    WHERE "IsActive" = TRUE;

CREATE OR REPLACE FUNCTION "{SCHEMA_NAME}"."fn_AIModelConfig_UpdatedOn"()
RETURNS TRIGGER LANGUAGE plpgsql AS $
BEGIN
    NEW."UpdatedOn" = NOW();
    RETURN NEW;
END;
$;

DROP TRIGGER IF EXISTS "trg_AIModelConfig_UpdatedOn" ON "{SCHEMA_NAME}"."AIModelConfig";
CREATE TRIGGER "trg_AIModelConfig_UpdatedOn"
    BEFORE UPDATE ON "{SCHEMA_NAME}"."AIModelConfig"
    FOR EACH ROW
    EXECUTE FUNCTION "{SCHEMA_NAME}"."fn_AIModelConfig_UpdatedOn"();

-- Default model routing (all agents use gpt-4.1 as primary)
INSERT INTO "{SCHEMA_NAME}"."AIModelConfig"
    ("AgentName", "UseCase", "ModelName", "Priority")
VALUES
    ('*', 'default', 'gpt-4.1', 1)
ON CONFLICT ("AgentName", "UseCase", "Priority") DO NOTHING;


-- ============================================================
-- Step 4: AIModelDetails — Model endpoint & secret store config
-- ============================================================
CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}"."AIModelDetails" (
    "Id"             SERIAL        NOT NULL,
    "ModelName"      VARCHAR(100)  NOT NULL,
    "Provider"       VARCHAR(50)   NOT NULL,
    "DeploymentName" VARCHAR(150)  NULL,
    "Endpoint"       VARCHAR(500)  NULL,
    "ApiVersion"     VARCHAR(50)   NULL,
    "Region"         VARCHAR(100)  NULL,
    "ApiKey"         VARCHAR(500)  NULL,
    "SecretStoreType" VARCHAR(50)  NOT NULL    DEFAULT 'DirectDBConfig',
    "SecretStoreConfig" JSONB      NULL,
    "IsActive"       BOOLEAN       NOT NULL    DEFAULT TRUE,
    "CreatedOn"      TIMESTAMP     NOT NULL    DEFAULT NOW(),
    "UpdatedOn"      TIMESTAMP     NOT NULL    DEFAULT NOW(),

    CONSTRAINT "PK_AIModelDetails"
        PRIMARY KEY ("Id")
);

CREATE INDEX IF NOT EXISTS "IX_AIModelDetails_ModelName"
    ON "{SCHEMA_NAME}"."AIModelDetails" ("ModelName")
    WHERE "IsActive" = TRUE;

CREATE OR REPLACE FUNCTION "{SCHEMA_NAME}"."fn_AIModelDetails_UpdatedOn"()
RETURNS TRIGGER LANGUAGE plpgsql AS $
BEGIN
    NEW."UpdatedOn" = NOW();
    RETURN NEW;
END;
$;

DROP TRIGGER IF EXISTS "trg_AIModelDetails_UpdatedOn" ON "{SCHEMA_NAME}"."AIModelDetails";
CREATE TRIGGER "trg_AIModelDetails_UpdatedOn"
    BEFORE UPDATE ON "{SCHEMA_NAME}"."AIModelDetails"
    FOR EACH ROW
    EXECUTE FUNCTION "{SCHEMA_NAME}"."fn_AIModelDetails_UpdatedOn"();


-- ============================================================
-- Sample data (uncomment and modify as needed)
-- ============================================================
-- Direct DB Config (API key stored in table):
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "ApiKey", "SecretStoreType")
-- VALUES
--     ('gpt-4.1', 'azureopenai', 'gpt-4.1', 'https://your-endpoint.openai.azure.com/', '2024-08-01-preview', 'your-api-key', 'DirectDBConfig');
--
-- AWS Secrets Manager:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gpt-4.1', 'azureopenai', 'gpt-4.1', 'https://your-endpoint.openai.azure.com/', '2024-08-01-preview',
--      'awssecretsmanager',
--      '{"AWSSecretStore": {"AWSRegion": "us-east-1", "AWSSecretName": "your-secret-name", "AWSVersion": "AWSCURRENT"}}');
--
-- Azure Key Vault:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gpt-4.1', 'azureopenai', 'gpt-4.1', 'https://your-endpoint.openai.azure.com/', '2024-08-01-preview',
--      'azurekeyvault',
--      '{"AzureSecretStore": {"AzureKeyVaultUri": "https://your-vault.vault.azure.net/", "AzureUserAssignedManagedIdentity": ""}}');
--
-- GCP Secret Manager:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gemini-2.5-flash', 'gcpvertex', 'gemini-2.5-flash', 'us-central1', NULL,
--      'gcpsecretmanager',
--      '{"GCPSecretStore": {"ProjectId": "your-project-id", "GCPSecretName": "your-secret-name", "VersionId": "latest"}}');

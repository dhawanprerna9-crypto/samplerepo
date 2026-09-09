-- ============================================================
-- 004_AIModelDetails.sql
-- Model endpoint & secret store configuration table
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

-- AWS Secrets Manager:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gpt-4.1', 'azureopenai', 'gpt-4.1', 'https://your-endpoint.openai.azure.com/', '2024-08-01-preview',
--      'awssecretsmanager',
--      '{"AWSSecretStore": {"AWSRegion": "us-east-1", "AWSSecretName": "your-secret-name", "AWSVersion": "AWSCURRENT"}}');

-- Azure Key Vault:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gpt-4.1', 'azureopenai', 'gpt-4.1', 'https://your-endpoint.openai.azure.com/', '2024-08-01-preview',
--      'azurekeyvault',
--      '{"AzureSecretStore": {"AzureKeyVaultUri": "https://your-vault.vault.azure.net/", "AzureUserAssignedManagedIdentity": ""}}');

-- GCP Secret Manager:
-- INSERT INTO "{SCHEMA_NAME}"."AIModelDetails"
--     ("ModelName", "Provider", "DeploymentName", "Endpoint", "ApiVersion", "SecretStoreType", "SecretStoreConfig")
-- VALUES
--     ('gemini-2.5-flash', 'gcpvertex', 'gemini-2.5-flash', 'us-central1', NULL,
--      'gcpsecretmanager',
--      '{"GCPSecretStore": {"ProjectId": "your-project-id", "GCPSecretName": "your-secret-name", "VersionId": "latest"}}');

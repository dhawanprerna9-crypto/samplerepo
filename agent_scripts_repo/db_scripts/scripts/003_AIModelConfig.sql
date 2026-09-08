-- ============================================================
-- 003_AIModelConfig.sql
-- Agent-to-model routing table
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
);

CREATE INDEX IF NOT EXISTS "IX_AIModelConfig_AgentName_UseCase"
    ON "{SCHEMA_NAME}"."AIModelConfig" ("AgentName", "UseCase")
    WHERE "IsActive" = TRUE;

CREATE OR REPLACE FUNCTION "{SCHEMA_NAME}"."fn_AIModelConfig_UpdatedOn"()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW."UpdatedOn" = NOW();
    RETURN NEW;
END;
$$;

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

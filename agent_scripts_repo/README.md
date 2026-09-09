# BusinessSpecificationGenerationOrchestrator Agent Project

This project was generated from an architecture JSON file.

## Run

```bash
pip install -r requirements.txt
python main.py --cli --prompt "Summarize current project status."
```

## Configuration

Agents can talk to the LLM either directly through **Azure AI Foundry** (default) or through the
**LiteLLM proxy**. The switch is the `use_litellm` flag in `llmConfig.ini`.

### Azure AI Foundry (default — `use_litellm = false`)

Edit `config.ini` before running:

```ini
[azure]
project_endpoint = https://your-resource.services.ai.azure.com/api/projects/your-project
model_deployment_name = gpt-5.4
```

### Deployment target (Foundry adapter vs ACA MCP/A2A)

Choose the server mode used by `main.py`:

```ini
[deployment]
targetDeployment = foundry
```

- `foundry` starts `agent_framework_foundry_hosting.ResponsesHostServer`
- `aca` starts the local MCP -> A2A -> Agent Framework server in `src/hosting/mcp_a2a_server.py`

### LiteLLM proxy (`use_litellm = true`)

The LiteLLM proxy is OpenAI-compatible, so agents route through it via an OpenAI-style client.
Enable it in `llmConfig.ini`:

```ini
[appsettings]
use_litellm = true

[litellm_gateway]
proxy_url = https://your-litellm-proxy/host
key       = sk-your-litellm-proxy-key
model     = gpt-4.1
```

- `proxy_url` — base URL of the LiteLLM proxy. If your proxy only serves OpenAI routes under
  `/v1`, include that suffix (the client POSTs to `{proxy_url}/chat/completions`).
- `key` — the LiteLLM proxy API key.
- `model` — the model/alias the proxy should route to.

Each value can also be supplied via environment variables (which take precedence):
`USE_LITELLM`, `LITELLM_PROXY_URL`, `LITELLM_API_KEY`, `LITELLM_MODEL`. When `use_litellm = true`,
the Azure Foundry `[azure]` endpoint/deployment settings are not required.

## Architecture

- Level 1: Supervisor manager agent delegates to level-2 super agents via `as_tool`
- Level 2: Super agents delegate to level-3 utility agents via `as_tool`
- Level 3: Utility agents execute concrete local tools

**RAG Agent Integration:**
If a RAG agent is generated, it's automatically integrated into the Level 1 supervisor as a delegatable tool. The supervisor can invoke it using `ask_{rag_agent_name}(query="...")` to perform knowledge retrieval from the configured backend (Quasar, Vertex, Azure Foundry, or Backstage).

## Generated Components

- Super agents: 3
- Utility agents: 9
- Tools: 27

## Database Setup

Run the database scripts to set up required tables:

```bash
python db_scripts/run_db_scripts.py
```

### RAG Tool Gateway Database (Scripts 005-006)

If this project includes RAG agents, the database setup includes:

- **005_rag_schema.sql** - Creates `agentbuilder.auth_profiles`, `agentbuilder.services`, and `agentbuilder.tools` tables
- **006_rag_sample_data.sql** - Populates sample data for Quasar, Vertex, Azure Foundry, and Backstage RAG services

**Before deploying**, update credentials in `db_scripts/scripts/006_rag_sample_data.sql`:

**Quasar (QPP) Service:**
- `token_url`, `username`, `password` in auth profile
- `endpoint` URL in service entry

**Vertex Service:**
- `api_key` in auth profile
- `endpoint` URL and `project_id` in service entry

**Azure Foundry Service:**
- `token_scope` in auth profile (`https://cognitiveservices.azure.com/.default`)
- `endpoint` URL in service entry
- optional `tenant_id`, `client_id`, `client_secret`, or `user_assigned_mi_client_id` in auth profile

**Backstage MCP Service:**
- `mcp_api_key` (Bearer token) in auth profile
- `mcp_server_name` (MCP server identifier) in auth profile and service config
- `mcp_url` (MCP gateway endpoint) in auth profile
- `endpoint` URL in service entry (should match mcp_url)

### How RAG Tool Gateway Works

When a RAG agent calls `invoke_tool_gateway(service_name, user_query)`:

1. **Service Lookup** - Queries `agentbuilder.services` by name (qpp, vertex, azure_foundry, or backstage)
2. **Authentication** - Resolves credentials from `agentbuilder.auth_profiles`
   - **QPP:** POST to token endpoint with username/password → JWT token
   - **Vertex:** Static API key → x-api-key header
   - **Azure Foundry:** Entra token → Authorization Bearer header
   - **Backstage:** Bearer token → x-litellm-api-key header + x-mcp-servers header
3. **Tool Discovery** - Loads tools from `agentbuilder.tools` (API transport) or discovers from endpoint (MCP transport)
4. **LLM Routing** - Internal LLM selects best tool and generates arguments
5. **Execution** - Calls the selected tool with resolved authentication

**Transport Types:**
- `api` - Template-based HTTP (QPP/Quasar)
- `streamable-http` - MCP Streamable HTTP (Vertex, Azure Foundry, Backstage) with dynamic tool discovery

For production deployment, enable secret store:
1. Store credentials in Azure/AWS/GCP secret manager
2. Update `is_secret_store_enabled` to `TRUE` in auth profiles
3. Remove sensitive data from `auth_config` column

**Secret Store Layout Example (Backstage):**
```
backstage-1-endpoint   → https://your-mcp-gateway.example.com
backstage-1-mcpurl     → https://your-mcp-gateway.example.com/mcp/
backstage-1-servername → your_backstage_server_name
backstage-1-apikey     → sk-your-actual-api-key
```

**Secret Store Layout Example (Azure Foundry):**
```
azfoundry-1-endpoint      → https://your-foundry-mcp-gateway.example.com/mcp
azfoundry-1-tokenscope    → https://cognitiveservices.azure.com/.default
azfoundry-1-tenantid      → 11111111-1111-1111-1111-111111111111   (optional)
azfoundry-1-clientid      → 22222222-2222-2222-2222-222222222222   (optional)
azfoundry-1-clientsecret  → <service-principal-secret>              (optional)
azfoundry-1-uami-clientid → 33333333-3333-3333-3333-333333333333   (optional)
```

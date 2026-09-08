-- ============================================================================
-- RAG Tool Gateway Sample Data
-- ============================================================================
-- Sample configurations for Quasar, Vertex, and Backstage RAG services
-- 
-- IMPORTANT: Update credentials before deploying to production:
--   - Quasar: username, password, token_url, endpoint
--   - Vertex: api_key, endpoint, project_id
--   - Backstage: mcp_api_key, mcp_server_name, mcp_url, endpoint
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. AUTH_PROFILES
-- ---------------------------------------------------------------------------

-- Quasar Authentication (qpp-token strategy)
INSERT INTO agentbuilder."auth_profiles" (auth_profile_uid, name, config, auth_config, is_secret_store_enabled, row_status_uid)
VALUES (
    'aaaa0001-0000-0000-0000-000000000001',
    'quasar-auth',
    '{
        "type": "qpp-token",
        "inject": [
            { "location": "header", "name": "apiToken", "prefix": "" }
        ]
    }',
    '{
        "token_url": "https://your-quasar-instance.example.com/auth/token",
        "username": "your-username",
        "password": "your-password"
    }',
    FALSE,
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (auth_profile_uid) DO NOTHING;

-- Vertex Authentication (api-key strategy)
INSERT INTO agentbuilder."auth_profiles" (auth_profile_uid, name, config, auth_config, is_secret_store_enabled, row_status_uid)
VALUES (
    'aaaa0002-0000-0000-0000-000000000001',
    'vertex-auth',
    '{
        "type": "api-key",
        "inject": [
            { "location": "header", "name": "x-api-key", "prefix": "" }
        ]
    }',
    '{
        "api_key": "your-vertex-api-key-here"
    }',
    FALSE,
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (auth_profile_uid) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. SERVICES
-- ---------------------------------------------------------------------------

-- Quasar Service (API transport)
INSERT INTO agentbuilder."services" (service_uid, name, endpoint, transport, config, auth_profile_uid, row_status_uid)
VALUES (
    'bbbb0001-0000-0000-0000-000000000001',
    'qpp',
    'https://your-quasar-instance.example.com',
    'api',
    '{
        "description": "Quasar service for vector search, document search-and-generate, and document upload over indexed knowledge bases"
    }',
    'aaaa0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (service_uid) DO NOTHING;

-- Vertex RAG Service (MCP transport)
INSERT INTO agentbuilder."services" (service_uid, name, endpoint, transport, config, auth_profile_uid, row_status_uid)
VALUES (
    'bbbb0002-0000-0000-0000-000000000001',
    'vertex',
    'https://your-vertex-gateway.example.com/mcp',
    'streamable-http',
    '{
        "project_id": "your-gcp-project-id",
        "description": "Vertex RAG service for semantic search over enterprise knowledge corpus via MCP"
    }',
    'aaaa0002-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (service_uid) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. TOOLS (Quasar / api transport only)
-- ---------------------------------------------------------------------------
-- Note: Vertex (MCP transport) discovers tools dynamically at runtime

-- Tool 1: Quasar Search and Generate
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000001',
    'QuasarSearchAndGenerate',
    '{
        "description": "Using the OpenAISearchVectorGenerate API, filter documents for a subset of useful information and transform the extracted information into another format. For example, based on a list of incidents with an ID and a Description, generate a set of testcases. Supports advanced metadata and similarity filtering.",
        "path": "/api/v2/acnopenai/searchandgenerate",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "query":            "{{query}}",
            "config":           "{{config}}",
            "max_count":        "{{max_count}}",
            "chunk_size":       "{{chunk_size}}",
            "outpul_eval":      "{{outpul_eval}}",
            "primary_index":    "{{primary_index}}",
            "prompt_prefix":    "{{prompt_prefix}}",
            "prompt_suffix":    "{{prompt_suffix}}",
            "prompt_template":  "{{prompt_template}}",
            "secondary_index":  "{{secondary_index}}",
            "prompt_objective": "{{prompt_objective}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["query", "primary_index", "secondary_index", "prompt_objective", "max_count", "chunk_size"],
            "properties": {
                "query":            { "type": "string", "source": "user",    "description": "Plain text which needs to be looked at the Open AI document for explanation." },
                "config":           { "type": "string", "source": "user",    "description": "Specify the source of where to target the search. If not set, generic OpenAI information will be used." },
                "max_count":        { "type": "number", "source": "default", "default": 10,                  "description": "The maximum number of items/records to be generated e.g. 100 use cases." },
                "chunk_size":       { "type": "number", "source": "default", "default": 1,                   "description": "The number of calls in each LLM batch call." },
                "outpul_eval":      { "type": "string", "source": "user",    "description": "Desired response format for output data; use * to indicate no specific format is required." },
                "primary_index":    { "type": "string", "source": "user",    "default": "CrossIndustry_idx", "description": "Index name to search." },
                "prompt_prefix":    { "type": "string", "source": "user",    "description": "How Quasar++ should approach the task." },
                "prompt_suffix":    { "type": "string", "source": "user",    "description": "Additional information explaining how information could be extracted or inferred." },
                "prompt_template":  { "type": "string", "source": "user",    "description": "The structure or format of the ingested data inputs." },
                "secondary_index":  { "type": "string", "source": "user",    "default": "CrossIndustry_idx", "description": "Secondary index name to search." },
                "prompt_objective": { "type": "string", "source": "user",    "description": "High-level goal that focusses the search." }
            }
        },
        "instructions": "Use @metadata: to filter by metadata tags. Use @similarity: for semantic similarity search. Use @content: to get content from specific fields. Use @file_name: to filter by filename. Note: file_name and metadata cannot be used together in the same request."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 2: Quasar Vector Search
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000002',
    'QuasarSearchVector',
    '{
        "description": "Search for related information from a vector database using vector similarity. Returns vectors with the highest similarity scores to the query. Supports @metadata:, @similarity:, @content:, @file_name: filter prefixes.",
        "path": "/api/v2/acnopenai/searchvectors",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "index":           "{{index}}",
            "query":           "{{query}}",
            "output":          "{{output}}",
            "search_limit":    "{{search_limit}}",
            "search_criteria": "{{search_criteria}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["output", "query", "index", "search_limit"],
            "properties": {
                "index":           { "type": "string", "source": "default", "default": "CrossIndustry_idx", "description": "Index name to search. Deterministically defaulted when the caller/agent does not supply one; a generated agent injects its own dedicated index via tool_arg_overrides." },
                "query":           { "type": "string", "source": "user",    "description": "Plain text query. Supports @metadata:, @similarity:, @content:, @file_name: filter prefixes." },
                "output":          { "type": "string", "source": "user",    "default": "*",                 "description": "Desired response format; use * for no specific format." },
                "search_limit":    { "type": "number", "source": "default", "default": 10000,               "description": "Maximum number of vector results to return." },
                "search_criteria": { "type": "array",  "items": { "type": "string", "enum": ["knn", "keyword", "graph"] }, "source": "default", "default": ["knn", "keyword"], "description": "Retrieval strategy: knn=vector nearest-neighbour, keyword=lexical, graph=knowledge-graph. May be combined; defaults to knn+keyword." }
            }
        },
        "instructions": "Use @metadata: to filter by metadata tags. Use @similarity: for semantic similarity search. Use @content: to get content from specific fields. Use @file_name: to filter by filename. Note: file_name and metadata cannot be used together in the same request."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 3: Quasar Document Upload
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000003',
    'quasar_upload_document',
    '{
        "description": "Upload a document file to a specified Quasar index for ingestion and vectorisation.",
        "path": "/api/v2/acnopenai/upload",
        "method": "POST",
        "bodyType": "multipart",
        "headers": {},
        "params": {},
        "bodyTemplate": {
            "index": "{{index}}"
        },
        "fileFields": ["file"],
        "inputSchema": {
            "type": "object",
            "required": ["index", "file"],
            "properties": {
                "file":  { "type": "string", "format": "binary", "source": "file", "description": "Document file to upload. Pass via the files parameter of invoke_tool_gateway()." },
                "index": { "type": "string", "source": "user",                     "description": "Index name where the document will be stored." }
            }
        }
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 4: Quasar Chat Completion (Stage 1 of two-stage RAG flow)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000004',
    'QuasarChatCompletion',
    '{
        "description": "Ask QPP''s own LLM a question against an indexed corpus. Returns a refined natural-language answer in the ''content'' field that can be used as the similarity query in a subsequent QuasarSearchVector call. Stage 1 of the two-stage RAG flow (ChatCompletion then SearchVector).",
        "path": "/api/v2/acnopenai/chatcompletion",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "prompt":          "{{prompt}}",
            "index":           "{{index}}",
            "reset_context":   "{{reset_context}}",
            "search_criteria": "{{search_criteria}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["prompt", "index"],
            "properties": {
                "prompt":          { "type": "string", "source": "user",    "description": "Natural-language question or passage to reason over." },
                "index":           { "type": "string", "source": "default", "default": "CrossIndustry_idx", "description": "Index to search when generating the response. A generated agent injects its own dedicated index via tool_arg_overrides." },
                "reset_context":   { "type": "string", "source": "default", "default": "True",              "description": "Reset conversation context on each call for stateless retrieval." },
                "search_criteria": { "type": "array",  "items": { "type": "string", "enum": ["knn", "keyword", "graph"] }, "source": "default", "default": ["knn", "keyword"], "description": "Retrieval strategy used internally by QPP when generating the response." }
            }
        },
        "instructions": "Stage 1 of the two-stage RAG flow. Call with the user query as prompt; extract content from the response and use it as the query for a subsequent QuasarSearchVector call."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 5: Quasar List Docs (list documents indexed under an index)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000005',
    'QuasarListDocs',
    '{
        "description": "List the documents that have been indexed under a given Quasar index. Returns an array of {doc_name, ...}.",
        "path": "/api/v2/acnopenai/listdocs/{{index}}",
        "method": "GET",
        "bodyType": "queryParams",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {},
        "inputSchema": {
            "type": "object",
            "required": ["index"],
            "properties": {
                "index": { "type": "string", "source": "user", "description": "Index name whose documents should be listed. Supplied via tool_arg_overrides by a generated agent." }
            }
        },
        "instructions": "Index is passed as a path segment. Use to confirm a document is present in an index before/after upload."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 6: Quasar Upload Status (poll async upload completion by TaskID)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000006',
    'QuasarUploadStatus',
    '{
        "description": "Check the status of an asynchronous document upload by its TaskID. Returns the formatted status (Completed, Started, etc.).",
        "path": "/api/v2/acnopenai/uploadstatus/taskid/{{task_id}}/formattedjson",
        "method": "GET",
        "bodyType": "queryParams",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": { "type": "string", "source": "user", "description": "TaskID returned by the upload call. Supplied via tool_arg_overrides." }
            }
        },
        "instructions": "TaskID is passed as a path segment. Poll until Status is Completed before running vector/graph steps against a freshly uploaded document."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 7: Quasar KG Workspace List (detect whether a knowledge graph exists)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000007',
    'QuasarKGWorkspaceList',
    '{
        "description": "List knowledge-graph workspaces for an index. Used to detect whether a SAVED graph already exists (enabling graph search criteria).",
        "path": "/api/v2/acnopenai/knowledgegraph/workspace/list",
        "method": "GET",
        "bodyType": "queryParams",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "index_name": "{{index_name}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["index_name"],
            "properties": {
                "index_name": { "type": "string", "source": "user", "description": "Index whose graph workspaces should be listed. Supplied via tool_arg_overrides." }
            }
        },
        "instructions": "Returns {workspaces: [{workspace_id, status, node_count, ...}]}. A workspace with status SAVED means graph search is available."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 8: Quasar KG Create Workspace
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000008',
    'QuasarKGCreateWorkspace',
    '{
        "description": "Create a knowledge-graph workspace for an index (step 1 of the create-graph flow).",
        "path": "/api/v2/acnopenai/knowledgegraph/workspace",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "workspace_name":      "{{workspace_name}}",
            "index_name":          "{{index_name}}",
            "selected_categories": "{{selected_categories}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["index_name"],
            "properties": {
                "workspace_name":      { "type": "string", "source": "user",    "description": "Workspace name. Supplied via tool_arg_overrides (e.g. adlc_ws_<index>)." },
                "index_name":          { "type": "string", "source": "user",    "description": "Index the workspace is built over." },
                "selected_categories": { "type": "array",  "items": { "type": "string" }, "source": "default", "default": ["structural", "business"], "description": "Entity categories to extract." }
            }
        },
        "instructions": "Returns {workspace_id}. Follow with QuasarKGBuild, then poll QuasarKGWorkspaceList until STAGED/SAVED, then QuasarKGSave."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 9: Quasar KG Build (extract entities into the workspace)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000009',
    'QuasarKGBuild',
    '{
        "description": "Trigger asynchronous entity extraction (graph build) for a workspace (step 2 of the create-graph flow).",
        "path": "/api/v2/acnopenai/knowledgegraph/build",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "workspace_id": "{{workspace_id}}",
            "index_name":   "{{index_name}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["workspace_id", "index_name"],
            "properties": {
                "workspace_id": { "type": "string", "source": "user", "description": "Workspace to build. Supplied via tool_arg_overrides." },
                "index_name":   { "type": "string", "source": "user", "description": "Index the workspace is built over." }
            }
        },
        "instructions": "Returns {task_id}. Build is async; poll QuasarKGWorkspaceList until status is STAGED/SAVED."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 10: Quasar KG Staged (review staged entities before save)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000010',
    'QuasarKGStaged',
    '{
        "description": "Fetch the staged (built but not yet committed) knowledge-graph entities for a build task.",
        "path": "/api/v2/acnopenai/knowledgegraph/staged",
        "method": "GET",
        "bodyType": "queryParams",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "task_id":      "{{task_id}}",
            "workspace_id": "{{workspace_id}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id":      { "type": "string", "source": "user", "description": "Build task_id from QuasarKGBuild. Supplied via tool_arg_overrides." },
                "workspace_id": { "type": "string", "source": "user", "description": "Workspace whose staged graph is reviewed." }
            }
        },
        "instructions": "Returns {nodes, summary}. Zero nodes with zero token_usage means extraction produced nothing; skip save and fall back to knn+keyword."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- Tool 11: Quasar KG Save (commit STAGED -> SAVED)
INSERT INTO agentbuilder."tools" (tool_uid, name, tool_definition, service_uid, row_status_uid)
VALUES (
    'cccc0001-0000-0000-0000-000000000011',
    'QuasarKGSave',
    '{
        "description": "Commit a staged knowledge graph (STAGED -> SAVED) so graph search becomes available for the index (final step of the create-graph flow).",
        "path": "/api/v2/acnopenai/knowledgegraph/save",
        "method": "POST",
        "bodyType": "json",
        "headers": {
            "Content-Type": "application/json"
        },
        "params": {},
        "bodyTemplate": {
            "workspace_id": "{{workspace_id}}",
            "index_name":   "{{index_name}}"
        },
        "inputSchema": {
            "type": "object",
            "required": ["workspace_id", "index_name"],
            "properties": {
                "workspace_id": { "type": "string", "source": "user", "description": "Workspace to save. Supplied via tool_arg_overrides." },
                "index_name":   { "type": "string", "source": "user", "description": "Index the graph belongs to." }
            }
        },
        "instructions": "Returns {entities, structural_nodes}. Zero of both means nothing was extracted; graph search stays disabled."
    }',
    'bbbb0001-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (tool_uid) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. BACKSTAGE MCP SERVICE
-- ---------------------------------------------------------------------------

-- Backstage Authentication (backstage-mcp strategy)
INSERT INTO agentbuilder."auth_profiles" (auth_profile_uid, name, config, auth_config, is_secret_store_enabled, row_status_uid)
VALUES (
    'aaaa0003-0000-0000-0000-000000000001',
    'backstage-mcp-auth',
    '{
        "type": "backstage-mcp",
        "inject": [
            { "location": "header", "name": "x-litellm-api-key", "prefix": "Bearer " }
        ]
    }',
    '{
        "mcp_url": "https://your-backstage-mcp-gateway.example.com/mcp/",
        "mcp_server_name": "your_backstage_server_name",
        "mcp_api_key": "sk-dummy-backstage-api-key-12345"
    }',
    FALSE,
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (auth_profile_uid) DO NOTHING;

-- Backstage Service (MCP transport)
INSERT INTO agentbuilder."services" (service_uid, name, endpoint, transport, config, auth_profile_uid, row_status_uid)
VALUES (
    'bbbb0003-0000-0000-0000-000000000001',
    'backstage',
    'https://your-backstage-mcp-gateway.example.com/mcp/',
    'streamable-http',
    '{
        "mcp_server_name": "your_backstage_server_name",
        "description": "Backstage internal documentation and service catalog via MCP protocol for dynamic tool discovery"
    }',
    'aaaa0003-0000-0000-0000-000000000001',
    '00100000-0000-0000-0000-000000000000'
)
ON CONFLICT (service_uid) DO NOTHING;

-- Note: No tools table entries needed for Backstage - MCP discovers tools dynamically at runtime

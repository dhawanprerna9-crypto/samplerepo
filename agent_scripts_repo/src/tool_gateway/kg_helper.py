"""
kg_helper.py
------------
Knowledge-graph resolution for QPP-backed RAG agents.

Public entry point: ``resolve_graph_criteria`` — decides the effective
``search_criteria`` for a QPP index by detecting (and optionally creating) a
knowledge graph. Every backend call goes through ``invoke_tool_gateway`` with
``preferred_tool_name`` for deterministic tool selection (no LLM routing).

Flow (mirrors the reference rag_agent _kg_ensure_graph, but gateway-routed):
  1. DETECT (always, cheap): one QuasarKGWorkspaceList call.
       - SAVED / COMPLETED workspace  -> graph search available.
       - STAGED workspace             -> save it, then available.
       - BUILDING / SCHEMA_GENERATED  -> (autocreate only) poll, then save.
  2. CREATE (only when ``autocreate`` is True and no graph exists):
       a. optionally upload the BRD from ``brd_dir`` when the index has no docs
       b. create workspace -> build -> poll until STAGED/SAVED -> save
  3. Return ``["graph","knn","keyword"]`` when a graph is usable, otherwise
     ``["knn","keyword"]``.

Guarantees (aligned with the RAG thumb-rules):
  - Best-effort and guarded: this module NEVER raises. Any failure returns the
    plain ``["knn","keyword"]`` criteria so RAG always proceeds.
  - Cached per index (process-wide) so the expensive detect/create path runs at
    most once per index per process — the per-query cost after warm-up is zero.
  - Only touches QPP; callers gate on service_name before calling.
"""

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("tool_gateway.kg_helper")

_DEFAULT_CRITERIA = ["knn", "keyword"]
_GRAPH_CRITERIA = ["graph", "knn", "keyword"]
_SELECTED_CATEGORIES = ["structural", "business"]

_SAVED_STATES = {"SAVED", "COMPLETED"}
_STAGED_STATES = {"STAGED"}
_BUILDING_STATES = {"BUILDING", "SCHEMA_GENERATED"}
_FAILED_STATES = {"FAILED", "ERROR"}

# Poll windows for the (opt-in) create path. Kept modest so a stuck build can
# never hang a request indefinitely; on timeout we fall back to knn+keyword.
_BUILD_MAX_WAIT = 300
_BUILD_POLL_INTERVAL = 30
_UPLOAD_MAX_WAIT = 600
_UPLOAD_POLL_INTERVAL = 60

# Document extensions worth uploading from the BRD directory.
_BRD_EXTENSIONS = (".docx", ".doc", ".pdf", ".txt", ".md")

# Process-wide cache: index -> resolved criteria list. Guards against re-running
# the detect/create flow on every query. An asyncio.Lock serialises concurrent
# first-time resolution for the same index.
_criteria_cache: dict = {}
_cache_lock = asyncio.Lock()


def is_qpp(service_name: str) -> bool:
    """True when the backend is QPP (the only backend with a graph API)."""
    return str(service_name or "").lower().startswith("qpp")


async def _gw(service_name, agent_name, preferred_tool_name, overrides, files=None):
    """Single deterministic gateway call. Returns the response dict or None on error."""
    from src.tool_gateway.handler import invoke_tool_gateway
    try:
        return await invoke_tool_gateway(
            user_query=f"knowledge graph operation: {preferred_tool_name}",
            service_name=service_name,
            agent_name=agent_name,
            files=files,
            tool_arg_overrides=overrides or None,
            preferred_tool_name=preferred_tool_name,
        )
    except Exception as exc:
        logger.warning("[KG] gateway call %s failed (non-fatal): %s", preferred_tool_name, exc)
        return None


def _workspaces(resp) -> list:
    return resp.get("workspaces", []) if isinstance(resp, dict) else []


def _ws_id(ws: dict):
    return ws.get("workspace_id") or ws.get("id")


def _pick(workspaces, states):
    return next((ws for ws in workspaces if ws.get("status") in states), None)


async def _save(service_name, agent_name, workspace_id, index):
    """Commit STAGED -> SAVED. Returns True on success."""
    resp = await _gw(service_name, agent_name, "QuasarKGSave",
                     {"workspace_id": workspace_id, "index_name": index})
    if resp is None:
        return False
    entities = resp.get("entities", 0) if isinstance(resp, dict) else 0
    structural = resp.get("structural_nodes", 0) if isinstance(resp, dict) else 0
    if entities == 0 and structural == 0:
        logger.warning("[KG] save produced 0 entities/0 structural_nodes for index=%s; "
                       "graph search DISABLED.", index)
        return False
    logger.info("[KG] graph SAVED for index=%s | entities=%s structural=%s",
                index, entities, structural)
    return True


async def _poll_until_ready(service_name, agent_name, workspace_id, index):
    """Poll workspace status until STAGED/SAVED/COMPLETED, FAILED, or timeout.
       Returns the terminal status string (or 'TIMEOUT')."""
    elapsed = 0
    while elapsed < _BUILD_MAX_WAIT:
        resp = await _gw(service_name, agent_name, "QuasarKGWorkspaceList",
                         {"index_name": index})
        for ws in _workspaces(resp):
            if _ws_id(ws) == workspace_id:
                status = ws.get("status", "UNKNOWN")
                logger.info("[KG] poll %ss | ws=%s | status=%s", elapsed, workspace_id, status)
                if status in _SAVED_STATES or status in _STAGED_STATES:
                    return status
                if status in _FAILED_STATES:
                    return status
        await asyncio.sleep(_BUILD_POLL_INTERVAL)
        elapsed += _BUILD_POLL_INTERVAL
    logger.warning("[KG] poll timed out after %ss for ws=%s", _BUILD_MAX_WAIT, workspace_id)
    return "TIMEOUT"


def _brd_files(brd_dir):
    """Return [{'path','filename'}] for uploadable BRD docs in brd_dir, or []."""
    try:
        if not brd_dir:
            return []
        d = Path(brd_dir)
        if not d.is_dir():
            logger.info("[KG] BRD dir not found: %s", brd_dir)
            return []
        found = [
            {"path": str(p), "filename": p.name}
            for p in sorted(d.iterdir())
            if p.is_file() and p.suffix.lower() in _BRD_EXTENSIONS
        ]
        logger.info("[KG] BRD dir %s -> %d uploadable file(s)", brd_dir, len(found))
        return found
    except Exception as exc:
        logger.warning("[KG] BRD dir scan failed (non-fatal): %s", exc)
        return []


async def _ensure_brd_in_index(service_name, agent_name, index, brd_dir):
    """When the index has no docs, upload BRD files from brd_dir. Best-effort."""
    docs = await _gw(service_name, agent_name, "QuasarListDocs", {"index": index})
    has_docs = isinstance(docs, list) and len(docs) > 0
    if has_docs:
        logger.info("[KG] index=%s already has %d doc(s); skipping BRD upload.", index, len(docs))
        return
    files = _brd_files(brd_dir)
    if not files:
        logger.info("[KG] index=%s empty and no BRD files to upload; graph build may find no content.", index)
        return
    for f in files:
        up = await _gw(service_name, agent_name, "quasar_upload_document",
                       {"index": index}, files=[f])
        task_id = None
        if isinstance(up, dict):
            task_id = up.get("TaskID") or up.get("task_id")
        logger.info("[KG] uploaded BRD %s to index=%s | task_id=%s", f["filename"], index, task_id)
        if task_id:
            await _poll_upload(service_name, agent_name, task_id)


async def _poll_upload(service_name, agent_name, task_id):
    """Poll QuasarUploadStatus until Completed or timeout. Best-effort."""
    elapsed = 0
    while elapsed < _UPLOAD_MAX_WAIT:
        resp = await _gw(service_name, agent_name, "QuasarUploadStatus", {"task_id": task_id})
        status = None
        if isinstance(resp, dict):
            results = resp.get("Status", {}).get("result", []) if isinstance(resp.get("Status"), dict) else []
            if results:
                status = results[0].get("Status")
        if status == "Completed":
            logger.info("[KG] upload task_id=%s Completed after %ss", task_id, elapsed)
            return
        await asyncio.sleep(_UPLOAD_POLL_INTERVAL)
        elapsed += _UPLOAD_POLL_INTERVAL
    logger.warning("[KG] upload task_id=%s not confirmed Completed after %ss", task_id, _UPLOAD_MAX_WAIT)


async def _create_graph(service_name, agent_name, index, brd_dir):
    """Full create flow: (BRD upload) -> create -> build -> poll -> save.
       Returns True when a graph ends up SAVED."""
    await _ensure_brd_in_index(service_name, agent_name, index, brd_dir)

    created = await _gw(service_name, agent_name, "QuasarKGCreateWorkspace",
                        {"workspace_name": f"adlc_ws_{index}", "index_name": index,
                         "selected_categories": _SELECTED_CATEGORIES})
    workspace_id = created.get("workspace_id") or created.get("id") if isinstance(created, dict) else None
    if not workspace_id:
        logger.warning("[KG] create workspace failed for index=%s; graph DISABLED.", index)
        return False

    built = await _gw(service_name, agent_name, "QuasarKGBuild",
                      {"workspace_id": workspace_id, "index_name": index})
    if built is None:
        logger.warning("[KG] build trigger failed for index=%s; graph DISABLED.", index)
        return False

    status = await _poll_until_ready(service_name, agent_name, workspace_id, index)
    if status in _SAVED_STATES:
        return True
    if status in _STAGED_STATES:
        return await _save(service_name, agent_name, workspace_id, index)
    logger.warning("[KG] build ended status=%s for index=%s; graph DISABLED.", status, index)
    return False


async def _resolve(service_name, index, brd_dir, autocreate, agent_name):
    """Uncached resolution. Returns a criteria list; never raises."""
    resp = await _gw(service_name, agent_name, "QuasarKGWorkspaceList", {"index_name": index})
    workspaces = _workspaces(resp)

    if _pick(workspaces, _SAVED_STATES):
        logger.info("[KG] SAVED graph found for index=%s; graph search ENABLED.", index)
        return list(_GRAPH_CRITERIA)

    staged = _pick(workspaces, _STAGED_STATES)
    if staged:
        logger.info("[KG] STAGED graph for index=%s; saving.", index)
        if await _save(service_name, agent_name, _ws_id(staged), index):
            return list(_GRAPH_CRITERIA)
        return list(_DEFAULT_CRITERIA)

    if not autocreate:
        logger.info("[KG] no graph for index=%s and autocreate disabled; using knn+keyword.", index)
        return list(_DEFAULT_CRITERIA)

    building = _pick(workspaces, _BUILDING_STATES)
    if building:
        logger.info("[KG] graph BUILDING for index=%s; waiting.", index)
        status = await _poll_until_ready(service_name, agent_name, _ws_id(building), index)
        if status in _SAVED_STATES:
            return list(_GRAPH_CRITERIA)
        if status in _STAGED_STATES and await _save(service_name, agent_name, _ws_id(building), index):
            return list(_GRAPH_CRITERIA)
        return list(_DEFAULT_CRITERIA)

    logger.info("[KG] no graph for index=%s; starting create flow.", index)
    if await _create_graph(service_name, agent_name, index, brd_dir):
        return list(_GRAPH_CRITERIA)
    return list(_DEFAULT_CRITERIA)


async def resolve_graph_criteria(service_name, index, *, brd_dir=None,
                                 autocreate=True, agent_name=None):
    """
    Resolve effective ``search_criteria`` for a QPP index.

    Args:
        service_name: RAG backend name; only QPP has a graph API.
        index:        QPP index to detect/build a graph for. Empty/None -> default.
        brd_dir:      Optional directory holding the BRD to upload when the index
                      is empty during the create flow (e.g.
                      ``{shared_folder}/{thread_id}/uploaded_files/brd``).
        autocreate:   When True (default), build the graph if none exists.
        agent_name:   Forwarded to the gateway for model routing.

    Returns:
        ``["graph","knn","keyword"]`` when a graph is usable, else
        ``["knn","keyword"]``. Never raises.
    """
    if not is_qpp(service_name) or not index:
        return list(_DEFAULT_CRITERIA)

    if index in _criteria_cache:
        return list(_criteria_cache[index])

    async with _cache_lock:
        # Re-check inside the lock in case a concurrent call resolved it first.
        if index in _criteria_cache:
            return list(_criteria_cache[index])
        try:
            criteria = await _resolve(service_name, index, brd_dir, autocreate, agent_name)
        except Exception as exc:
            logger.warning("[KG] resolve_graph_criteria failed for index=%s (non-fatal): %s", index, exc)
            criteria = list(_DEFAULT_CRITERIA)
        _criteria_cache[index] = list(criteria)
        return list(criteria)


def build_brd_dir(shared_folder, thread_id):
    """Compose the conventional BRD directory for a thread, or None if unknown.
       Convention: {shared_folder}/{thread_id}/uploaded_files/brd"""
    try:
        if not shared_folder or not thread_id:
            return None
        return os.path.join(str(shared_folder), str(thread_id), "uploaded_files", "brd")
    except Exception:
        return None

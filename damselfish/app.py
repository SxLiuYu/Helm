from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import AppConfig, load_config
from .git_sync import GitMemorySync
from .router import ModelRouter, NoTargetAvailable
from .pipeline import plan_collaboration, run_pipeline, sse_stream_from_text, CollabUnavailable
from .selector import infer_context, resolve_allowed_targets, RouteContext
from .store import Store, merge_messages, project_context_message
from .tokens import estimate_messages_tokens

log = logging.getLogger("damselfish")


def create_app(config: AppConfig | None = None, config_path: str | Path | None = None) -> FastAPI:
    loaded = config or load_config(config_path)
    # Path remembered so SIGHUP reloads re-read the same file even when the
    # app was constructed with an explicit ``config`` object (e.g. tests).
    _config_file = (
        Path(config_path).expanduser()
        if config_path
        else Path(os.environ.get("DAMSELFISH_CONFIG", "config.yml")).expanduser()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(loaded.database, [target.id for target in loaded.targets])
        # Prune old decision rows to prevent unbounded table growth.
        pruned = store.prune_decisions(keep=5000)
        if pruned:
            log.info("pruned %d old decision rows on startup", pruned)
        timeout = httpx.Timeout(
            loaded.routing.request_timeout_seconds,
            connect=loaded.routing.connect_timeout_seconds,
        )
        client = httpx.AsyncClient(timeout=timeout)
        router = ModelRouter(loaded, store, client)
        memory_sync = GitMemorySync(loaded.git_sync, store)
        await memory_sync.startup_sync()
        stop = asyncio.Event()
        probe_task = asyncio.create_task(router.probe_loop(stop))
        sync_task = asyncio.create_task(memory_sync.sync_loop(stop))
        app.state.config = loaded
        app.state.store = store
        app.state.router = router
        app.state.memory_sync = memory_sync
        app.state.started_at = time.time()

        async def reload_config() -> None:
            """Hot-reload config.yml without dropping in-flight streams."""
            try:
                fresh = load_config(_config_file)
            except Exception as error:
                log.error("SIGHUP config reload failed, keeping previous config: %s", error)
                return
            store.ensure_targets([target.id for target in fresh.targets])
            router.reconfigure(fresh)
            app.state.config = fresh
            log.info(
                "config reloaded (SIGHUP): %d targets (%d enabled)",
                len(fresh.targets),
                sum(1 for t in fresh.targets if t.enabled),
            )

        loop = asyncio.get_running_loop()

        def _on_sighup() -> None:
            loop.create_task(reload_config())

        try:
            loop.add_signal_handler(signal.SIGHUP, _on_sighup)
        except (NotImplementedError, AttributeError, RuntimeError):
            # NotImplementedError: platform without SIGHUP support;
            # RuntimeError: not the main thread (e.g. TestClient).
            pass
        try:
            yield
        finally:
            try:
                loop.remove_signal_handler(signal.SIGHUP)
            except (NotImplementedError, AttributeError, RuntimeError):
                pass
            stop.set()
            # Give in-flight requests up to 10 seconds to finish gracefully.
            try:
                await asyncio.wait_for(
                    asyncio.gather(probe_task, sync_task),
                    timeout=10.0,
                )
            except TimeoutError:
                log.warning("graceful shutdown timed out after 10s, forcing exit")
                probe_task.cancel()
                sync_task.cancel()
            try:
                await asyncio.wait_for(
                    memory_sync.sync_pending(force=True),
                    timeout=15.0,
                )
            except TimeoutError:
                log.warning("final memory sync timed out, will resume on next startup")
            await client.aclose()
            store.close()

    app = FastAPI(title="Damselfish", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        expected = os.environ.get("DAMSELFISH_API_KEY")
        if expected and request.url.path.startswith("/v1/"):
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {expected}"):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": "invalid Damselfish API key"}},
                )
        return await call_next(request)

    # ------------------------------------------------------------------ #
    # Dashboard — the only page
    # ------------------------------------------------------------------ #
    @app.get("/", include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        return HTMLResponse("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Damselfish Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#f5f7fa;color:#1a1a2e;padding:20px}
h1{color:#2b5cb8;margin-bottom:16px;font-size:22px}
.summary{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #e0e6ed;border-radius:10px;padding:14px 18px;min-width:120px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card .num{font-size:28px;font-weight:700;color:#2b5cb8}
.card .label{font-size:12px;color:#888;margin-top:2px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e0e6ed;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
th{background:#eef2f7;padding:10px 12px;text-align:left;font-size:12px;color:#5a6a7a;text-transform:uppercase}
td{padding:8px 12px;border-top:1px solid#eef2f7;font-size:13px}
.tr-ok{color:#2e7d32} .tr-fail{color:#c62828} .tr-na{color:#999}
.bar{height:4px;border-radius:2px;background:#eef2f7;margin-top:2px}
.bar-fill{height:100%;border-radius:2px;background:#2b5cb8}
</style></head><body>
<h1>🐟 Damselfish Dashboard</h1>
<div class="summary" id="summary"></div>
<table><thead><tr>
  <th>模型</th><th>状态</th><th>智能</th><th>优先级</th><th>免费</th><th>成功率</th>
  <th>Token 总量</th><th>延迟(ms)</th><th>用量条</th>
</tr></thead><tbody id="tbody"></tbody></table>
<script>
async function load(){
  const stats=await fetch('/stats').then(r=>r.json()).catch(()=>null);
  if(!stats){document.getElementById('tbody').innerHTML='<tr><td colspan=9>加载失败</td></tr>';return;}
  const ts=Object.values(stats.targets||{});
  const avail=ts.filter(t=>t.available).length;
  const totalReq=ts.reduce((s,t)=>s+t.requests,0);
  const totalTok=ts.reduce((s,t)=>s+t.total_tokens,0);
  document.getElementById('summary').innerHTML=`
    <div class="card"><div class="num">${avail}/${ts.length}</div><div class="label">可用模型</div></div>
    <div class="card"><div class="num">${totalReq}</div><div class="label">总请求</div></div>
    <div class="card"><div class="num">${(totalTok/1e6).toFixed(1)}M</div><div class="label">总 Token</div></div>
  `;
  const maxTok=Math.max(...ts.map(t=>t.total_tokens),1);
  document.getElementById('tbody').innerHTML=ts.map(t=>`
    <tr>
      <td>${t.target_id}</td>
      <td class="${t.available?'tr-ok':'tr-na'}">${t.available?'✅':'❌'}</td>
      <td><span title="智能分数 ${t.intelligence||'-'}/100" style="color:#2b5cb8;font-weight:600">${t.intelligence||'-'}</span></td>
      <td style="color:#5a6a7a">${t.priority??'-'}</td>
      <td style="color:${t.free?'#2e7d32':'#e65100'}">${t.free?'免费':'付费'}</td>
      <td class="${t.requests>0?(t.failures>0?'tr-fail':'tr-ok'):'tr-na'}" title="${t.successes}/${t.requests}">${t.requests>0?(t.successes/t.requests*100).toFixed(1)+'%':'-'}</td>
      <td>${t.total_tokens>1e6?(t.total_tokens/1e6).toFixed(1)+'M':t.total_tokens}</td>
      <td>${t.ewma_latency_ms?t.ewma_latency_ms.toFixed(0):'-'}</td>
      <td><div class="bar"><div class="bar-fill" style="width:${(t.total_tokens/maxTok*100).toFixed(0)}%"></div></div></td>
    </tr>`).join('');
}
load();setInterval(load,10000);
</script></body></html>""")

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats(request: Request) -> dict[str, Any]:
        # Read the CURRENT config (not the startup closure) so SIGHUP reloads
        # are reflected immediately.
        loaded = request.app.state.config
        states = request.app.state.store.all_stats()
        return {
            "in_flight": request.app.state.router.in_flight,
            "targets": {
                target.id: {
                    "label": target.label,
                    "model": target.model,
                    "local": target.local,
                    "available": target.available,
                    "capabilities": sorted(target.capabilities),
                    "intelligence": target.intelligence,
                    "priority": target.priority,
                    "free": target.free,
                    **states[target.id].public(),
                }
                for target in loaded.targets
            },
            "recent_decisions": request.app.state.store.recent_decisions(),
            "memory_sync": request.app.state.memory_sync.status(),
        }

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        loaded = request.app.state.config
        created = int(time.time())
        entries = [{"id": "damselfish/auto", "object": "model", "created": created, "owned_by": "damselfish"}]
        entries.extend(
            {
                "id": target.id,
                "object": "model",
                "created": created,
                "owned_by": "local" if target.local else "upstream",
            }
            for target in loaded.targets
            if target.available
        )
        return {"object": "list", "data": entries}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        x_damselfish_session: str | None = Header(None),
        x_damselfish_session_title: str | None = Header(None),
        x_damselfish_project: str | None = Header(None),
        x_damselfish_project_title: str | None = Header(None),
        x_damselfish_scenario: str | None = Header(None),
        x_damselfish_persona: str | None = Header(None),
    ):
        try:
            payload = await request.json()
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="request body must be JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise HTTPException(status_code=400, detail="messages must be an array")
        # Read the CURRENT config so SIGHUP reloads take effect without restart.
        loaded = request.app.state.config

        extension = payload.pop("damselfish", {}) or {}
        if not isinstance(extension, dict):
            raise HTTPException(status_code=400, detail="damselfish must be an object")
        session_id = _identifier(
            x_damselfish_session or extension.get("session_id"), "session_id", optional=True
        )
        raw_project = x_damselfish_project or extension.get("project_id")
        if not raw_project:
            raw_project = _infer_project_id(payload.get("messages", []))
        # Track whether the project was actually identified (header,
        # extension, or system-prompt inference).  The "default" bucket
        # mixes unrelated stateless clients, so cross-session context
        # injection must never draw from it — doing so leaked one
        # client's history into another's conversation.
        project_identified = bool(raw_project)
        project_id = _identifier(raw_project or "default", "project_id")
        if not session_id:
            session_id = _derive_session_id(payload.get("messages", []))
        project_title = _title(
            x_damselfish_project_title or extension.get("project_title")
        )
        session_title = _title(
            x_damselfish_session_title or extension.get("session_title")
        )
        scenario = x_damselfish_scenario or extension.get("scenario")
        persona = x_damselfish_persona or extension.get("persona")
        # Request headers/extension are DEFAULT hints; content inference
        # (system prompt keywords, tools, image presence) takes priority so
        # CEO-delegated subagents get the right persona/scenario from their
        # system prompt rather than the static provider header default.
        header_scenario = scenario
        header_persona = persona
        memory_enabled = bool(extension.get("memory", True)) and bool(session_id)
        project_memory_enabled = (
            bool(extension.get("project_memory", True)) and project_identified
        )
        incoming = payload["messages"]
        history = []
        transcript = list(incoming)
        if memory_enabled:
            await request.app.state.memory_sync.pull_if_due()
            history = request.app.state.store.get_project_session(
                project_id, session_id, loaded.routing.memory_ttl_days
            )
            transcript = merge_messages(history, incoming)
            payload["messages"] = transcript
            if project_memory_enabled:
                shared = request.app.state.store.project_context(
                    project_id,
                    session_id,
                    loaded.routing.project_memory_session_limit,
                    loaded.routing.project_memory_message_limit,
                )
                context_message = project_context_message(
                    project_id, shared, loaded.routing.project_memory_max_chars
                )
                if context_message:
                    payload["messages"] = [context_message, *transcript]

        # Content inference from the ORIGINAL incoming messages (before memory
        # merge) so memory history doesn't pollute persona/scenario detection.
        context = infer_context(
            loaded, incoming, payload.get("tools"), None, None
        )
        if header_persona and not context.persona:
            context = replace(context, persona=header_persona.lower())
        if header_scenario and context.scenario == "default":
            context = replace(context, scenario=header_scenario.lower())
        # Persona/scenario `targets` lists are hard allowlists; resolve after
        # header overrides so an explicit persona header also restricts.
        context = resolve_allowed_targets(loaded, context)
        wants_stream = bool(payload.get("stream"))
        try:
            decision_session = f"{project_id}/{session_id}" if session_id else None
            # Multi-role collaboration: a bare question that spans roles
            # (e.g. 评估+修复) runs as a staged pipeline.  Agent turns (tools)
            # are never pipelined implicitly; on any pipeline failure the
            # normal single-model path below takes over.
            collab_roles = plan_collaboration(
                payload, loaded, request.headers.get("x-damselfish-collab"),
            )
            if collab_roles:
                response = await _handle_collaboration(
                    request, payload, context, decision_session, collab_roles,
                    memory_enabled=memory_enabled,
                    transcript=transcript,
                    session_id=session_id,
                    project_id=project_id,
                    project_title=project_title,
                    session_title=session_title,
                    max_messages=loaded.routing.memory_max_messages,
                )
                if response is not None:
                    return response
            if wants_stream:
                return await _handle_streaming(
                    request, payload, context, decision_session,
                    memory_enabled, transcript, project_id, project_title,
                    session_title, session_id,
                )
            # Response cache for non-streaming requests (same prompt returns
            # cached result without hitting upstream). Use ORIGINAL incoming
            # messages for the key so memory merge doesn't break cache hits.
            cache_cfg = loaded.routing
            if cache_cfg.cache_enabled:
                cache_key = _cache_key({"messages": incoming, "model": payload.get("model","auto"), "tools": payload.get("tools",[])}, context, project_id)
                cached = _cache_get(request.app.state, cache_key, cache_cfg.cache_ttl_seconds)
                if cached is not None:
                    headers = {
                        "X-Damselfish-Target": cached["target_id"],
                        "X-Damselfish-Model": cached["model"],
                        "X-Damselfish-Latency-Ms": "0.0",
                        "X-Damselfish-Scenario": context.scenario,
                        "X-Damselfish-Cache": "HIT",
                        "X-Damselfish-Project": project_id,
                        "X-Damselfish-Memory-Sync": request.app.state.memory_sync.response_status(),
                    }
                    if session_id:
                        headers["X-Damselfish-Session"] = session_id
                    return JSONResponse(content=cached["body"], headers=headers)
            result = await request.app.state.router.complete(
                payload, context, decision_session
            )
            if cache_cfg.cache_enabled:
                _cache_set(request.app.state, _cache_key({"messages": incoming, "model": payload.get("model","auto"), "tools": payload.get("tools",[])}, context, project_id), {
                    "target_id": result.target.id,
                    "model": result.target.model,
                    "body": result.body,
                }, cache_cfg.cache_max_entries)
        except NoTargetAvailable as error:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": str(error), "type": "router_unavailable"}},
            )

        if memory_enabled:
            assistant = result.body["choices"][0]["message"]
            request.app.state.store.save_session(
                session_id,
                transcript + [assistant],
                loaded.routing.memory_max_messages,
                project_id=project_id,
                project_title=project_title,
                session_title=session_title,
                source_device=request.app.state.memory_sync.device_id,
            )
            await request.app.state.memory_sync.sync_pending()
            # Background compression for long conversations
            if len(transcript) + 1 > loaded.routing.memory_compression_threshold:
                asyncio.create_task(_compress_conversation(
                    request.app.state.store, request.app.state.router,
                    session_id, transcript + [assistant],
                    loaded.routing.memory_compression_keep,
                ))
        headers = {
            "X-Damselfish-Target": result.target.id,
            "X-Damselfish-Model": result.target.model,
            "X-Damselfish-Latency-Ms": f"{result.latency_ms:.1f}",
            "X-Damselfish-Scenario": context.scenario,
            "X-Damselfish-Project": project_id,
            "X-Damselfish-Memory-Sync": request.app.state.memory_sync.response_status(),
        }
        if session_id:
            headers["X-Damselfish-Session"] = session_id
        return JSONResponse(content=result.body, headers=headers)

    return app


def build_default_app() -> FastAPI:
    return create_app()


# ------------------------------------------------------------------ #
# Caching
# ------------------------------------------------------------------ #
def _cache_key(payload: dict[str, Any], context, project_id: str = "") -> str:
    """Build a stable cache key from messages, model, scenario, persona.

    The project id is part of the key: identical opening prompts from
    different projects must never share cached responses.
    """
    import hashlib
    raw = json.dumps({
        "messages": payload.get("messages", []),
        "model": payload.get("model", "auto"),
        "scenario": context.scenario,
        "persona": context.persona,
        "tools": payload.get("tools", []),
        "project": project_id,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(app_state, key: str, ttl_seconds: int):
    """Return cached entry if not expired, else None."""
    cache = getattr(app_state, "_response_cache", None)
    if cache is None:
        return None
    entry = cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > ttl_seconds:
        cache.pop(key, None)
        return None
    return entry["data"]


def _cache_set(app_state, key: str, data, max_entries: int):
    """Store a cache entry, evicting oldest if over max_entries."""
    if not hasattr(app_state, "_response_cache"):
        app_state._response_cache = {}
    cache = app_state._response_cache
    if len(cache) >= max_entries:
        oldest = min(cache, key=lambda k: cache[k]["ts"])
        cache.pop(oldest, None)
    cache[key] = {"ts": time.time(), "data": data}


# ------------------------------------------------------------------ #
# Conversation compression
# ------------------------------------------------------------------ #
async def _compress_conversation(store, router, session_id, messages, keep):
    """Compress old conversation messages using a lightweight model."""
    if not session_id or len(messages) <= keep + 5:
        return
    try:
        old = messages[:-keep]
        recent = messages[-keep:]
        text = "\n".join(
            str(m.get("content", ""))
            for m in old if isinstance(m.get("content"), str) and m.get("content")
        )
        if not text.strip():
            return
        prompt = (
            "请用中文简要总结以下对话。\n"
            "涵盖用户需求、已解决的问题和关键决策。\n"
            "保留足够细节以保持对话连续性。最多 200 字。\n\n" + text
        )
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400, "temperature": 0.5,
        }
        ctx = RouteContext(
            scenario="default", persona=None,
            required=frozenset({"chat"}), preferred=frozenset({"fast"}),
            preferred_targets=(),
            estimated_input_tokens=estimate_messages_tokens(payload["messages"]),
        )
        result = await router.complete(payload, ctx, None)
        summary = result.body["choices"][0]["message"].get("content", "")
        if not summary:
            return
        compressed = [
            {"role": "system", "content": "对话摘要：" + summary}
        ] + recent
        old_tokens = estimate_messages_tokens(messages)
        new_tokens = estimate_messages_tokens(compressed)
        if new_tokens >= old_tokens:
            log.info("compression skipped for %s: tokens %d -> %d (no reduction)",
                     _short_id(session_id), old_tokens, new_tokens)
            return
        store.update_session_messages(session_id, compressed)
        log.info("compressed session %s: %d -> %d messages, tokens %d -> %d",
                 _short_id(session_id), len(messages), len(compressed), old_tokens, new_tokens)
    except Exception as e:
        log.warning("compression failed for %s: %s", _short_id(session_id), e)


# ------------------------------------------------------------------ #
# Streaming
# ------------------------------------------------------------------ #
async def _as_sse(body: dict[str, Any]) -> AsyncIterator[str]:
    choice = body["choices"][0]
    chunk = {
        "id": body.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion.chunk",
        "created": body.get("created", int(time.time())),
        "model": body.get("model", "damselfish/auto"),
        "choices": [
            {
                "index": choice.get("index", 0),
                "delta": choice["message"],
                "finish_reason": choice.get("finish_reason"),
            }
        ],
    }
    if body.get("usage"):
        chunk["usage"] = body["usage"]
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _handle_collaboration(
    request: Request,
    payload: dict[str, Any],
    context: RouteContext,
    decision_session: str | None,
    roles: tuple[str, ...],
    *,
    memory_enabled: bool,
    transcript: list[dict[str, Any]],
    session_id: str | None,
    project_id: str,
    project_title: str | None,
    session_title: str | None,
    max_messages: int,
) -> JSONResponse | StreamingResponse | None:
    """Run the multi-role pipeline.  Returns None to fall back to normal
    single-model routing when the pipeline cannot produce anything."""
    started = time.perf_counter()
    try:
        body, meta = await run_pipeline(
            request.app.state.router, request.app.state.config, payload,
            context, decision_session, roles,
        )
    except CollabUnavailable as error:
        log.warning("collab pipeline unavailable (%s); normal routing takes over", error)
        return None
    except Exception as error:  # noqa: BLE001 — the pipeline must never break the endpoint
        log.warning("collab pipeline error (%s); normal routing takes over", error)
        return None

    if memory_enabled and session_id:
        assistant = body["choices"][0]["message"]
        request.app.state.store.save_session(
            session_id,
            transcript + [assistant],
            max_messages,
            project_id=project_id,
            project_title=project_title,
            session_title=session_title,
            source_device=request.app.state.memory_sync.device_id,
        )
        await request.app.state.memory_sync.sync_pending()

    targets = [part.split("=", 1)[1] for part in meta["targets"]]
    headers = {
        "X-Damselfish-Target": ",".join(dict.fromkeys(targets)),
        "X-Damselfish-Model": "damselfish/collab",
        "X-Damselfish-Pipeline": ", ".join(meta["targets"]),
        "X-Damselfish-Latency-Ms": f"{(time.perf_counter() - started) * 1000:.1f}",
        "X-Damselfish-Scenario": context.scenario,
        "X-Damselfish-Project": project_id,
        "X-Damselfish-Memory-Sync": request.app.state.memory_sync.response_status(),
    }
    if session_id:
        headers["X-Damselfish-Session"] = session_id

    if payload.get("stream"):
        merged = body["choices"][0]["message"]["content"]
        return StreamingResponse(
            sse_stream_from_text(merged, str(payload.get("model", "auto"))),
            media_type="text/event-stream",
            headers=headers,
        )
    return JSONResponse(content=body, headers=headers)


async def _handle_streaming(
    request: Request,
    payload: dict[str, Any],
    context: RouteContext,
    decision_session: str | None,
    memory_enabled: bool,
    transcript: list[dict[str, Any]],
    project_id: str,
    project_title: str | None,
    session_title: str | None,
    session_id: str | None,
) -> StreamingResponse:
    """Handle a streaming chat completion request.

    Calls ``router.stream_complete()`` and forwards normalized SSE chunks
    to the client.  Accumulates content and saves memory after the stream
    ends.
    """
    router = request.app.state.router
    loaded = request.app.state.config
    accumulated_content: list[str] = []
    accumulated_chars: list[int] = [0]
    _MAX_ACCUMULATED_CHARS = 50000
    first_chunk_time: list[float] = []

    async def stream_chunks() -> AsyncIterator[str]:
        target_id = ""
        target_model = ""
        try:
            async for chunk in router.stream_complete(payload, context, decision_session):
                # Track latency from first chunk
                if not first_chunk_time:
                    first_chunk_time.append(time.monotonic())
                # Accumulate content for memory (with cap to avoid OOM on huge streams)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if isinstance(delta, dict) and delta.get("content"):
                        content_piece = delta["content"]
                        if accumulated_chars[0] < _MAX_ACCUMULATED_CHARS:
                            accumulated_content.append(content_piece)
                            accumulated_chars[0] += len(content_piece)
                if chunk.get("model"):
                    target_model = chunk["model"]
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except NoTargetAvailable as error:
            error_chunk = {
                "error": {"message": str(error), "type": "router_unavailable"}
            }
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            return
        # Emit a final chunk with usage + finish_reason so downstream
        # consumers (e.g. DSH token-meter) can compute tok/s. Most upstream
        # providers do not include usage in streaming chunks, so estimate
        # from accumulated content and the request messages.
        from .tokens import estimate_text_tokens, estimate_messages_tokens
        full_content = "".join(accumulated_content)
        completion_tokens = estimate_text_tokens(full_content)
        prompt_tokens = estimate_messages_tokens(payload.get("messages", []))
        final_chunk = {
            "id": "",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": target_model or "damselfish/auto",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        # Save memory after stream ends
        result = getattr(router, "_stream_result", None)
        if result is not None:
            target_id = result.target.id
            target_model = result.target.model
        log.info("stream response target=%s content_preview=%s",
                 target_id, repr(full_content[:200]))
        if memory_enabled and session_id and accumulated_content:
            assistant = {"role": "assistant", "content": "".join(accumulated_content)}
            request.app.state.store.save_session(
                session_id,
                transcript + [assistant],
                loaded.routing.memory_max_messages,
                project_id=project_id,
                project_title=project_title,
                session_title=session_title,
                source_device=request.app.state.memory_sync.device_id,
            )
            await request.app.state.memory_sync.sync_pending()
            if len(transcript) + 1 > loaded.routing.memory_compression_threshold:
                asyncio.create_task(_compress_conversation(
                    request.app.state.store, request.app.state.router,
                    session_id, transcript + [assistant],
                    loaded.routing.memory_compression_keep,
                ))

    headers = {
        "X-Damselfish-Scenario": context.scenario,
        "X-Damselfish-Project": project_id,
        "X-Damselfish-Memory-Sync": request.app.state.memory_sync.response_status(),
    }
    if session_id:
        headers["X-Damselfish-Session"] = session_id
    return StreamingResponse(
        stream_chunks(), media_type="text/event-stream", headers=headers
    )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _infer_project_id(messages: list[dict[str, Any]]) -> str | None:
    """Infer project_id from the working directory in the system prompt.

    DSH (and other agentic clients) include ``Your working directory is
    /path/to/project`` in the system prompt.  Extract the last path segment
    so sessions from different projects are isolated in memory even when
    the client doesn't send X-Damselfish-Project headers.
    """
    for msg in messages[:3]:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        m = re.search(r"[Ww]orking directory is (/[\S]+)", content)
        if m:
            path = m.group(1).rstrip("/.,;:")
            return path.rsplit("/", 1)[-1] or None
    return None


def _derive_session_id(messages: list[dict[str, Any]]) -> str | None:
    """Derive a stable session id from the opening messages.

    Enables memory, context, and cloud sync for stateless clients (e.g.
    agentic coding tools) that send the full conversation in each request
    but omit ``X-Damselfish-Session`` headers.  The same opening messages
    always map to the same session id, so conversations stay continuous
    across requests, devices, and agents.

    The first system prompt is part of the fingerprint: different projects
    running through a stateless client often start with identical user
    text ("继续", "评估这个项目"), and hashing the user message alone
    would merge those conversations into one session.
    """
    first_user = None
    first_system = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if first_system is None and msg.get("role") == "system":
            if isinstance(content, str):
                first_system = content
            elif isinstance(content, list):
                first_system = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
        if first_user is None and msg.get("role") == "user":
            if isinstance(content, str):
                first_user = content.strip()[:2000] or None
            elif isinstance(content, list):
                first_user = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()[:2000] or None
        if first_user is not None and first_system is not None:
            break
    if not first_user:
        return None
    fingerprint = f"{first_system or ''}\x00{first_user}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def _identifier(value: Any, name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, (str, int)):
        raise HTTPException(status_code=400, detail=f"{name} must be a string")
    normalized = str(value).strip()
    if not normalized:
        if optional:
            return None
        raise HTTPException(status_code=400, detail=f"{name} cannot be empty")
    if len(normalized) > 200:
        raise HTTPException(status_code=400, detail=f"{name} is too long")
    return normalized


def _short_id(session_id: str | None, length: int = 8) -> str:
    """Truncate session_id for logging, safe for Unicode strings."""
    if not session_id:
        return "-"
    return session_id[:length]


def _title(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="memory title must be a string")
    normalized = " ".join(value.split())
    return normalized[:200] or None

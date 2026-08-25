"""Multi-role collaboration pipeline.

One request can require several specialists — e.g. "评估后修复" needs
评估 → 修复 → 审查 → 测试.  Each stage runs as a normal routed request on
that role's fixed model list (persona allowlist + per-role session pins
still apply; the sub-session key is "<session>/<role>").  Stage outputs
are merged into one assistant message, so the caller sees the full
collaboration trace.

Auto-trigger only fires for bare questions: payloads that carry tools are
agent turns (zcode / Hermes / DSH) and are never pipelined unless the
caller opts in explicitly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from .config import AppConfig
from .router import NoTargetAvailable, _is_canned_refusal
from .selector import RouteContext, resolve_allowed_targets
from .tokens import estimate_messages_tokens

log = logging.getLogger("damselfish.pipeline")

COLLAB_MODELS = {"damselfish/collab", "collab"}


@dataclass(frozen=True)
class StageSpec:
    role: str            # persona key in config.personas
    title: str           # section heading in the merged answer
    system: str          # stage instruction injected as system message
    patterns: tuple[re.Pattern[str], ...]


def _p(*words: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(w) for w in words)


# Canonical execution order: evaluate first, test last.
STAGES: dict[str, StageSpec] = {
    "evaluator": StageSpec(
        "evaluator", "评估",
        "你是评估工程师。只做问题定位与影响评估：列出发现的问题、严重度和根因，"
        "不要实施修改。用简洁的中文要点输出。",
        _p(r"评估", r"评测", r"诊断", r"根因", r"问题出在", r"哪里有问题", r"风险"),
    ),
    "developer": StageSpec(
        "developer", "修复",
        "你是开发工程师。基于前面阶段的结论实施修复/实现，给出完整可用的方案或代码，"
        "不要重复评估过程。用中文输出。",
        _p(r"修复", r"解决", r"改好", r"修好", r"实现", r"开发", r"重构", r"帮我写", r"帮我做"),
    ),
    "reviewer": StageSpec(
        "reviewer", "审查",
        "你是审查员。审查前面阶段产出的方案/代码：正确性、边界条件、安全隐患、遗漏，"
        "指出必须修正的点；没有问题就明确说通过。用中文要点输出。",
        _p(r"审查", r"评审", r"review", r"把关", r"检查一下", r"挑错"),
    ),
    "tester": StageSpec(
        "tester", "测试",
        "你是测试工程师。基于前面的修复与审查结论设计验证：测试用例/验证步骤和预期结果，"
        "最后给出明确的通过/不通过结论。用中文输出。",
        _p(r"测试", r"单测", r"用例", r"回归", r"验证"),
    ),
}
CANONICAL_ORDER = ("evaluator", "developer", "reviewer", "tester")


class CollabUnavailable(Exception):
    """No stage produced output — caller should fall back to normal routing."""


def plan_collaboration(
    payload: dict[str, Any],
    config: AppConfig,
    header_collab: str | None = None,
) -> tuple[str, ...] | None:
    """Decide whether a request runs as a multi-role pipeline.

    Returns an ordered role tuple (≥2 entries) or None for normal routing.
    """
    text = _question_text(payload.get("messages") or [])
    explicit = header_collab or (
        str(payload.get("model", "")).lower() in COLLAB_MODELS
    )
    if not explicit and payload.get("tools"):
        return None  # agent turn — never pipelined implicitly

    available = [r for r in CANONICAL_ORDER if r in config.personas]
    matched = [
        r for r in available
        if any(p.search(text) for p in STAGES[r].patterns)
    ]
    if explicit:
        requested = [
            r.strip().lower()
            for r in str(header_collab or "").split(",") if r.strip()
        ]
        roles = [r for r in requested if r in available] or matched or available
    else:
        roles = matched
        if len(roles) < 2:
            return None  # single-domain question → normal routing

    roles = tuple(dict.fromkeys(roles))
    return roles if len(roles) >= 2 else None


def _question_text(messages: list[dict[str, Any]]) -> str:
    parts = []
    for m in messages[-4:]:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts).lower()


def build_stage_messages(
    payload: dict[str, Any],
    spec: StageSpec,
    prior_sections: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Stage input = stage instruction + original question + prior outputs."""
    question = ""
    history: list[dict[str, Any]] = []
    for m in payload.get("messages") or []:
        if m.get("role") == "system":
            continue
        if isinstance(m.get("content"), str):
            history.append({"role": m["role"], "content": m["content"]})
    for m in reversed(history):
        if m["role"] == "user":
            question = m["content"]
            break

    merged_msgs: list[dict[str, Any]] = [{"role": "system", "content": spec.system}]
    if prior_sections:
        rendered = "\n\n".join(
            f"【{title}】\n{text}" for title, _, text in prior_sections[-3:]
        )
        if len(rendered) > 6000:
            rendered = rendered[:3000] + "\n…\n" + rendered[-2500:]
        merged_msgs.append({
            "role": "user",
            "content": f"以下是前置阶段的结论：\n\n{rendered}",
        })
        merged_msgs.append({
            "role": "assistant",
            "content": "已阅读前置结论，请继续。",
        })
    merged_msgs.append({
        "role": "user",
        "content": question or "请处理上述内容。",
    })
    return merged_msgs


_STAGE_ATTEMPTS = 3      # tries per stage before its section is lost
_SECTION_MIN_CHARS = 2   # shorter content counts as empty
# Reasoning models spend thinking tokens before any content appears; a
# small client-side max_tokens starves them into empty answers
# (finish_reason=length, content="").  Each stage gets its own floor.
_STAGE_MIN_TOKENS = 4096


def _stage_text_ok(text: str) -> bool:
    """A stage output is only usable when it is real content.

    Empty strings and canned refusals (HTTP 200 + "作为一个人工智能…")
    count as failures so the stage retries on another target — this is
    what guarantees section completeness.
    """
    if not text or len(text.strip()) < _SECTION_MIN_CHARS:
        return False
    return not _is_canned_refusal(text)


async def run_pipeline(
    router: Any,
    config: AppConfig,
    payload: dict[str, Any],
    base_context: RouteContext,
    session_id: str | None,
    roles: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute stages sequentially; every planned role must produce output.

    Per stage: up to ``_STAGE_ATTEMPTS`` attempts.  A failed attempt rotates
    to a different target via the avoid-set (ranking-level exclusion), and
    the final attempt drops the persona allowlist so the global pool backs
    the stage up.  A role's section is lost only when the whole pool is
    unavailable — at which point normal routing would fail too.  Raises
    CollabUnavailable only when NO stage produced output, so the caller can
    fall back to normal single-model routing.
    """
    sections: list[tuple[str, str, str]] = []
    targets_used: list[str] = []
    prompt_tokens = completion_tokens = 0

    for role in roles:
        spec = STAGES[role]
        ctx_role = resolve_allowed_targets(config, replace(base_context, persona=role))
        stage_payload = dict(payload)
        stage_payload["messages"] = build_stage_messages(payload, spec, sections)
        stage_payload.pop("stream", None)
        try:
            requested = int(stage_payload.get("max_tokens") or 0)
        except (TypeError, ValueError):
            requested = 0
        stage_payload["max_tokens"] = max(requested, _STAGE_MIN_TOKENS)
        sub_session = f"{session_id}/{role}" if session_id else None

        text, target_id = "", ""
        tried: list[str] = []
        usage: dict[str, Any] = {}
        for attempt in range(1, _STAGE_ATTEMPTS + 1):
            ctx = ctx_role
            if attempt == _STAGE_ATTEMPTS and ctx_role.allowed_targets:
                # Last resort for this section: let the whole pool back it.
                log.warning("pipeline stage %s: falling back to global pool", role)
                ctx = replace(ctx_role, allowed_targets=None)
            try:
                result = await router.complete(
                    dict(stage_payload), ctx, sub_session,
                    avoid=frozenset(tried) or None,
                )
            except NoTargetAvailable as error:
                log.warning("pipeline stage %s attempt %d: no target: %s",
                            role, attempt, error)
                await asyncio.sleep(min(1.5 * attempt, 4))
                continue
            except Exception as error:  # noqa: BLE001 — keep the pipeline alive
                log.warning("pipeline stage %s attempt %d failed: %s",
                            role, attempt, error)
                await asyncio.sleep(min(1.5 * attempt, 4))
                continue
            candidate = ""
            finish = ""
            try:
                choice = result.body["choices"][0]
                candidate = choice["message"].get("content") or ""
                finish = str(choice.get("finish_reason") or "")
            except (KeyError, IndexError, TypeError):
                pass
            if not _stage_text_ok(candidate):
                log.warning(
                    "pipeline stage %s attempt %d: unusable output from %s"
                    " (%d chars, finish=%s), rotating target", role, attempt,
                    result.target.id, len(candidate), finish or "?",
                )
                tried.append(result.target.id)
                continue
            text, target_id = candidate, result.target.id
            usage = result.body.get("usage") or {}
            break

        if not text.strip():
            log.error("pipeline stage %s exhausted all attempts — section omitted", role)
            continue
        sections.append((spec.title, target_id, text))
        targets_used.append(f"{role}={target_id}")
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)

    if not sections:
        raise CollabUnavailable("all pipeline stages failed")

    merged = "\n\n".join(
        f"## {title}（{target}）\n\n{text}" for title, target, text in sections
    )
    body = {
        "id": f"chatcmpl-collab-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(payload.get("model", "auto")),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": merged},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    log.info("pipeline done: %s", ", ".join(targets_used))
    meta = {"targets": targets_used, "roles": [t for t, _, _ in sections]}
    return body, meta


def sse_stream_from_text(text: str, model: str):
    """Fabricate standard chat.completion SSE chunks from a final text."""
    async def stream():
        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            obj = {
                "id": f"chatcmpl-collab-{int(time.time() * 1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        yield chunk({"role": "assistant"})
        piece_size = 64
        for i in range(0, len(text), piece_size):
            yield chunk({"content": text[i:i + piece_size]})
        yield chunk({}, finish="stop")
        yield "data: [DONE]\n\n"

    return stream()

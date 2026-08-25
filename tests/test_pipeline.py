"""Multi-role collaboration pipeline: planning, completeness, rotation."""
import asyncio
from pathlib import Path

import httpx

from damselfish.config import AppConfig, PersonaRule, RouteRule, RoutingConfig, TargetConfig
from damselfish.pipeline import (
    CollabUnavailable,
    build_stage_messages,
    plan_collaboration,
    run_pipeline,
    sse_stream_from_text,
)
from damselfish.router import ModelRouter
from damselfish.selector import RouteContext
from damselfish.store import Store


def _config(tmp_path: Path):
    return AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=0),
        targets=(
            TargetConfig("flaky", "Flaky", "http://r/v1", "m-flaky", local=True),
            TargetConfig("solid", "Solid", "http://r/v1", "m-solid", local=True),
            TargetConfig("devbox", "DevBox", "http://r/v1", "m-dev", local=True),
            TargetConfig("testlab", "TestLab", "http://r/v1", "m-test", local=True),
        ),
        personas={
            "evaluator": PersonaRule(targets=("flaky", "solid")),
            "developer": PersonaRule(targets=("devbox",)),
            "tester": PersonaRule(targets=("testlab",)),
        },
        scenarios={"default": RouteRule(preferred=frozenset({"chat"}))},
    )


def _payload(content: str, **extra) -> dict:
    payload = {"model": "auto", "messages": [{"role": "user", "content": content}]}
    payload.update(extra)
    return payload


def _ctx() -> RouteContext:
    return RouteContext("default", None, frozenset(), frozenset(), ())


# ── Planning ─────────────────────────────────────────────────────────


def test_plan_multi_role_question() -> None:
    config = _config(Path("/tmp/dsf-test"))
    roles = plan_collaboration(
        _payload("评估这段代码的问题，然后修复它"), config,
    )
    assert roles == ("evaluator", "developer")


def test_plan_single_domain_not_pipelined() -> None:
    config = _config(Path("/tmp/dsf-test"))
    assert plan_collaboration(_payload("帮我修个bug"), config) is None
    assert plan_collaboration(_payload("写个测试用例"), config) is None


def test_plan_agent_tools_never_auto_pipelined() -> None:
    config = _config(Path("/tmp/dsf-test"))
    roles = plan_collaboration(
        _payload("评估并修复，然后审查和测试", tools=[{"type": "function"}]), config,
    )
    assert roles is None


def test_plan_explicit_header_overrides_and_filters() -> None:
    config = _config(Path("/tmp/dsf-test"))
    roles = plan_collaboration(
        _payload("随便什么内容"), config, header_collab="tester, developer",
    )
    assert roles == ("tester", "developer")
    # Explicit with unknown role names → falls back to all available.
    roles = plan_collaboration(_payload("x"), config, header_collab="writer")
    assert roles == ("evaluator", "developer", "tester")


# ── Stage message construction ───────────────────────────────────────


def test_build_stage_messages_chains_prior_sections() -> None:
    from damselfish.pipeline import STAGES
    msgs = build_stage_messages(
        _payload("修复登录超时"),
        STAGES["developer"],
        [("评估", "ev-1", "发现 token 过期逻辑错误")],
    )
    assert msgs[0]["role"] == "system" and "开发工程师" in msgs[0]["content"]
    joined = json_join(msgs)
    assert "token 过期" in joined and "修复登录超时" in joined


def json_join(msgs) -> str:
    return "\n".join(m["content"] for m in msgs)


# ── Execution & completeness ─────────────────────────────────────────


def _ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "ok", "choices": [{"message": {"role": "assistant", "content": text},
                                  "finish_reason": "stop"}],
    })


REFUSAL = "作为一个人工智能语言模型，我还没学习如何回答这个问题。"


def test_run_pipeline_full_sections_and_pins(tmp_path: Path) -> None:
    """Every planned role produces its section; per-role pins are recorded."""
    config = _config(tmp_path)
    store = Store(config.database, ["flaky", "solid", "devbox", "testlab"])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append(body["model"])
        # Stage token floor: client's small max_tokens must not starve
        # reasoning models into empty answers.
        assert body["max_tokens"] >= 4096
        text = {
            "m-flaky": "评估结论：配置缺失。",
            "m-solid": "备用评估。",
            "m-dev": "修复补丁如下……",
            "m-test": "测试用例通过。",
        }[body["model"]]
        return _ok(text)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            body, meta = await run_pipeline(
                router, config,
                _payload("评估这个项目的问题，然后修复它"),
                _ctx(), "sess-1", ("evaluator", "developer"),
            )
            content = body["choices"][0]["message"]["content"]
            assert "## 评估（flaky）" in content and "配置缺失" in content
            assert "## 修复（devbox）" in content
            assert meta["targets"] == ["evaluator=flaky", "developer=devbox"]
            # Per-role session pins recorded under sub-session keys.
            assert store.get_session_route("sess-1/evaluator") == "flaky"
            assert store.get_session_route("sess-1/developer") == "devbox"

    asyncio.run(run())
    store.close()


def test_unusable_output_rotates_to_next_target(tmp_path: Path) -> None:
    """A canned refusal counts as failure — the stage retries on the next
    member of the role list via the avoid-set."""
    config = _config(tmp_path)
    store = Store(config.database, ["flaky", "solid", "devbox", "testlab"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        if body["model"] == "m-flaky":
            return _ok(REFUSAL)
        return _ok(f"{body['model']} 的正经回答")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            body, meta = await run_pipeline(
                router, config, _payload("评估一下风险在哪"), _ctx(),
                None, ("evaluator",),
            )
            content = body["choices"][0]["message"]["content"]
            assert REFUSAL not in content
            assert meta["targets"] == ["evaluator=solid"]

    asyncio.run(run())
    store.close()


def test_stage_exhaustion_falls_to_global_pool_then_raises(tmp_path: Path) -> None:
    """All targets failing → attempts exhaust → CollabUnavailable so the
    caller can fall back to normal routing."""
    config = _config(tmp_path)
    store = Store(config.database, ["flaky", "solid", "devbox", "testlab"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            try:
                await run_pipeline(
                    router, config, _payload("评估并修复"), _ctx(),
                    None, ("evaluator", "developer"),
                )
            except CollabUnavailable:
                pass
            else:
                raise AssertionError("expected CollabUnavailable")

    asyncio.run(run())
    store.close()


# ── SSE fabrication ───────────────────────────────────────────────────


def test_sse_stream_shape() -> None:
    async def collect():
        chunks = []
        async for piece in sse_stream_from_text("你好世界 hello", "collab"):
            chunks.append(piece)
        return chunks

    raw = asyncio.run(collect())
    assert raw[-1] == "data: [DONE]\n\n"
    import json
    objs = [json.loads(r.removeprefix("data: ").strip())
            for r in raw[:-1] if r.startswith("data: ")]
    assert objs[0]["choices"][0]["delta"] == {"role": "assistant"}
    contents = "".join(o["choices"][0]["delta"].get("content", "")
                       for o in objs[1:-1])
    assert contents == "你好世界 hello"
    assert objs[-1]["choices"][0]["finish_reason"] == "stop"

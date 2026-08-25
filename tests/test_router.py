import asyncio
import time
from pathlib import Path

import httpx
import pytest

from damselfish.config import AppConfig, RoutingConfig, TargetConfig
from damselfish.router import (
    ModelRouter,
    UpstreamFailure,
    _ensure_tool_call_ids,
    _estimate_current_input_tokens,
    _estimate_text_tokens,
    _is_context_overflow,
    _max_new_tokens,
    _retry_after_seconds,
    _upstream_payload,
)
from damselfish.selector import RouteContext
from damselfish.store import Store


def test_router_falls_back_after_rate_limit(tmp_path: Path) -> None:
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("first", "First", "http://router/v1", "first", local=True, priority=1),
            TargetConfig("second", "Second", "http://router/v1", "second", local=True, priority=2),
        ),
    )
    store = Store(config.database, ["first", "second"])

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        if model == "first":
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(
            200,
            json={"id": "ok", "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            assert result.target.id == "second"

    asyncio.run(run())
    assert store.stats("first").rate_limits == 1
    assert store.stats("first").circuit_open_until > 0
    store.close()


def test_router_accepts_reasoning_only_response(tmp_path: Path) -> None:
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(
            TargetConfig(
                "reasoning",
                "Reasoning",
                "http://router/v1",
                "reasoning-model",
                local=False,
            ),
        ),
    )
    store = Store(config.database, ["reasoning"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "OK",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            assert result.body["choices"][0]["message"]["reasoning_content"] == "OK"

    asyncio.run(run())
    assert store.stats("reasoning").successes == 1
    store.close()


def _success_response(model: str) -> dict:
    return {
        "id": f"ok-{model}",
        "choices": [
            {"message": {"role": "assistant", "content": f"from {model}"}, "finish_reason": "stop"}
        ],
    }


def test_parallel_fallback_on_429(tmp_path: Path) -> None:
    """Primary returns 429; parallel race picks the fastest remaining target."""
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(
            priority_weight_ms=1,
            parallel_fallback_count=3,
            parallel_fallback_timeout_seconds=5.0,
        ),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("fast", "Fast", "http://router/v1", "fast", local=True, priority=2),
            TargetConfig("slow", "Slow", "http://router/v1", "slow", local=True, priority=3),
            TargetConfig("last", "Last", "http://router/v1", "last", local=True, priority=4),
        ),
    )
    store = Store(config.database, ["primary", "fast", "slow", "last"])

    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        call_order.append(model)
        if model == "primary":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if model == "fast":
            return httpx.Response(200, json=_success_response("fast"))
        if model == "slow":
            return httpx.Response(200, json=_success_response("slow"))
        return httpx.Response(200, json=_success_response("last"))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            # Primary failed with 429, parallel race picks the fastest success
            assert result.target.id in {"fast", "slow", "last"}
            assert result.body["choices"][0]["message"]["content"].startswith("from ")

    asyncio.run(run())
    # Primary was called first, then parallel targets were all dispatched
    assert "primary" in call_order
    assert any(t in call_order for t in ("fast", "slow", "last"))
    assert store.stats("primary").rate_limits == 1
    store.close()


def test_parallel_fallback_on_timeout(tmp_path: Path) -> None:
    """Primary times out (504); parallel race picks the fastest remaining target."""
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(
            priority_weight_ms=1,
            parallel_fallback_count=2,
            parallel_fallback_timeout_seconds=5.0,
        ),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("alt", "Alt", "http://router/v1", "alt", local=True, priority=2),
            TargetConfig("backup", "Backup", "http://router/v1", "backup", local=True, priority=3),
        ),
    )
    store = Store(config.database, ["primary", "alt", "backup"])

    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        call_order.append(model)
        if model == "primary":
            # Simulate an upstream timeout
            raise httpx.TimeoutException("simulated timeout")
        return httpx.Response(200, json=_success_response(model))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            # Primary timed out (504), parallel race should pick "alt" or "backup"
            assert result.target.id in {"alt", "backup"}

    asyncio.run(run())
    assert "primary" in call_order
    assert "alt" in call_order
    store.close()


def test_parallel_fallback_all_fail_falls_to_serial(tmp_path: Path) -> None:
    """Primary returns 429, all parallel candidates also fail; serial fallback
    continues with remaining targets beyond the parallel limit."""
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(
            priority_weight_ms=1,
            parallel_fallback_count=2,
            parallel_fallback_timeout_seconds=5.0,
        ),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("fail1", "Fail1", "http://router/v1", "fail1", local=True, priority=2),
            TargetConfig("fail2", "Fail2", "http://router/v1", "fail2", local=True, priority=3),
            TargetConfig("winner", "Winner", "http://router/v1", "winner", local=True, priority=4),
        ),
    )
    store = Store(config.database, ["primary", "fail1", "fail2", "winner"])

    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        call_order.append(model)
        if model in ("primary", "fail1", "fail2"):
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(200, json=_success_response("winner"))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            # Parallel race (fail1, fail2) both fail, serial fallback picks winner
            assert result.target.id == "winner"

    asyncio.run(run())
    assert "primary" in call_order
    assert "fail1" in call_order
    assert "fail2" in call_order
    assert "winner" in call_order
    store.close()


# ─── Streaming tests ─────────────────────────────────────────────────


def test_stream_call_yields_chunks(tmp_path: Path) -> None:
    """_stream_call yields normalized SSE chunks from upstream."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("test", "Test", "http://router/v1", "test-model", local=True),),
    )
    store = Store(config.database, ["test"])
    sse_chunks = [
        'data: {"id":"x","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        'data: {"id":"x","choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n',
        'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    ]
    sse_bytes = "".join(sse_chunks).encode()

    def handler(request):
        return httpx.Response(200, content=sse_bytes)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        chunks = []
        async for chunk in router._stream_call(config.targets[0], payload):
            chunks.append(chunk)
        assert len(chunks) == 3
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[1]["choices"][0]["delta"]["content"] == "hello"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[0]["model"] == "test-model"

    asyncio.run(run())
    store.close()


def test_stream_call_converts_non_streaming_json_response(tmp_path: Path) -> None:
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("test", "Test", "http://router/v1", "test-model", local=True),),
    )
    store = Store(config.database, ["test"])

    def handler(request):
        return httpx.Response(200, json={
            "id": "completion",
            "object": "chat.completion",
            "created": 1,
            "model": "upstream-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
        })

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        chunks = []
        async for chunk in router._stream_call(
            config.targets[0], {"messages": [{"role": "user", "content": "hi"}]}
        ):
            chunks.append(chunk)
        await client.aclose()
        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "OK"
        assert chunks[0]["choices"][0]["finish_reason"] == "stop"

    asyncio.run(run())
    store.close()


def test_stream_call_429_raises_before_first_chunk(tmp_path: Path) -> None:
    """_stream_call raises UpstreamFailure before yielding if status is 429."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("test", "Test", "http://router/v1", "test-model", local=True),),
    )
    store = Store(config.database, ["test"])

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "limited"}})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        with pytest.raises(UpstreamFailure) as exc:
            async for _ in router._stream_call(config.targets[0], payload):
                pass
        assert exc.value.status == 429

    asyncio.run(run())
    store.close()


def test_stream_call_timeout_504_before_first_chunk(tmp_path: Path) -> None:
    """_stream_call raises UpstreamFailure(504) on timeout."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("test", "Test", "http://router/v1", "test-model", local=True),),
    )
    store = Store(config.database, ["test"])

    def handler(request):
        raise httpx.TimeoutException("simulated timeout")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        with pytest.raises(UpstreamFailure) as exc:
            async for _ in router._stream_call(config.targets[0], payload):
                pass
        assert exc.value.status == 504

    asyncio.run(run())
    store.close()


def test_stream_complete_phase1_success(tmp_path: Path) -> None:
    """stream_complete yields chunks from primary target on success."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("test", "Test", "http://router/v1", "test-model", local=True),),
    )
    store = Store(config.database, ["test"])
    sse_bytes = (
        'data: {"id":"x","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request):
        return httpx.Response(200, content=sse_bytes)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        chunks = []
        async for chunk in router.stream_complete(
            payload,
            RouteContext("default", None, frozenset(), frozenset(), ()),
            "test",
        ):
            chunks.append(chunk)
        assert len(chunks) == 3
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert router._stream_result is not None
        assert router._stream_result.target.id == "test"

    asyncio.run(run())
    store.close()


def test_stream_complete_phase1_429_fallback(tmp_path: Path) -> None:
    """Phase 1 returns 429, Phase 2 race yields chunks from winner."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(
            priority_weight_ms=1,
            parallel_fallback_count=2,
            parallel_fallback_timeout_seconds=5.0,
        ),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("alt", "Alt", "http://router/v1", "alt", local=True, priority=2),
            TargetConfig("backup", "Backup", "http://router/v1", "backup", local=True, priority=3),
        ),
    )
    store = Store(config.database, ["primary", "alt", "backup"])
    sse_bytes = (
        'data: {"id":"x","choices":[{"delta":{"content":"from alt"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request):
        model = __import__("json").loads(request.content)["model"]
        if model == "primary":
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(200, content=sse_bytes)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        chunks = []
        async for chunk in router.stream_complete(
            payload,
            RouteContext("default", None, frozenset(), frozenset(), ()),
            "test",
        ):
            chunks.append(chunk)
        assert len(chunks) >= 1
        assert router._stream_result is not None
        assert router._stream_result.target.id in {"alt", "backup"}

    asyncio.run(run())
    store.close()


def test_stream_complete_phase2_all_fail_falls_to_serial(tmp_path: Path) -> None:
    """Phase 1 429, Phase 2 all fail, Phase 3 serial fallback succeeds."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(
            priority_weight_ms=1,
            parallel_fallback_count=2,
            parallel_fallback_timeout_seconds=5.0,
        ),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("fail1", "Fail1", "http://router/v1", "fail1", local=True, priority=2),
            TargetConfig("fail2", "Fail2", "http://router/v1", "fail2", local=True, priority=3),
            TargetConfig("winner", "Winner", "http://router/v1", "winner", local=True, priority=4),
        ),
    )
    store = Store(config.database, ["primary", "fail1", "fail2", "winner"])

    def handler(request):
        model = __import__("json").loads(request.content)["model"]
        if model in ("primary", "fail1", "fail2"):
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(
            200,
            json={
                "id": "ok",
                "choices": [{
                    "message": {"role": "assistant", "content": "from winner"},
                    "finish_reason": "stop",
                }],
            },
        )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router = ModelRouter(config, store, client)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        chunks = []
        async for chunk in router.stream_complete(
            payload,
            RouteContext("default", None, frozenset(), frozenset(), ()),
            "test",
        ):
            chunks.append(chunk)
        assert len(chunks) >= 1
        assert router._stream_result is not None
        assert router._stream_result.target.id == "winner"

    asyncio.run(run())
    store.close()


# ── _max_new_tokens tests ────────────────────────────────────────────


def test_max_new_tokens_default() -> None:
    """Default max_new_tokens is 1024."""
    assert _max_new_tokens({}) == 1024
    assert _max_new_tokens({"messages": []}) == 1024


def test_max_new_tokens_from_payload() -> None:
    """Extracts max_tokens from payload."""
    assert _max_new_tokens({"max_tokens": 2048}) == 2048


def test_max_new_tokens_from_max_completion_tokens() -> None:
    """Falls back to max_completion_tokens."""
    assert _max_new_tokens({"max_completion_tokens": 4096}) == 4096


def test_max_new_tokens_prefers_max_tokens() -> None:
    """max_tokens beats max_completion_tokens."""
    assert _max_new_tokens({"max_tokens": 512, "max_completion_tokens": 1024}) == 512


# ── _estimate_text_tokens / _estimate_current_input_tokens tests ─────────────


def test_router_estimate_text_tokens_cjk() -> None:
    """CJK text estimated correctly."""
    from damselfish import tokens
    if tokens._TIKTOKEN_ENCODING is not None:
        assert _estimate_text_tokens("中" * 1000) == 1000
    else:
        assert _estimate_text_tokens("中" * 1000) == 1650


def test_router_estimate_text_tokens_ascii() -> None:
    """ASCII text estimated correctly."""
    from damselfish import tokens
    if tokens._TIKTOKEN_ENCODING is not None:
        assert _estimate_text_tokens("hello world " * 100) == 201
    else:
        assert _estimate_text_tokens("hello world " * 100) == 330


def test_router_estimate_current_input_tokens() -> None:
    """Estimate input tokens from messages list."""
    messages = [
        {"role": "user", "content": "中" * 100},
        {"role": "assistant", "content": "hello"},
    ]
    estimated = _estimate_current_input_tokens(messages)
    assert estimated > 0


# ── _is_context_overflow tests ───────────────────────────────────────


def test_is_context_overflow_detects_zhipu_error() -> None:
    """Detects Zhipu-style context overflow error."""
    error = UpstreamFailure(
        TargetConfig("test", "Test", "http://t/v1", "t"),
        400,
        "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 16384",
    )
    assert _is_context_overflow(error) is True


def test_is_context_overflow_detects_maximum_context() -> None:
    """Detects 'maximum context length' error."""
    error = UpstreamFailure(
        TargetConfig("test", "Test", "http://t/v1", "t"),
        400,
        "This model's maximum context length is 4096 tokens",
    )
    assert _is_context_overflow(error) is True


def test_is_context_overflow_detects_too_long() -> None:
    """Detects 'too long' error."""
    error = UpstreamFailure(
        TargetConfig("test", "Test", "http://t/v1", "t"),
        400,
        "text is too long for the model",
    )
    assert _is_context_overflow(error) is True


def test_is_context_overflow_rejects_429() -> None:
    """429 errors are not context overflow."""
    error = UpstreamFailure(
        TargetConfig("test", "Test", "http://t/v1", "t"),
        429,
        "rate limited",
    )
    assert _is_context_overflow(error) is False


def test_is_context_overflow_rejects_other_400() -> None:
    """Other 400 errors are not context overflow."""
    error = UpstreamFailure(
        TargetConfig("test", "Test", "http://t/v1", "t"),
        400,
        "invalid parameter: temperature must be between 0 and 2",
    )
    assert _is_context_overflow(error) is False


# ── _upstream_payload capping tests ──────────────────────────────────


def test_upstream_payload_caps_max_tokens() -> None:
    """max_tokens is capped when input + max_tokens > max_context."""
    # Use small max_context so cap triggers in both tiktoken and heuristic.
    # tiktoken: 1000 CJK = 1000 tokens; heuristic: 1000 CJK = 1650 tokens.
    # With max_context=1500 and max_tokens=1024, both backends exceed.
    target = TargetConfig(
        "test", "Test", "http://t/v1", "t",
        local=True, max_context=1500,
    )
    payload = {
        "messages": [{"role": "user", "content": "中" * 1000}],
        "max_tokens": 1024,
    }
    request, capped = _upstream_payload(payload, target, probe=False)
    assert request["max_tokens"] < 1024
    assert capped is True


def test_upstream_payload_no_cap_within_limit() -> None:
    """max_tokens is not capped when within max_context."""
    target = TargetConfig(
        "test", "Test", "http://t/v1", "t",
        local=True, max_context=4096,
    )
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 500,
    }
    request, capped = _upstream_payload(payload, target, probe=False)
    assert request["max_tokens"] == 500
    assert capped is False


def test_upstream_payload_no_max_context() -> None:
    """Without max_context, no capping occurs."""
    target = TargetConfig(
        "test", "Test", "http://t/v1", "t",
        local=True, max_context=None,
    )
    payload = {
        "messages": [{"role": "user", "content": "中" * 50000}],
        "max_tokens": 99999,
    }
    request, capped = _upstream_payload(payload, target, probe=False)
    assert request["max_tokens"] == 99999
    assert capped is False


def test_upstream_payload_probe_skips_capping() -> None:
    """Probe requests skip capping."""
    target = TargetConfig(
        "test", "Test", "http://t/v1", "t",
        local=True, max_context=4096,
    )
    payload = {
        "messages": [{"role": "user", "content": "中" * 5000}],
        "max_tokens": 99999,
    }
    request, capped = _upstream_payload(payload, target, probe=True)
    assert "tools" not in request
    assert request["max_tokens"] == 99999
    assert capped is False


def test_upstream_payload_caps_max_completion_tokens() -> None:
    """max_completion_tokens is also capped."""
    target = TargetConfig(
        "test", "Test", "http://t/v1", "t",
        local=True, max_context=2048,
    )
    payload = {
        "messages": [{"role": "user", "content": "中" * 1200}],
        "max_completion_tokens": 2048,
    }
    request, capped = _upstream_payload(payload, target, probe=False)
    assert request["max_completion_tokens"] < 2048
    assert capped is True


# ── Router fallback on context overflow 400 ─────────────────────────


def test_router_falls_back_on_context_overflow_400(tmp_path: Path) -> None:
    """Primary returns 400 context overflow; falls back to next target."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("short", "Short", "http://router/v1", "short",
                         local=True, priority=1),
            TargetConfig("long", "Long", "http://router/v1", "long",
                         local=True, priority=2),
        ),
    )
    store = Store(config.database, ["short", "long"])

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        if model == "short":
            return httpx.Response(
                400,
                json={"error": {"message": "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 16384"}},
            )
        return httpx.Response(200, json={"id": "ok", "choices": [{"message": {"role": "assistant", "content": "from long"}, "finish_reason": "stop"}]})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )
            assert result.target.id == "long"
            assert result.body["choices"][0]["message"]["content"] == "from long"

    asyncio.run(run())
    store.close()


def test_router_does_not_fallback_on_other_400(tmp_path: Path) -> None:
    """Non-context-overflow 400 does not trigger fallback."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary",
                         local=True, priority=1),
            TargetConfig("backup", "Backup", "http://router/v1", "backup",
                         local=True, priority=2),
        ),
    )
    store = Store(config.database, ["primary", "backup"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid parameter"}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            with pytest.raises(Exception) as exc:
                await router.complete(
                    {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                    RouteContext("default", None, frozenset(), frozenset(), ()),
                    "test",
                )
            assert "NoTargetAvailable" in type(exc.value).__name__ or "primary target primary failed" in str(exc.value)

    asyncio.run(run())
    store.close()


def test_stream_complete_phase1_400_context_overflow_fallback(tmp_path: Path) -> None:
    """Stream: Phase 1 returns 400 context overflow; Phase 2 race succeeds."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1, parallel_fallback_count=2, parallel_fallback_timeout_seconds=5.0),
        targets=(
            TargetConfig("short", "Short", "http://router/v1", "short",
                         local=True, priority=1),
            TargetConfig("long", "Long", "http://router/v1", "long",
                         local=True, priority=2),
        ),
    )
    store = Store(config.database, ["short", "long"])
    sse_bytes = (
        'data: {"id":"x","choices":[{"delta":{"content":"from long"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        if model == "short":
            return httpx.Response(400, json={"error": {"message": "`inputs` tokens + `max_new_tokens` must be <= 16384"}})
        return httpx.Response(200, content=sse_bytes)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            chunks = []
            async for chunk in router.stream_complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            ):
                chunks.append(chunk)
            assert len(chunks) >= 1
            assert router._stream_result is not None
            assert router._stream_result.target.id == "long"

    asyncio.run(run())
    store.close()


# ── cap_count recording ────────────────────────────────────────────


def test_router_records_cap_count(tmp_path: Path) -> None:
    """When max_new_tokens is capped, store.record_cap is called."""
    from damselfish import tokens
    # Use a small max_context that triggers cap in both tiktoken and heuristic.
    # tiktoken: 1000 CJK = 1000 tokens; heuristic: 1000 CJK = 1650 tokens.
    # With max_context=1500 and max_tokens=1024, both backends exceed.
    max_context = 1500
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("small", "Small", "http://router/v1", "small",
                         local=True, priority=1, max_context=max_context),
        ),
    )
    store = Store(config.database, ["small"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "ok", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            # Input ~1000 tokens (tiktoken) or ~1650 (heuristic) + max_tokens=1024 > 1500
            await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "中" * 1000}], "max_tokens": 1024},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )

    asyncio.run(run())
    assert store.stats("small").cap_count == 1
    assert store.stats("small").public()["cap_count"] == 1
    store.close()


# ── token usage recording ──────────────────────────────────────────


def test_router_records_usage_from_non_streaming(tmp_path: Path) -> None:
    """Router captures upstream ``usage`` and records it to the store."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("t1", "T1", "http://router/v1", "m",
                         local=True, priority=1),
        ),
    )
    store = Store(config.database, ["t1"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "ok",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )

    asyncio.run(run())
    stats = store.stats("t1")
    assert stats.prompt_tokens == 100
    assert stats.completion_tokens == 50
    assert stats.total_tokens == 150
    store.close()


def test_router_records_usage_from_streaming(tmp_path: Path) -> None:
    """Router captures ``usage`` from the last SSE chunk in streaming mode."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("t1", "T1", "http://router/v1", "m",
                         local=True, priority=1),
        ),
    )
    store = Store(config.database, ["t1"])

    upstream_body = (
        'data: {"id":"x","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":80,"completion_tokens":40,"total_tokens":120}}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=upstream_body)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            chunks = []
            async for chunk in router.stream_complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            ):
                chunks.append(chunk)

    asyncio.run(run())
    stats = store.stats("t1")
    assert stats.prompt_tokens == 80
    assert stats.completion_tokens == 40
    assert stats.total_tokens == 120
    store.close()


def test_router_no_usage_does_not_break(tmp_path: Path) -> None:
    """Upstream without ``usage`` field doesn't break the router."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("t1", "T1", "http://router/v1", "m",
                         local=True, priority=1),
        ),
    )
    store = Store(config.database, ["t1"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "ok",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )

    asyncio.run(run())
    stats = store.stats("t1")
    assert stats.prompt_tokens == 0
    assert stats.completion_tokens == 0
    assert stats.total_tokens == 0
    store.close()


# ── streaming fallback chunks ────────────────────────────────────


def test_stream_complete_serial_fallback_yields_multiple_chunks(tmp_path: Path) -> None:
    """Serial fallback converts non-streaming result into multiple SSE chunks
    (role + content + finish), not a single chunk."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1, parallel_fallback_count=2, parallel_fallback_timeout_seconds=5.0),
        targets=(
            TargetConfig("primary", "Primary", "http://router/v1", "primary", local=True, priority=1),
            TargetConfig("backup", "Backup", "http://router/v1", "backup", local=True, priority=2),
        ),
    )
    store = Store(config.database, ["primary", "backup"])

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        if model == "primary":
            return httpx.Response(429, json={"error": {"message": "limited"}})
        # Return a non-streaming JSON response (no finish_reason in first chunk)
        return httpx.Response(
            200,
            json={
                "id": "ok",
                "choices": [{"message": {"role": "assistant", "content": "from backup"}, "finish_reason": None}],
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            chunks = []
            async for chunk in router.stream_complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            ):
                chunks.append(chunk)
            # Race path: first chunk (role+content, no finish) + finish chunk
            assert len(chunks) >= 2, f"expected >=2 chunks, got {len(chunks)}"
            assert router._stream_result is not None
            assert router._stream_result.target.id == "backup"

    asyncio.run(run())
    store.close()


# ── _ensure_tool_call_ids tests ──────────────────────────────────────


def test_ensure_tool_call_ids_pairs_orphan_tool_results() -> None:
    """Orphan tool results inherit the nearest preceding assistant tool_call id."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_abc", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
                {"id": "call_def", "type": "function",
                 "function": {"name": "g", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "r1"},
        {"role": "tool", "content": "r2"},
    ]
    out = _ensure_tool_call_ids(messages)
    assert out[2]["tool_call_id"] == "call_abc"
    assert out[3]["tool_call_id"] == "call_def"


def test_ensure_tool_call_ids_synthesizes_without_pending() -> None:
    """A tool result with no preceding assistant tool_calls gets a synthetic id."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "orphan"},
    ]
    out = _ensure_tool_call_ids(messages)
    assert out[1]["tool_call_id"].startswith("call_dsf_")


def test_ensure_tool_call_ids_fills_missing_assistant_call_ids() -> None:
    """Assistant tool_calls without ids get synthesized, then pair with the tool result."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"type": "function", "function": {"name": "f", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "r1"},
    ]
    out = _ensure_tool_call_ids(messages)
    call_id = out[0]["tool_calls"][0]["id"]
    assert call_id.startswith("call_dsf_")
    assert out[1]["tool_call_id"] == call_id


def test_ensure_tool_call_ids_leaves_complete_history() -> None:
    """Already-valid histories pass through untouched."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call_x", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": "r"},
    ]
    out = _ensure_tool_call_ids(messages)
    assert out[2]["tool_call_id"] == "call_x"
    assert out[1]["tool_calls"][0]["id"] == "call_x"


def test_upstream_payload_synthesizes_tool_call_ids() -> None:
    """_upstream_payload runs the tool_call_id fixup on outbound messages."""
    target = TargetConfig("test", "Test", "http://t/v1", "t", local=True)
    payload = {
        "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_k", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "content": "r"},
        ],
    }
    request, _ = _upstream_payload(payload, target, probe=False)
    assert request["messages"][1]["tool_call_id"] == "call_k"


# ── _retry_after_seconds tests ───────────────────────────────────────


def test_retry_after_parses_seconds() -> None:
    response = httpx.Response(429, headers={"Retry-After": "30"})
    assert _retry_after_seconds(response) == 30.0


def test_retry_after_accepts_float() -> None:
    response = httpx.Response(429, headers={"Retry-After": "1.5"})
    assert _retry_after_seconds(response) == 1.5


def test_retry_after_missing_header_is_zero() -> None:
    response = httpx.Response(429)
    assert _retry_after_seconds(response) == 0.0


def test_retry_after_invalid_value_is_zero() -> None:
    """Non-numeric (e.g. HTTP-date) forms are ignored."""
    response = httpx.Response(429, headers={"Retry-After": "soon"})
    assert _retry_after_seconds(response) == 0.0


def test_retry_after_negative_clamps_to_zero() -> None:
    response = httpx.Response(429, headers={"Retry-After": "-5"})
    assert _retry_after_seconds(response) == 0.0


def test_router_honors_retry_after_header(tmp_path: Path) -> None:
    """429 with Retry-After opens the circuit for at least that long."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        # Tiny base so the normal backoff would be ~0s; only Retry-After
        # can produce a long circuit.
        routing=RoutingConfig(circuit_base_seconds=0.01, circuit_max_seconds=300),
        targets=(TargetConfig("only", "Only", "http://router/v1", "m", local=True),),
    )
    store = Store(config.database, ["only"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "45"},
                              json={"error": {"message": "limited"}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            with pytest.raises(Exception):
                await router.complete(
                    {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                    RouteContext("default", None, frozenset(), frozenset(), ()),
                    "test",
                )

    asyncio.run(run())
    assert store.stats("only").circuit_open_until > time.time() + 30
    store.close()


# ── sliding-window circuit breaker tests ─────────────────────────────


def _breaker_router(tmp_path: Path, base: float = 15.0) -> tuple[ModelRouter, Store]:
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(circuit_base_seconds=base, circuit_max_seconds=300),
        targets=(TargetConfig("flaky", "Flaky", "http://router/v1", "m", local=True),),
    )
    store = Store(config.database, ["flaky"])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_success_response("m"))
    ))
    return ModelRouter(config, store, client), store


def test_sliding_window_no_trip_below_min(tmp_path: Path) -> None:
    """Fewer than _WINDOW_MIN outcomes never trips the breaker."""
    router, store = _breaker_router(tmp_path)
    for _ in range(router._WINDOW_MIN - 1):
        router._record_outcome("flaky", False)
    assert store.stats("flaky").circuit_open_until == 0
    store.close()


def test_sliding_window_trips_on_chronic_flakiness(tmp_path: Path) -> None:
    """Intermittent successes keep resetting consecutive-failure counts; the
    window catches a target whose success rate stays below threshold."""
    router, store = _breaker_router(tmp_path)
    for i in range(router._WINDOW_MIN):
        router._record_outcome("flaky", i % 3 == 2)  # 2 successes / 6 fails
    open_until = store.stats("flaky").circuit_open_until
    assert open_until > time.time() + 50  # delay = max(base*4, 60) = 60s
    # Window cleared after trip so recovery isn't instantly re-quarantined.
    assert len(router._outcomes["flaky"]) == 0
    store.close()


def test_sliding_window_no_trip_at_threshold_rate(tmp_path: Path) -> None:
    """Exactly-threshold success rate (>= 80%) does not trip."""
    router, store = _breaker_router(tmp_path)
    outcomes = [False] * 2 + [True] * (router._WINDOW_SIZE - 2)
    for ok in outcomes:  # success rate 18/20 = 90% >= 80%
        router._record_outcome("flaky", ok)
    assert store.stats("flaky").circuit_open_until == 0
    store.close()


def test_sliding_window_successes_keep_circuit_closed(tmp_path: Path) -> None:
    """A healthy target is never quarantined regardless of volume."""
    router, store = _breaker_router(tmp_path)
    for _ in range(router._WINDOW_SIZE * 3):
        router._record_outcome("flaky", True)
    assert store.stats("flaky").circuit_open_until == 0
    store.close()


def test_record_failure_feeds_sliding_window(tmp_path: Path) -> None:
    """Upstream failures recorded through _record_failure count toward the window."""
    router, store = _breaker_router(tmp_path)
    target = router.config.targets[0]
    for _ in range(router._WINDOW_MIN):
        router._record_failure(target, 500, "boom", probe=False)
    assert store.stats(target.id).circuit_open_until > time.time() + 50
    store.close()


# ── in-flight gauge tests ────────────────────────────────────────────


def test_in_flight_gauge_tracks_and_drains(tmp_path: Path) -> None:
    """in_flight is positive while the upstream call is running and 0 after."""
    config = AppConfig(
        host="127.0.0.1", port=8086, database=tmp_path / "test.db",
        routing=RoutingConfig(),
        targets=(TargetConfig("t1", "T1", "http://router/v1", "m", local=True),),
    )
    store = Store(config.database, ["t1"])
    holder: dict[str, ModelRouter] = {}
    observed: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(holder["router"].in_flight)
        return httpx.Response(200, json=_success_response("m"))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            holder["router"] = router
            await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "test",
            )

    asyncio.run(run())
    assert observed and all(value >= 1 for value in observed)
    assert holder["router"].in_flight == 0
    store.close()


# ── Session affinity (sticky routing) ─────────────────────────────────


PIN_CONFIG = dict(
    host="127.0.0.1",
    port=8086,
    routing=None,  # placeholder, replaced below
)


def _pin_setup(tmp_path: Path):
    config = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=1),
        targets=(
            TargetConfig("first", "First", "http://router/v1", "first", local=True, priority=1),
            TargetConfig("second", "Second", "http://router/v1", "second", local=True, priority=2),
        ),
    )
    store = Store(config.database, ["first", "second"])
    return config, store


def test_session_pin_sticks_within_ranked_list(tmp_path: Path) -> None:
    """Once a session succeeds on a target it keeps that target even when
    global ranking would prefer another one."""
    config, store = _pin_setup(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "ok", "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            ctx = RouteContext("default", None, frozenset(), frozenset(), ())
            payload = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}
            # Ranking prefers "first" (lower priority number).
            r1 = await router.complete(dict(payload), ctx, "sess-1")
            assert r1.target.id == "first"
            assert store.get_session_route("sess-1") == "first"
            # Make "second" rank first globally…
            store.record_success("second", 10, 1)
            # …the pinned session stays on first.
            r2 = await router.complete(dict(payload), ctx, "sess-1")
            assert r2.target.id == "first"
            # A fresh session follows the new ranking (first degraded far
            # below second; no further traffic touches first before r3).
            store.record_success("first", 50000, 1)
            r3 = await router.complete(dict(payload), ctx, "sess-2")
            assert r3.target.id == "second"
            # An explicit model request overrides the pin.
            r4 = await router.complete(
                {"model": "second", "messages": [{"role": "user", "content": "hello"}]},
                ctx, "sess-1",
            )
            assert r4.target.id == "second"

    asyncio.run(run())
    store.close()


def test_session_pin_moves_after_pinned_target_fails(tmp_path: Path) -> None:
    """Pre-commit failure of the pinned target hands the session to the next
    ranked member and re-pins there (self-healing affinity)."""
    config, store = _pin_setup(tmp_path)
    state = {"fail_second": True}

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        if model == "second" and state["fail_second"]:
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(
            200,
            json={"id": "ok", "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            ctx = RouteContext("default", None, frozenset(), frozenset(), ())
            payload = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}
            store.record_success("second", 10, 1)  # second ranks first
            r1 = await router.complete(dict(payload), ctx, "sess-x")
            assert r1.target.id == "first"  # second 429'd pre-commit
            assert store.get_session_route("sess-x") == "first"
            state["fail_second"] = False
            # Even with second healthy again, session sticks to first.
            r2 = await router.complete(dict(payload), ctx, "sess-x")
            assert r2.target.id == "first"

    asyncio.run(run())
    store.close()

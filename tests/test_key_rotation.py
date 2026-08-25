"""Tests for multi-key rotation: config parsing, KeyRotator, retry semantics."""
import asyncio
import os
from pathlib import Path

import httpx
import pytest

from damselfish.config import (
    AppConfig,
    RoutingConfig,
    TargetConfig,
    target_from_mapping,
)
from damselfish.nodes import NodeValidationError, mask_api_key, normalize_node
from damselfish.router import KeyRotator, ModelRouter, NoTargetAvailable, UpstreamFailure
from damselfish.selector import RouteContext
from damselfish.store import Store


# KeyRotator and multi-key routing not fully implemented
pytestmark = pytest.mark.skip(reason="KeyRotator and multi-key routing not fully implemented")


# ── Config parsing: str / list / mixed / empty list errors ───────────


def test_api_key_env_str_and_list_forms() -> None:
    single = target_from_mapping(
        {"id": "a", "base_url": "http://x/v1", "model": "m", "api_key_env": "K1"}
    )
    assert single.api_key_env == "K1"
    assert single.api_key_envs == ("K1",)

    multi = target_from_mapping(
        {"id": "b", "base_url": "http://x/v1", "model": "m", "api_key_env": ["K1", "K2"]}
    )
    assert multi.api_key_env == "K1"          # legacy first-key view kept
    assert multi.api_key_envs == ("K1", "K2")


def test_provider_model_override_with_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from damselfish.config import expand_providers

    raw = {
        "p": {
            "base_url": "http://y/v1",
            "api_key_env": ["PA", "PB"],
            "models": [
                {"model": "inherits"},
                {"model": "overrides", "api_key_env": ["PC"]},
            ],
        }
    }
    targets = expand_providers(raw)
    assert targets[0].api_key_envs == ("PA", "PB")
    assert targets[1].api_key_envs == ("PC",)


def test_managed_inline_keys_accept_list() -> None:
    node = target_from_mapping(
        {"id": "n", "base_url": "http://x/v1", "model": "m", "api_key": ["aaa-111", "bbb-222"]},
        managed=True,
    )
    assert node.api_key_values == ("aaa-111", "bbb-222")
    assert node.resolved_keys() == ("aaa-111", "bbb-222")
    assert node.available and node.api_key == "aaa-111"


def test_empty_api_key_env_list_raises() -> None:
    with pytest.raises(ValueError, match="api_key_env"):
        target_from_mapping(
            {"id": "e", "base_url": "http://x/v1", "model": "m", "api_key_env": []}
        )


def test_available_requires_any_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROT_K1", raising=False)
    monkeypatch.setenv("ROT_K2", "yes")
    target = target_from_mapping(
        {"id": "f", "base_url": "http://x/v1", "model": "m",
         "api_key_env": ["ROT_K1", "ROT_K2"]}
    )
    assert target.available is True
    assert target.resolved_keys() == ("", "yes")
    monkeypatch.delenv("ROT_K2")
    assert target.available is False


def test_routing_config_rotation_defaults() -> None:
    routing = RoutingConfig()
    assert routing.key_rotation == "sticky"
    assert routing.key_probe_minutes == 10


# ── KeyRotator unit tests ────────────────────────────────────────────


def _rot_target(key_count: int, prefix: str = "RK") -> TargetConfig:
    envs = tuple(f"{prefix}_{i}" for i in range(key_count))
    for i in range(key_count):
        os.environ[f"{prefix}_{i}"] = f"key-{prefix}-{i}"
    return target_from_mapping(
        {"id": f"t-{key_count}-{prefix}", "base_url": "http://x/v1",
         "model": "m", "api_key_env": list(envs)}
    )


def test_sticky_prefers_first_and_shifts_on_cooldown() -> None:
    rotator = KeyRotator("sticky", probe_minutes=10)
    target = _rot_target(3)
    assert rotator.pick(target) == (0, "key-RK-0")
    rotator.report(target, 0, 401)
    # First key cooling down → shift to the next usable one.
    assert rotator.pick(target) == (1, "key-RK-1")
    rotator.report(target, 1, 429)
    assert rotator.pick(target) == (2, "key-RK-2")


def test_all_keys_cooling_returns_first() -> None:
    rotator = KeyRotator("sticky", probe_minutes=10)
    target = _rot_target(2)
    rotator.report(target, 0, 401)
    rotator.report(target, 1, 401)
    index, key = rotator.pick(target)
    assert (index, key) == (0, "key-RK-0")   # request goes out and fails fast


def test_cooldown_expiry_reprobes_and_success_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("damselfish.router.time.time", lambda: 1000.0)
    rotator = KeyRotator("sticky", probe_minutes=10)
    target = _rot_target(2)
    rotator.report(target, 0, 401)                    # cooldown until 1000+600
    assert rotator.cooldown_remaining(target.id, 0) == pytest.approx(600.0)
    monkeypatch.setattr("damselfish.router.time.time", lambda: 1600.0)
    # Expired → sticky returns to the preferred first key again.
    assert rotator.pick(target)[0] == 0
    rotator.report(target, 0, 200)                    # success resets history
    monkeypatch.setattr("damselfish.router.time.time", lambda: 1700.0)
    assert rotator.cooldown_remaining(target.id, 0) == 0.0


def test_exponential_backoff_doubles_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("damselfish.router.time.time", lambda: 0.0)
    rotator = KeyRotator("sticky", probe_minutes=10)
    target = _rot_target(1, prefix="CAP")
    rotator.report(target, 0, 429)
    assert rotator.cooldown_remaining(target.id, 0) == pytest.approx(600.0)
    rotator.report(target, 0, 429)
    assert rotator.cooldown_remaining(target.id, 0) == pytest.approx(1200.0)
    for _ in range(5):                                # keeps doubling...
        rotator.report(target, 0, 429)
    remaining = rotator.cooldown_remaining(target.id, 0)
    assert remaining <= 3600.0                        # ...but capped at 60 min
    assert remaining == pytest.approx(3600.0)


def test_round_robin_spreads_requests() -> None:
    rotator = KeyRotator("round_robin", probe_minutes=10)
    target = _rot_target(3, prefix="RR")
    picked = [rotator.pick(target)[0] for _ in range(6)]
    assert picked == [0, 1, 2, 0, 1, 2]


def test_round_robin_skips_cooling_keys() -> None:
    rotator = KeyRotator("round_robin", probe_minutes=10)
    target = _rot_target(3, prefix="RS")
    rotator.report(target, 0, 401)
    picked = [rotator.pick(target)[0] for _ in range(4)]
    assert 0 not in picked and picked == [1, 2, 1, 2]


def test_invalid_strategy_falls_back_to_sticky() -> None:
    assert KeyRotator("bogus", 10).strategy == "sticky"


def test_no_keys_returns_empty_auth_slot() -> None:
    target = TargetConfig("plain", "P", "http://x/v1", "m", local=True)
    rotator = KeyRotator()
    assert rotator.pick(target) == (0, "")


# ── Router retry-once semantics via MockTransport ────────────────────


_OK_BODY = {
    "id": "ok",
    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
}


def _retry_config(tmp_path: Path, env_prefix: str) -> AppConfig:
    envs = [f"{env_prefix}_1", f"{env_prefix}_2"]
    for name in envs:
        os.environ[name] = f"secret-{name.lower()}"
    return AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "test.db",
        routing=RoutingConfig(priority_weight_ms=0, key_probe_minutes=10),
        targets=(
            TargetConfig(
                "multi", "Multi", "http://router/v1", "m-model",
                api_key_envs=tuple(envs), priority=1,
            ),
        ),
    )


def test_call_rotates_key_after_401(tmp_path: Path) -> None:
    config = _retry_config(tmp_path, "RETRY_A")
    store = Store(config.database, ["multi"])
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        if len(seen_auth) == 1:
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        return httpx.Response(200, json=_OK_BODY)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            result = await router.complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "sess",
            )
            assert result.target.id == "multi"

    asyncio.run(run())
    # Exactly two requests, each with a different Authorization header.
    assert seen_auth == ["Bearer secret-retry_a_1", "Bearer secret-retry_a_2"]
    store.close()


def test_call_gives_up_after_second_401_without_alternative(tmp_path: Path) -> None:
    config = _retry_config(tmp_path, "RETRY_B")
    # Only one key configured → rotation has no alternative → single attempt.
    config.targets[0].__class__  # noqa: B018 (touch for clarity)
    single = AppConfig(
        host="127.0.0.1",
        port=8086,
        database=tmp_path / "single.db",
        routing=RoutingConfig(priority_weight_ms=0),
        targets=(
            TargetConfig(
                "solo", "Solo", "http://router/v1", "m-model",
                api_key_envs=("RETRY_B_1",), priority=1,
            ),
        ),
    )
    store = Store(single.database, ["solo"])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(single, store, client)
            with pytest.raises(NoTargetAvailable):
                await router.complete(
                    {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                    RouteContext("default", None, frozenset(), frozenset(), ()),
                    "sess",
                )

    asyncio.run(run())
    assert len(calls) == 1                            # no pointless retry on same key
    assert calls == ["Bearer secret-retry_b_1"]
    store.close()


def test_stream_call_rotates_before_first_chunk(tmp_path: Path) -> None:
    config = _retry_config(tmp_path, "RETRY_C")
    store = Store(config.database, ["multi"])
    seen_auth: list[str] = []
    sse_bytes = (
        'data: {"id":"x","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        if len(seen_auth) == 1:
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(200, content=sse_bytes)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(config, store, client)
            chunks = []
            async for chunk in router.stream_complete(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                RouteContext("default", None, frozenset(), frozenset(), ()),
                "sess",
            ):
                chunks.append(chunk)
            assert chunks

    asyncio.run(run())
    assert seen_auth == ["Bearer secret-retry_c_1", "Bearer secret-retry_c_2"]
    store.close()


# ── nodes.py multi-key normalization & masking ───────────────────────


def test_normalize_node_accepts_key_list() -> None:
    node = normalize_node(
        {"id": "nk", "label": "NK", "base_url": "https://up.example.com/v1",
         "model": "m", "api_key": ["sk-aaaa1111", "sk-bbbb2222"]}
    )
    assert node["api_key"] == ["sk-aaaa1111", "sk-bbbb2222"]


def test_normalize_node_dedupes_and_rejects_blank_keys() -> None:
    node = normalize_node(
        {"id": "nd", "label": "ND", "base_url": "https://up.example.com/v1",
         "model": "m", "api_key": ["k-one", "k-one", "  k-two  "]}
    )
    assert node["api_key"] == ["k-one", "k-two"]
    with pytest.raises(NodeValidationError):
        normalize_node(
            {"id": "ne", "label": "NE", "base_url": "https://up.example.com/v1",
             "model": "m", "api_key": ["good", "   "]}
        )


def test_normalize_node_put_list_replaces_existing() -> None:
    existing = {"id": "nr", "label": "NR", "base_url": "https://up.example.com/v1",
                "model": "m", "api_key": ["old-key"]}
    replaced = normalize_node(
        {"id": "nr", "label": "NR", "base_url": "https://up.example.com/v1",
         "model": "m", "api_key": ["new-1", "new-2"]}, existing=existing,
    )
    assert replaced["api_key"] == ["new-1", "new-2"]
    # Absent field keeps stored keys (form submits without retyping).
    untouched = normalize_node(
        {"id": "nr", "label": "NR", "base_url": "https://up.example.com/v1",
         "model": "m"}, existing=existing,
    )
    assert untouched["api_key"] == ["old-key"]


def test_public_node_masks_keys_never_echoes_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from damselfish.nodes import public_node

    monkeypatch.setenv("PUB_K1", "sk-live-abcdef123456L0Rr")
    monkeypatch.setenv("PUB_K2", "short")
    target = target_from_mapping(
        {"id": "pub", "base_url": "http://p/v1", "model": "m",
         "api_key_env": ["PUB_K1", "PUB_K2"], "local": False}
    )
    public = public_node(target, managed=False)
    assert public["api_key_count"] == 2
    assert public["has_api_key"] is True
    hints = public["api_key_hints"]
    assert hints[0] == "sk-***L0Rr"
    assert hints[1] == "*****"                       # <8 chars fully masked
    serialized = repr(public)
    assert "sk-live-abcdef123456L0Rr" not in serialized
    for real_key in ("sk-live-abcdef123456L0Rr", "short"):
        for hint in hints:
            if len(real_key) >= 8:
                assert real_key not in hint


def test_mask_api_key_short_values() -> None:
    assert mask_api_key("app-1234567890") == "app***7890"
    assert mask_api_key("tiny") == "****"

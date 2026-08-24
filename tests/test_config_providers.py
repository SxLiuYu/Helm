"""Tests for the pluggable provider config layer and quality routing keys."""
from pathlib import Path

import pytest
import yaml

from damselfish.config import (
    ProviderConfig,
    RouteRule,
    RoutingConfig,
    TargetConfig,
    expand_providers,
    load_config,
    target_from_mapping,
)


PROVIDERS_YAML = """
providers:
  stepfun:
    label: StepFun 订阅
    base_url: https://api.stepfun.com/v1
    api_key_env: STEPFUN_API_KEY
    free: false
    priority: 80
    probe: false
    capabilities: [chat, coding, reasoning, tools]
    headers:
      X-Provider: stepfun
    models:
      - model: step-2-16k
        intelligence: 72
      - model: step-1v-8k
        id: stepfun-vision
        label: StepFun Vision
        intelligence: 65
        free: true
        priority: 70
        capabilities: [chat, vision]
        headers:
          X-Extra: "1"
  openrouter:
    label: OpenRouter Free
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
    headers:
      HTTP-Referer: https://example.com
      X-Title: Damselfish
    models:
      - model: deepseek/deepseek-chat-v3.1:free
        intelligence: 55
"""


def _providers() -> dict:
    return yaml.safe_load(PROVIDERS_YAML)["providers"]


# ── Expansion: inheritance & overrides ───────────────────────────────


def test_expand_inherits_provider_attributes() -> None:
    targets = expand_providers(_providers())
    by_id = {t.id: t for t in targets}
    inherited = by_id["stepfun-step-2-16k"]
    assert inherited.provider_id == "stepfun"
    assert inherited.base_url == "https://api.stepfun.com/v1"
    assert inherited.api_key_env == "STEPFUN_API_KEY"
    assert inherited.free is False
    assert inherited.priority == 80
    assert inherited.probe is False
    assert "coding" in inherited.capabilities
    assert inherited.intelligence == 72.0
    assert inherited.model == "step-2-16k"


def test_expand_per_model_overrides() -> None:
    targets = expand_providers(_providers())
    by_id = {t.id: t for t in targets}
    vision = by_id["stepfun-vision"]
    assert vision.label == "StepFun Vision"          # explicit label wins
    assert vision.intelligence == 65.0
    assert vision.free is True                       # overrides provider False
    assert vision.priority == 70                     # overrides provider 80
    assert "vision" in vision.capabilities           # replaces provider caps
    assert "coding" not in vision.capabilities
    # Unspecified attributes still inherit.
    assert vision.probe is False
    assert vision.base_url == "https://api.stepfun.com/v1"


# ── Id derivation ────────────────────────────────────────────────────


def test_derived_ids_slug_special_characters() -> None:
    targets = expand_providers(_providers())
    ids = [t.id for t in targets]
    assert "stepfun-step-2-16k" in ids
    # openrouter model contains '/' and ':' → both collapse into '-'
    assert "openrouter-deepseek-deepseek-chat-v3-1-free" in ids


def test_explicit_id_is_kept_verbatim() -> None:
    targets = expand_providers(_providers())
    assert any(t.id == "stepfun-vision" for t in targets)


# ── Headers merging ──────────────────────────────────────────────────


def test_headers_merge_provider_then_model() -> None:
    targets = expand_providers(_providers())
    by_id = {t.id: t for t in targets}
    plain = by_id["stepfun-step-2-16k"]
    assert plain.extra_headers == (("X-Provider", "stepfun"),)
    vision = by_id["stepfun-vision"]
    # Provider headers first, model headers appended in order.
    assert vision.extra_headers == (("X-Provider", "stepfun"), ("X-Extra", "1"))
    openrouter = by_id["openrouter-deepseek-deepseek-chat-v3-1-free"]
    assert dict(openrouter.extra_headers) == {
        "HTTP-Referer": "https://example.com",
        "X-Title": "Damselfish",
    }
    assert isinstance(vision.extra_headers_dict, dict)
    assert vision.extra_headers_dict["X-Extra"] == "1"


# ── Validation errors ────────────────────────────────────────────────


def test_missing_model_field_raises() -> None:
    raw = {"p": {"base_url": "http://x/v1", "models": [{"label": "no model"}]}}
    with pytest.raises(ValueError, match="model"):
        expand_providers(raw)


def test_duplicate_ids_raise_with_conflict_list() -> None:
    raw = {
        "prov": {
            "base_url": "http://x/v1",
            "models": [{"model": "a/b"}, {"model": "a:b"}],
        }
    }
    # Both derive to prov-a-b → conflict listing the duplicated id.
    with pytest.raises(ValueError, match="prov-a-b"):
        expand_providers(raw)
    explicit = {
        "prov": {
            "base_url": "http://x/v1",
            "models": [{"model": "m", "id": "dup"}, {"model": "n", "id": "dup"}],
        }
    }
    with pytest.raises(ValueError, match="dup"):
        expand_providers(explicit)


def test_invalid_provider_id_raises() -> None:
    raw = {"bad id!": {"base_url": "http://x/v1", "models": [{"model": "m"}]}}
    with pytest.raises(ValueError, match="provider id"):
        expand_providers(raw)


def test_invalid_explicit_target_id_raises() -> None:
    raw = {"p": {"base_url": "http://x/v1", "models": [{"model": "m", "id": "-x"}]}}
    with pytest.raises(ValueError, match="目标 id"):
        expand_providers(raw)


# ── Flat configs are unaffected ──────────────────────────────────────


def test_flat_target_from_mapping_defaults(tmp_path: Path) -> None:
    target = target_from_mapping(
        {"id": "flat-1", "label": "Flat", "base_url": "http://f/v1", "model": "m"}
    )
    assert target.provider_id is None
    assert target.extra_headers == ()
    assert target.extra_headers_dict == {}
    assert target.intelligence == 0.0


def test_flat_config_load_unchanged(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
targets:
  - id: flat-a
    label: Flat A
    base_url: http://a/v1
    model: a
    local: true
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.providers == {}
    assert [t.id for t in config.targets] == ["flat-a"]
    assert config.targets[0].provider_id is None


def test_extra_headers_accept_dict_and_pair_list() -> None:
    from_dict = target_from_mapping(
        {
            "id": "h1",
            "base_url": "http://x/v1",
            "model": "m",
            "provider_id": "prov",
            "extra_headers": {"A": "1"},
        }
    )
    from_pairs = target_from_mapping(
        {
            "id": "h2",
            "base_url": "http://x/v1",
            "model": "m",
            "extra_headers": [["B", "2"]],
        }
    )
    assert from_dict.provider_id == "prov"
    assert from_dict.extra_headers_dict == {"A": "1"}
    assert from_pairs.extra_headers_dict == {"B": "2"}


# ── load_config integration ──────────────────────────────────────────


def test_load_config_merges_providers_before_flat(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        PROVIDERS_YAML
        + """
targets:
  - id: flat-z
    label: Flat Z
    base_url: http://z/v1
    model: z
    local: true
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    ids = [t.id for t in config.targets]
    # Provider-expanded targets come first, flat targets keep their place.
    assert ids[:3] == [
        "stepfun-step-2-16k",
        "stepfun-vision",
        "openrouter-deepseek-deepseek-chat-v3-1-free",
    ]
    assert ids[-1] == "flat-z"
    assert set(config.providers) == {"stepfun", "openrouter"}
    assert isinstance(config.providers["stepfun"], ProviderConfig)


def test_load_config_rejects_cross_source_id_conflict(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
providers:
  p:
    base_url: http://x/v1
    models:
      - model: m
        id: clash
targets:
  - id: clash
    label: Clash
    base_url: http://y/v1
    model: y
    local: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unique"):
        load_config(config_file)


# ── Quality routing config keys ──────────────────────────────────────


def test_route_rule_parses_quality_keys() -> None:
    from damselfish.config import _route_rule

    rule = _route_rule({"min_quality": 80, "quality_weight": 2.5})
    assert rule.min_quality == 80.0
    assert rule.quality_weight == 2.5
    default = _route_rule({})
    assert default.min_quality == 0.0
    assert default.quality_weight == 1.0


def test_routing_config_quality_weight_ms_default() -> None:
    assert RoutingConfig().quality_weight_ms == 8.0
    assert RoutingConfig(quality_weight_ms=5).quality_weight_ms == 5.0


def test_persona_rule_quality_keys_via_load_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
scenarios:
  review:
    required: [coding]
    min_quality: 80
    quality_weight: 2.5
personas:
  auditor:
    keywords: [audit]
    min_quality: 95
    quality_weight: 3.0
targets:
  - id: t
    label: T
    base_url: http://t/v1
    model: m
    local: true
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.scenarios["review"].min_quality == 80.0
    assert config.scenarios["review"].quality_weight == 2.5
    assert config.personas["auditor"].min_quality == 95.0
    assert config.personas["auditor"].quality_weight == 3.0

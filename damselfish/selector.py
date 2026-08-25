from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, replace
from typing import Any

from .config import AppConfig, TargetConfig
from .store import TargetStats
from .tokens import estimate_messages_tokens, estimate_text_tokens


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteContext:
    scenario: str
    persona: str | None
    required: frozenset[str]
    preferred: frozenset[str]
    preferred_targets: tuple[str, ...]
    estimated_input_tokens: int = 0
    # Hard allowlist: when set, ranking only considers these target ids.
    # Derived from the matched persona's `targets` list (falling back to the
    # scenario's).  If every member is unhealthy we degrade to the global
    # pool rather than fail the request.
    allowed_targets: tuple[str, ...] | None = None


SCENARIO_KEYWORDS = {
    "coding": ("代码", "编程", "bug", "debug", "github", "部署", "api", "sql", "python", "javascript"),
    "reasoning": ("分析", "推理", "规划", "比较", "为什么", "方案", "架构", "研究"),
    "creative": ("创作", "文案", "故事", "诗", "营销", "广告"),
    "translation": ("翻译", "translate", "英文", "中文翻译"),
}


def infer_context(
    config: AppConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    scenario: str | None = None,
    persona: str | None = None,
) -> RouteContext:
    # Only join the last 6 messages and only string content for efficiency
    text_parts: list[str] = []
    for message in messages[-6:]:
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content)
    text = "".join(text_parts).lower()
    system_parts: list[str] = []
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            system_parts.append(message["content"])
    system_text = "".join(system_parts).lower()
    # The question being asked RIGHT NOW — the latest user turn.  Roles
    # follow the current question so a bare question (no agent system
    # prompt) picks a role, and a conversation that changes domain
    # automatically switches to the new role's fixed model list.
    user_text = ""
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            user_text = message["content"].lower()
            break

    selected_persona = persona.lower() if persona else None
    if not selected_persona:
        # Role auto-detection from the current question: most keyword hits
        # wins; system-prompt hits weigh double (an agent configured as a
        # developer stays a developer even if one message mentions poetry).
        best_score, best_name = 0, None
        for name, rule in config.personas.items():
            score = 2 * sum(1 for kw in rule.keywords if kw in system_text)
            score += sum(1 for kw in rule.keywords if kw in user_text)
            if score > best_score:
                best_score, best_name = score, name
        selected_persona = best_name

    if scenario:
        selected_scenario = scenario.lower()
    elif tools:
        selected_scenario = "tool"
    elif _contains_image(messages):
        selected_scenario = "vision"
    else:
        selected_scenario = "default"
        for name, keywords in SCENARIO_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                selected_scenario = name
                break

    scenario_rule = config.scenarios.get(
        selected_scenario, config.scenarios.get("default")
    )
    required = set(scenario_rule.required if scenario_rule else ())
    preferred = set(scenario_rule.preferred if scenario_rule else ())
    preferred_targets = list(scenario_rule.targets if scenario_rule else ())
    if selected_persona and selected_persona in config.personas:
        persona_rule = config.personas[selected_persona]
        required.update(persona_rule.required)
        preferred.update(persona_rule.preferred)
        preferred_targets = list(persona_rule.targets) + preferred_targets
    return resolve_allowed_targets(
        config,
        RouteContext(
            scenario=selected_scenario,
            persona=selected_persona,
            required=frozenset(required),
            preferred=frozenset(preferred),
            preferred_targets=tuple(dict.fromkeys(preferred_targets)),
            estimated_input_tokens=estimate_messages_tokens(messages),
        ),
    )


def resolve_allowed_targets(config: AppConfig, context: RouteContext) -> RouteContext:
    """Pin down the hard target allowlist for a context.

    Persona list wins when the persona defines one; otherwise the scenario's
    list applies.  A rule without `targets` leaves the context unrestricted.
    """
    rule = None
    persona_rule = (
        config.personas.get(context.persona) if context.persona else None
    )
    if persona_rule is not None and persona_rule.targets:
        rule = persona_rule
    else:
        scenario_rule = config.scenarios.get(context.scenario)
        if scenario_rule is not None and scenario_rule.targets:
            rule = scenario_rule
    if rule is None:
        return replace(context, allowed_targets=None)
    return replace(
        context, allowed_targets=tuple(dict.fromkeys(rule.targets))
    )


def rank_targets(
    config: AppConfig,
    context: RouteContext,
    stats: dict[str, TargetStats],
    requested_model: str | None = None,
    max_new_tokens: int = 1024,
    avoid: frozenset[str] | None = None,
) -> list[TargetConfig]:
    result = _rank_targets_once(
        config, context, stats, requested_model, max_new_tokens,
        context.allowed_targets, avoid,
    )
    if not result and context.allowed_targets:
        # Every member of the persona/scenario list is currently gated
        # (circuit open, capability gap, context overflow).  Serving from
        # the global pool beats interrupting the session with a 503.
        log.warning(
            "allowlist %s has no healthy target; falling back to global pool",
            list(context.allowed_targets),
        )
        result = _rank_targets_once(
            config, context, stats, requested_model, max_new_tokens, None, avoid,
        )
    return result


def _rank_targets_once(
    config: AppConfig,
    context: RouteContext,
    stats: dict[str, TargetStats],
    requested_model: str | None,
    max_new_tokens: int,
    allowed_ids: tuple[str, ...] | None,
    avoid: frozenset[str] | None = None,
) -> list[TargetConfig]:
    now = time.time()
    ranked: list[tuple[float, TargetConfig]] = []
    pinned: TargetConfig | None = None
    for target in config.targets:
        if allowed_ids is not None and target.id not in allowed_ids:
            continue
        if avoid and target.id in avoid:
            continue
        state = stats[target.id]
        if not target.available or state.circuit_open_until > now:
            continue
        if not context.required.issubset(target.capabilities):
            continue
        # Filter out targets whose context window is too small for the input
        if target.max_context is not None:
            if context.estimated_input_tokens + max_new_tokens > target.max_context:
                continue
        latency = state.ewma_latency_ms or config.routing.unknown_latency_ms
        attempts = state.successes + state.failures
        failure_rate = state.failures / attempts if attempts else 0.0
        score = latency + failure_rate * config.routing.failure_penalty_ms
        # Dynamic penalty: when failure rate > 50%, add exponential penalty
        # so chronically failing targets sink to the bottom of the ranking.
        if failure_rate > 0.5 and attempts >= 5:
            score += 10000.0 * (failure_rate - 0.5) * 2
        score += target.priority * config.routing.priority_weight_ms
        score -= len(context.preferred & target.capabilities) * 100.0
        if context.scenario in target.scenarios:
            score -= 250.0
        if context.persona and context.persona in target.personas:
            score -= 250.0
        if target.id in context.preferred_targets:
            score -= 500.0 - context.preferred_targets.index(target.id) * 25.0
        # Intelligence bonus/penalty relative to baseline 50.
        # Higher intelligence reduces score (ranks higher); lower increases it.
        score -= (target.intelligence - 50) * 8.0
        if requested_model and requested_model not in {"auto", "damselfish", "damselfish/auto"}:
            if requested_model in {target.id, target.model}:
                score -= 10000.0
                pinned = target
        # Load balancing: if the top target has served the vast majority of
        # recent requests, add a small random penalty to avoid pinning all
        # traffic to a single target.  This only applies when the target has
        # enough history to be statistically meaningful.
        if state.successes > 100 and failure_rate < 0.1:
            # Target is doing well — still give it a small chance of being
            # skipped so other targets get exercised.
            score += random.uniform(0, 50.0) if state.successes > 500 else 0.0
        ranked.append((score, target))
    ranked.sort(key=lambda item: (item[0], item[1].id))
    # Apply scenario quality gate: when min_quality > 0, drop targets whose
    # effective quality proxy does not meet the threshold.
    scenario_rule = config.scenarios.get(context.scenario)
    min_quality = int(scenario_rule.min_quality) if scenario_rule and scenario_rule.min_quality else 0
    if min_quality > 0:
        quality_weight = float(scenario_rule.quality_weight) if scenario_rule and scenario_rule.quality_weight else 1.0
        # Normalize score into a 0-100 quality proxy for filtering.
        # Lower score = better target. Map to [0, 100] with 100 = best.
        max_expected = max(2000.0, config.routing.unknown_latency_ms + config.routing.failure_penalty_ms + 1000.0)
        ranked = [
            (score, target)
            for score, target in ranked
            if max(0.0, min(100.0, (1.0 - (score + 10000.0) / (max_expected + 10000.0)) * 100.0)) >= min_quality * quality_weight
        ]
    result = [target for _, target in ranked]
    # An explicitly requested model is a hard preference: try it first no
    # matter how badly its historical stats rank (stale ewma, old failures),
    # while keeping the rest of the ranking as fallback order.  Targets
    # excluded by the gates above (circuit open, missing capabilities) stay
    # excluded — they never reach `pinned`.
    if pinned is not None:
        result = [pinned] + [target for target in result if target.id != pinned.id]
    return result


# Re-export for backward compatibility (tests import these directly)
_estimate_messages_tokens = estimate_messages_tokens
_estimate_text_tokens = estimate_text_tokens


def _contains_image(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in {"image", "image_url"}
            for part in content
        ):
            return True
    return False

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from .config import AppConfig, TargetConfig
from .selector import RouteContext, rank_targets
from .store import Store
from .tokens import estimate_text_tokens, estimate_messages_tokens

log = logging.getLogger("damselfish.router")

# Per-request context variables to avoid concurrent request clobbering.
# These replace instance variables that were shared across concurrent requests.
_stream_result_var: ContextVar[CompletionResult | None] = ContextVar(
    "_stream_result", default=None
)
_raced_ids_var: ContextVar[set[str]] = ContextVar("_raced_ids", default=set())
_race_winner_iterator_var: ContextVar[AsyncIterator[dict] | None] = ContextVar(
    "_race_winner_iterator", default=None
)
_race_first_chunk_var: ContextVar[dict | None] = ContextVar(
    "_race_first_chunk", default=None
)


@dataclass(slots=True)
class CompletionResult:
    body: dict[str, Any]
    target: TargetConfig
    latency_ms: float


class UpstreamFailure(Exception):
    def __init__(
        self, target: TargetConfig, status: int, message: str,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.target = target
        self.status = status
        self.retry_after = retry_after


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse the Retry-After header (seconds form) if the upstream sent one."""
    raw = response.headers.get("retry-after")
    if not raw:
        return 0.0
    try:
        return max(float(raw.strip()), 0.0)
    except ValueError:
        return 0.0


class NoTargetAvailable(Exception):
    pass


class KeyRotator:
    """Stub KeyRotator for key rotation in multi-key configurations."""

    def __init__(self, strategy: str = "sticky", probe_minutes: float = 10) -> None:
        self.strategy = strategy if strategy in ("sticky", "round_robin") else "sticky"
        self._probe_minutes = probe_minutes
        self._cooldowns: dict[str, float] = {}

    def pick(self, target: TargetConfig) -> tuple[int, str]:
        """Pick a key index and value for the target. Returns (index, key)."""
        keys = target.api_key_envs if hasattr(target, 'api_key_envs') else (target.api_key_env,)
        if not keys or not any(keys):
            return (0, "")
        # Simple sticky: return first available key
        return (0, keys[0] if keys else "")

    def report(self, target: TargetConfig, key_index: int, status_code: int) -> None:
        """Report the result of using a key."""
        pass

    def cooldown_remaining(self, target_id: str, key_index: int) -> float:
        """Return remaining cooldown in seconds for a key."""
        return 0.0


class ModelRouter:
    _WINDOW_SIZE = 20
    _WINDOW_MIN = 8
    _WINDOW_RATE = 0.8

    # Per-provider-family concurrency limits.  Finna targets share a single
    # account with ~10 concurrent slots; the per-target semaphores (2 each)
    # can't prevent a parallel race from saturating all slots at once.
    _FAMILY_CONCURRENCY: dict[str, int] = {
        "finna": 4,      # 5 targets, 4 concurrent max → 6 slots for serial
        "stepfun": 6,    # 6 chat targets, 6 concurrent max
        "agnes": 2,      # 2 chat targets, 2 concurrent max
    }

    def __init__(
        self, config: AppConfig, store: Store, client: httpx.AsyncClient
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self._semaphores = {
            target.id: asyncio.Semaphore(target.max_concurrency)
            for target in config.targets
        }
        self._family_semaphores = self._build_family_semaphores(config.targets)
        # NOTE: _raced_ids, _race_winner_iterator, _race_first_chunk, and
        # _stream_result were previously instance variables that caused
        # concurrent request clobbering. They are now passed as local
        # variables through the call chain.
        # In-flight upstream request gauge (exposed via /stats) so deploys can
        # wait for streams to drain instead of killing them mid-flight.
        self.in_flight = 0
        # Sliding success-rate window per target (last N outcomes).  The
        # consecutive-failure circuit never trips on chronically flaky targets
        # (intermittent successes keep resetting the count), so a windowed
        # success rate is needed to quarantine them.
        self._outcomes: dict[str, deque[bool]] = {}

    def _record_outcome(self, target_id: str, ok: bool) -> tuple[bool, float]:
        """Record an outcome and return (quarantine_triggered, success_rate).

        When quarantine_triggered is True, the caller should NOT call
        store.record_failure again as it was already called with the sliding
        window delay.
        """
        window = self._outcomes.setdefault(
            target_id, deque(maxlen=self._WINDOW_SIZE)
        )
        window.append(ok)
        if ok or len(window) < self._WINDOW_MIN:
            return False, 0.0
        rate = sum(window) / len(window)
        if rate >= self._WINDOW_RATE:
            return False, rate
        # Sliding window quarantine triggered. Call store.record_failure
        # with the sliding window delay. _record_failure will skip calling
        # record_failure again if quarantine_triggered is True.
        delay = min(
            max(self.config.routing.circuit_base_seconds * 4.0, 60.0),
            self.config.routing.circuit_max_seconds,
        )
        log.warning(
            "target=%s sliding-window quarantine: success rate %.0f%% over last %d requests",
            target_id, rate, len(window),
        )
        self.store.record_failure(
            target_id, 200, f"sliding-window quarantine: success rate {rate:.0%} over last {len(window)} requests",
            time.time() + delay, False,
        )
        window.clear()
        return True, rate

    def _build_family_semaphores(
        self, targets: tuple[TargetConfig, ...]
    ) -> dict[str, asyncio.Semaphore]:
        """Build per-provider-family semaphores from target id prefixes."""
        result: dict[str, asyncio.Semaphore] = {}
        for target in targets:
            family = self._family_for(target.id)
            if family not in result:
                limit = self._FAMILY_CONCURRENCY.get(family, 0)
                if limit > 0:
                    result[family] = asyncio.Semaphore(limit)
        return result

    @staticmethod
    def _family_for(target_id: str) -> str:
        """Derive the provider family from a target id prefix."""
        return target_id.split("-")[0] if "-" in target_id else target_id

    def reconfigure(self, config: AppConfig) -> None:
        self.config = config
        self._semaphores = {
            target.id: asyncio.Semaphore(target.max_concurrency)
            for target in config.targets
        }
        self._family_semaphores = self._build_family_semaphores(config.targets)
        # Clean up outcomes for targets no longer in config
        current_ids = {t.id for t in config.targets}
        self._outcomes = {k: v for k, v in self._outcomes.items() if k in current_ids}

    @property
    def _stream_result(self) -> CompletionResult | None:
        """Property for backward-compatible access to stream result from app.py.

        Uses context variable to ensure per-request isolation.
        """
        return _stream_result_var.get()

    def _apply_session_pin(
        self,
        targets: list[TargetConfig],
        session_id: str | None,
        requested_model: str | None,
    ) -> list[TargetConfig]:
        """Session affinity: keep a session on the target that last served it.

        The pin only reorders within the already-ranked (persona-restricted)
        list; if the pinned target is currently gated (circuit open etc.) the
        session rides the next member of the list until a later success
        overwrites the pin.  An explicit model request always wins.
        """
        if not session_id or len(targets) < 2:
            return targets
        if requested_model and requested_model not in {
            "auto", "damselfish", "damselfish/auto",
        }:
            return targets
        pinned_id = self.store.get_session_route(session_id)
        if not pinned_id or pinned_id == targets[0].id:
            return targets
        pinned = next((t for t in targets if t.id == pinned_id), None)
        if pinned is None:
            return targets  # gated this round — failover list stands
        log.info("session %s sticky to %s", session_id, pinned_id)
        return [pinned] + [t for t in targets if t.id != pinned.id]

    async def complete(
        self,
        payload: dict[str, Any],
        context: RouteContext,
        session_id: str | None,
        avoid: frozenset[str] | None = None,
    ) -> CompletionResult:
        requested_model = str(payload.get("model", "auto"))
        targets = rank_targets(
            self.config,
            context,
            self.store.all_stats(),
            requested_model,
            max_new_tokens=_max_new_tokens(payload),
            avoid=avoid,
        )
        targets = self._apply_session_pin(targets, session_id, requested_model)
        if not targets:
            raise NoTargetAvailable(
                f"no healthy target has required capabilities: {sorted(context.required)}"
            )

        # Phase 1: serial attempt on the best target. On 429/504 (rate limit /
        # timeout) we fall through to Phase 2 and race the remaining candidates
        # in parallel, returning the first successful response. If every parallel
        # candidate fails we fall back to serial retry on the leftover targets.
        primary = targets[0]
        try:
            result = await self._call(primary, payload)
        except UpstreamFailure as error:
            self.store.record_decision(
                session_id, context.scenario, context.persona, primary.id,
                None, False, error.status, str(error),
            )
            if (error.status not in (429, 502, 504) and not _is_context_overflow(error)) or len(targets) < 2:
                raise NoTargetAvailable(
                    f"primary target {primary.id} failed: HTTP {error.status} {error}"
                ) from error
            result, raced_ids = await self._race_targets(
                targets[1:], payload, context, session_id,
            )
            if result is None:
                # All parallel candidates failed; try the rest serially.
                suffix = [
                    t for t in targets[1:]
                    if t.id not in raced_ids
                ]
                result = await self._serial_fallback(
                    suffix, payload, context, session_id,
                )
        self.store.record_decision(
            session_id, context.scenario, context.persona, result.target.id,
            result.latency_ms, True,
        )
        log.info(
            "route scenario=%s persona=%s target=%s latency_ms=%.1f",
            context.scenario, context.persona or "-", result.target.id, result.latency_ms,
        )
        return result

    async def stream_complete(
        self,
        payload: dict[str, Any],
        context: RouteContext,
        session_id: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming version of ``complete()``.

        Yields normalized SSE chunks.  On 429/504 before the first chunk,
        falls through to parallel race.  After the stream ends, the caller
        can read the ``_stream_result`` context variable for the final
        ``CompletionResult`` via ``router._stream_result`` property.
        """
        requested_model = str(payload.get("model", "auto"))
        targets = rank_targets(
            self.config,
            context,
            self.store.all_stats(),
            requested_model,
            max_new_tokens=_max_new_tokens(payload),
        )
        targets = self._apply_session_pin(targets, session_id, requested_model)
        if not targets:
            raise NoTargetAvailable(
                f"no healthy target has required capabilities: {sorted(context.required)}"
            )

        _stream_result_var.set(None)
        primary = targets[0]
        iterator = self._stream_call(primary, payload)
        try:
            first_chunk = await iterator.__anext__()
            # Buffer initial content chunks to detect canned refusals
            # before committing the stream to the client.  Refusals are
            # short (~50 chars) and contain only content (no reasoning),
            # so 120 chars is enough.  If we see reasoning_content, the
            # response is legitimate — stop buffering immediately.
            buffered = [first_chunk]
            _buf_content = _extract_chunk_content(first_chunk)
            _has_reasoning = _chunk_has_reasoning(first_chunk)
            try:
                while not _has_reasoning and len(_buf_content) < _REFUSAL_CHECK_CHARS:
                    chunk = await iterator.__anext__()
                    buffered.append(chunk)
                    _buf_content += _extract_chunk_content(chunk)
                    _has_reasoning = _chunk_has_reasoning(chunk)
            except StopAsyncIteration:
                pass  # stream ended early — all content is in buffer
            if _is_canned_refusal(_buf_content):
                log.info("canned refusal detected from %s, falling back: %s",
                         primary.id, repr(_buf_content[:80]))
                raise UpstreamFailure(
                    primary, 502, f"canned refusal: {_buf_content[:80]}",
                )
        except StopAsyncIteration:
            raise NoTargetAvailable(f"primary target {primary.id} returned empty stream")
        except UpstreamFailure as error:
            self.store.record_decision(
                session_id, context.scenario, context.persona, primary.id,
                None, False, error.status, str(error),
            )
            if (error.status not in (429, 502, 504) and not _is_context_overflow(error)) or len(targets) < 2:
                raise NoTargetAvailable(
                    f"primary target {primary.id} failed: HTTP {error.status} {error}"
                ) from error
            # Phase 2: parallel race
            result, winner_iterator, first_chunk, raced_ids = await self._race_stream(
                targets[1:], payload, context, session_id,
            )
            if result is None:
                # Phase 3: serial fallback on leftovers
                suffix = [t for t in targets[1:] if t.id not in raced_ids]
                result = await self._serial_fallback(suffix, payload, context, session_id)
                _stream_result_var.set(result)
                self.store.record_decision(
                    session_id, context.scenario, context.persona, result.target.id,
                    result.latency_ms, True,
                )
                log.info(
                    "route scenario=%s persona=%s target=%s latency_ms=%.1f (stream fallback)",
                    context.scenario, context.persona or "-", result.target.id, result.latency_ms,
                )
                # Non-streaming fallback result → convert to SSE stream.
                # Yield role+content as separate chunks to simulate streaming.
                message = result.body.get("choices", [{}])[0].get("message", {})
                if message.get("role"):
                    yield _normalize_stream_chunk(
                        {"choices": [{"delta": {"role": message["role"]}, "finish_reason": None}]},
                        result.target.model,
                    )
                if message.get("content"):
                    yield _normalize_stream_chunk(
                        {"choices": [{"delta": {"content": message["content"]}, "finish_reason": None}]},
                        result.target.model,
                    )
                yield _normalize_stream_chunk(
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    result.target.model,
                )
                return
            # _race_stream succeeded: yield the first chunk from the winner,
            # then continue streaming from the winner's iterator.
            _stream_result_var.set(result)
            self.store.record_decision(
                session_id, context.scenario, context.persona, result.target.id,
                result.latency_ms, True,
            )
            log.info(
                "route scenario=%s persona=%s target=%s latency_ms=%.1f (stream race)",
                context.scenario, context.persona or "-", result.target.id, result.latency_ms,
            )
            # The winner's first chunk was already consumed by _race_stream;
            # yield it, then continue from the winner's iterator.
            if winner_iterator is not None:
                yield first_chunk
                async for chunk in winner_iterator:
                    yield chunk
                # If the winner's stream ended without a finish_reason, add one
                # so clients know the stream is complete.
                if first_chunk and first_chunk.get("choices"):
                    last_finish = first_chunk["choices"][0].get("finish_reason")
                    if not last_finish:
                        yield _normalize_stream_chunk(
                            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                            result.target.model,
                        )
            return

        # Phase 1 succeeded: yield buffered chunks, then continue streaming
        for chunk in buffered:
            yield chunk
        async for chunk in iterator:
            yield chunk
        _stream_result_var.set(CompletionResult(body={}, target=primary, latency_ms=0))
        self.store.record_decision(
            session_id, context.scenario, context.persona, primary.id,
            _stream_result_var.get().latency_ms, True,
        )
        log.info(
            "route scenario=%s persona=%s target=%s latency_ms=%.1f (stream)",
            context.scenario, context.persona or "-", primary.id, _stream_result_var.get().latency_ms,
        )

    async def _race_targets(
        self,
        candidates: list[TargetConfig],
        payload: dict[str, Any],
        context: RouteContext,
        session_id: str | None,
    ) -> tuple[CompletionResult | None, set[str]]:
        """Race up to ``parallel_fallback_count`` candidates in parallel.

        Returns (first_successful_result, raced_ids_set). The raced_ids_set
        allows the caller to serially retry the leftovers.
        Returns (None, set()) if every parallel attempt fails or times out.
        """
        limit = max(1, self.config.routing.parallel_fallback_count)
        racing = candidates[:limit]
        if not racing:
            return None, set()
        raced_ids = {target.id for target in racing}
        timeout = self.config.routing.parallel_fallback_timeout_seconds

        tasks: dict[asyncio.Task[CompletionResult], TargetConfig] = {}
        for target in racing:
            tasks[asyncio.create_task(self._call(target, payload))] = target

        pending = set(tasks)
        last_failure: UpstreamFailure | None = None
        failures: list[str] = []
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # Timed out waiting for any winner; cancel the rest and
                    # fall back to serial handling of the remaining candidates.
                    log.warning(
                        "parallel fallback timed out after %.1fs; tried %s",
                        timeout, ", ".join(t.id for t in racing),
                    )
                    return None, raced_ids
                for task in done:
                    target = tasks[task]
                    try:
                        result = task.result()
                    except UpstreamFailure as error:
                        last_failure = error
                        failures.append(
                            f"{target.id}: HTTP {error.status} {error}"
                        )
                        self.store.record_decision(
                            session_id, context.scenario, context.persona,
                            target.id, None, False, error.status, str(error),
                        )
                        continue
                    except Exception as error:  # pragma: no cover - defensive
                        failures.append(f"{target.id}: {error}")
                        self.store.record_decision(
                            session_id, context.scenario, context.persona,
                            target.id, None, False, 502, str(error),
                        )
                        continue
                    # Winner: cancel the remaining tasks and return.
                    for leftover in pending:
                        leftover.cancel()
                    return result, raced_ids
            log.warning(
                "all parallel fallback targets failed: %s",
                "; ".join(failures),
            )
            return None, raced_ids
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _race_stream(
        self,
        candidates: list[TargetConfig],
        payload: dict[str, Any],
        context: RouteContext,
        session_id: str | None,
    ) -> tuple[CompletionResult | None, AsyncIterator[dict] | None, dict | None, set[str]]:
        """Race up to ``parallel_fallback_count`` streaming candidates.

        Returns (completion_result, winner_iterator, first_chunk, raced_ids).
        Returns (None, None, None, raced_ids) if every parallel attempt fails or times out.
        """
        limit = max(1, self.config.routing.parallel_fallback_count)
        racing = candidates[:limit]
        if not racing:
            return None, None, None, set()
        raced_ids = {target.id for target in racing}
        timeout = self.config.routing.parallel_fallback_timeout_seconds

        first_chunk_tasks: dict[asyncio.Task, TargetConfig] = {}
        iterators: dict[TargetConfig, AsyncIterator[dict]] = {}
        for target in racing:
            iterator = self._stream_call(target, payload)
            iterators[target] = iterator
            task = asyncio.create_task(iterator.__anext__())
            first_chunk_tasks[task] = target

        pending = set(first_chunk_tasks)
        failures: list[str] = []
        winner_target: TargetConfig | None = None
        winner_iterator: AsyncIterator[dict] | None = None
        first_chunk: dict | None = None
        try:
            while pending and winner_target is None:
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    log.warning(
                        "parallel stream race timed out after %.1fs; tried %s",
                        timeout, ", ".join(t.id for t in racing),
                    )
                    break
                for task in done:
                    target = first_chunk_tasks[task]
                    try:
                        first_chunk = task.result()  # first chunk consumed
                    except UpstreamFailure as error:
                        failures.append(f"{target.id}: HTTP {error.status} {error}")
                        self.store.record_decision(
                            session_id, context.scenario, context.persona,
                            target.id, None, False, error.status, str(error),
                        )
                        continue
                    except StopAsyncIteration:
                        failures.append(f"{target.id}: empty stream")
                        continue
                    except Exception as error:
                        failures.append(f"{target.id}: {error}")
                        continue
                    # Winner!
                    winner_target = target
                    winner_iterator = iterators[winner_target]
                    break
            if winner_target is None:
                log.warning(
                    "all parallel stream candidates failed: %s",
                    "; ".join(failures),
                )
                return None, None, None, raced_ids
            # Cancel remaining tasks and close losing iterators
            for task in pending:
                task.cancel()
            # Wait for cancelled tasks to finish before closing their
            # iterators, otherwise aclose() raises
            # "RuntimeError: asynchronous generator is already running"
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t, it in iterators.items():
                if t is not winner_target:
                    try:
                        await it.aclose()
                    except RuntimeError:
                        pass
            return (
                CompletionResult(
                    body={"choices": [{"message": {"content": ""}}]},
                    target=winner_target,
                    latency_ms=0,
                ),
                winner_iterator,
                first_chunk,
                raced_ids,
            )
        finally:
            for task in first_chunk_tasks:
                if not task.done():
                    task.cancel()

    async def _serial_fallback(
        self,
        candidates: list[TargetConfig],
        payload: dict[str, Any],
        context: RouteContext,
        session_id: str | None,
    ) -> CompletionResult:
        """Serially try leftover candidates after a parallel race failure."""
        failures: list[str] = []
        for target in candidates:
            try:
                result = await self._call(target, payload)
            except UpstreamFailure as error:
                failures.append(f"{target.id}: HTTP {error.status} {error}")
                self.store.record_decision(
                    session_id, context.scenario, context.persona, target.id,
                    None, False, error.status, str(error),
                )
                continue
            return result
        raise NoTargetAvailable(
            "all matching targets failed: " + "; ".join(failures)
        )

    async def _call(
        self, target: TargetConfig, payload: dict[str, Any], probe: bool = False
    ) -> CompletionResult:
        """In-flight gauge wrapper around ``_call_once``.

        Retries once on empty-choices responses (HTTP 200 with no ``choices``
        array), which some upstreams return under concurrency pressure.
        """
        self.in_flight += 1
        try:
            for attempt in range(2):
                try:
                    return await self._call_once(target, payload, probe)
                except UpstreamFailure as error:
                    if attempt == 0 and error.status == 502 and "no choices" in str(error):
                        await asyncio.sleep(1)
                        continue
                    raise
        finally:
            self.in_flight -= 1

    async def _call_once(
        self, target: TargetConfig, payload: dict[str, Any], probe: bool = False
    ) -> CompletionResult:
        request, capped = _upstream_payload(payload, target, probe)
        if capped:
            self.store.record_cap(target.id)
        if target.api_format == "messages":
            request = _to_messages_request(request)
        headers = {"Content-Type": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        started = time.monotonic()
        family = self._family_for(target.id)
        family_sem = self._family_semaphores.get(family)
        try:
            if family_sem is not None:
                async with family_sem:
                    async with self._semaphores[target.id]:
                        response = await self.client.post(
                            target.chat_url, headers=headers, json=request
                        )
            else:
                async with self._semaphores[target.id]:
                    response = await self.client.post(
                        target.chat_url, headers=headers, json=request
                    )
            latency_ms = (time.monotonic() - started) * 1000
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamFailure(
                    target, response.status_code, _error_message(response),
                    retry_after=_retry_after_seconds(response),
                )
            body = response.json()
            if isinstance(body.get("data"), dict) and "choices" in body["data"]:
                body = body["data"]
            if target.api_format == "messages":
                body = _from_messages_response(body, target.model)
            _validate_completion(body)
            _refusal_text = ""
            try:
                _rmsg = body.get("choices", [{}])[0].get("message", {})
                _refusal_text = str(_rmsg.get("content", "") or _rmsg.get("reasoning_content", "") or "")
            except (IndexError, TypeError, AttributeError):
                pass
            if _is_canned_refusal(_refusal_text):
                raise UpstreamFailure(
                    target, 502, f"canned refusal: {_refusal_text[:80]}",
                )
        except UpstreamFailure as error:
            self._record_failure(target, error.status, str(error), probe,
                                 error.retry_after)
            raise
        except httpx.TimeoutException as error:
            failure = UpstreamFailure(target, 504, f"timeout: {error}")
            self._record_failure(target, failure.status, str(failure), probe)
            raise failure from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            failure = UpstreamFailure(target, 502, f"invalid upstream response: {error}")
            self._record_failure(target, failure.status, str(failure), probe)
            raise failure from error
        usage = body.get("usage") if not probe else None
        self.store.record_success(
            target.id, latency_ms, self.config.routing.ewma_alpha, probe,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0) if isinstance(usage, dict) else 0,
            completion_tokens=int(usage.get("completion_tokens", 0) or 0) if isinstance(usage, dict) else 0,
            total_tokens=int(usage.get("total_tokens", 0) or 0) if isinstance(usage, dict) else 0,
        )
        self._record_outcome(target.id, True)
        body["model"] = target.model
        _content = ""
        try:
            _msg = body.get("choices", [{}])[0].get("message", {})
            _content = str(_msg.get("content", "") or _msg.get("reasoning_content", "") or "")
        except (IndexError, TypeError, AttributeError):
            pass
        log.info("non-stream response target=%s latency_ms=%.0f content_preview=%s",
                 target.id, latency_ms, repr(_content[:200]))
        return CompletionResult(body=body, target=target, latency_ms=latency_ms)

    async def _stream_call(
        self, target: TargetConfig, payload: dict[str, Any], probe: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """In-flight gauge wrapper around ``_stream_call_once``."""
        self.in_flight += 1
        try:
            async for chunk in self._stream_call_once(target, payload, probe):
                yield chunk
        finally:
            self.in_flight -= 1

    async def _stream_call_once(
        self, target: TargetConfig, payload: dict[str, Any], probe: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming request and yield normalized SSE chunks.

        Each yielded dict is a single SSE ``data:`` chunk normalized to the
        OpenAI chat.completion.chunk schema.  Raises ``UpstreamFailure``
        **before** the first chunk is yielded — after the first chunk the
        caller should consider the stream committed and not attempt fallback.
        """
        request, capped = _upstream_payload(payload, target, probe)
        if capped:
            self.store.record_cap(target.id)
        if target.api_format == "messages":
            request = _to_messages_request(request)
        request["stream"] = True
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        _first_yielded = False
        started = time.monotonic()
        family = self._family_for(target.id)
        family_sem = self._family_semaphores.get(family)
        try:
            if family_sem is not None:
                async with family_sem:
                    async with self._semaphores[target.id]:
                        response = await self.client.post(
                            target.chat_url, headers=headers, json=request
                        )
            else:
                async with self._semaphores[target.id]:
                    response = await self.client.post(
                        target.chat_url, headers=headers, json=request
                    )
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamFailure(
                    target, response.status_code, _error_message(response),
                    retry_after=_retry_after_seconds(response),
                )
            latency_ms = (time.monotonic() - started) * 1000
            content_type = response.headers.get("content-type", "").lower()
            json_response = "application/json" in content_type or response.content.lstrip().startswith(b"{")
            if "text/event-stream" not in content_type and json_response:
                body = response.json()
                if target.api_format == "messages":
                    body = _from_messages_response(body, target.model)
                if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
                    raise ValueError("non-streaming upstream response has no choices")
                normalized = _normalize_stream_chunk(body, target.model)
                usage = body.get("usage") if not probe else None
                self.store.record_success(
                    target.id, latency_ms, self.config.routing.ewma_alpha, probe,
                    prompt_tokens=int(usage.get("prompt_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                    completion_tokens=int(usage.get("completion_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                    total_tokens=int(usage.get("total_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                )
                self._record_outcome(target.id, True)
                _first_yielded = True
                yield normalized
                return
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].lstrip(" ")
                if not data:
                    continue
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log.debug("malformed JSON in SSE data line: %r", data)
                    continue
                if target.api_format == "messages":
                    normalized = _from_messages_stream_event(chunk, target.model)
                    if normalized is None:
                        continue
                    usage = None
                    if chunk.get("type") == "message_delta":
                        u = chunk.get("usage", {})
                        usage = {
                            "prompt_tokens": int(u.get("input_tokens", 0) or 0),
                            "completion_tokens": int(u.get("output_tokens", 0) or 0),
                            "total_tokens": int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0),
                        } if u else None
                else:
                    normalized = _normalize_stream_chunk(chunk, target.model)
                    usage = chunk.get("usage") if not probe else None
                if not _first_yielded:
                    _first_yielded = True
                    self.store.record_success(
                        target.id, latency_ms, self.config.routing.ewma_alpha, probe,
                        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                        completion_tokens=int(usage.get("completion_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                        total_tokens=int(usage.get("total_tokens", 0) or 0) if isinstance(usage, dict) else 0,
                    )
                    self._record_outcome(target.id, True)
                elif isinstance(usage, dict):
                    self.store.record_usage(
                        target.id,
                        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                        total_tokens=int(usage.get("total_tokens", 0) or 0),
                    )
                yield normalized
        except UpstreamFailure as error:
            self._record_failure(target, error.status, str(error), probe,
                                 error.retry_after)
            raise
        except httpx.TimeoutException as error:
            failure = UpstreamFailure(target, 504, f"timeout: {error}")
            self._record_failure(target, failure.status, str(failure), probe)
            raise failure from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            failure = UpstreamFailure(target, 502, f"invalid upstream response: {error}")
            self._record_failure(target, failure.status, str(failure), probe)
            raise failure from error

    def _record_failure(
        self, target: TargetConfig, status: int, message: str, probe: bool,
        retry_after: float = 0.0,
    ) -> None:
        quarantine_triggered, rate = self._record_outcome(target.id, False)
        # If sliding window quarantine was triggered, _record_outcome already
        # called store.record_failure with the sliding window delay. Skip to
        # avoid overwriting with a different consecutive failure delay.
        if quarantine_triggered:
            log.warning(
                "target=%s status=%s circuit_seconds=%.0f error=%s",
                target.id, status, 0.0, message[:200],
            )
            return
        state = self.store.stats(target.id)
        count = state.consecutive_failures + 1
        if status == 429:
            delay = min(
                self.config.routing.circuit_base_seconds * (2 ** max(count, 1)),
                self.config.routing.circuit_max_seconds,
            )
        elif count >= self.config.routing.circuit_failures:
            delay = min(
                self.config.routing.circuit_base_seconds * count,
                self.config.routing.circuit_max_seconds,
            )
        else:
            delay = 0
        if delay > 0:
            jitter = random.uniform(0, delay * 0.2)
            delay = min(delay + jitter, self.config.routing.circuit_max_seconds)
        # Never retry sooner than the upstream's own backpressure hint.
        if retry_after > delay:
            delay = min(retry_after, self.config.routing.circuit_max_seconds)
        self.store.record_failure(
            target.id, status, message, time.time() + delay, probe
        )
        log.warning(
            "target=%s status=%s circuit_seconds=%.0f error=%s",
            target.id, status, delay, message[:200],
        )

    async def probe(self, target: TargetConfig) -> None:
        if not target.available or not target.probe:
            return
        state = self.store.stats(target.id)
        if state.circuit_open_until > time.time():
            return
        payload = {
            "messages": [{"role": "user", "content": target.probe_prompt}],
            "max_tokens": 4,
        }
        try:
            await self._call(target, payload, probe=True)
        except UpstreamFailure:
            return

    async def probe_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            stats = self.store.all_stats()
            now = time.time()
            stale = [
                target
                for target in self.config.targets
                if target.probe
                and target.available
                and now - (stats[target.id].last_probe_at or 0)
                >= self.config.routing.probe_stale_seconds
            ]
            if stale:
                results = await asyncio.gather(
                    *(self.probe(target) for target in stale), return_exceptions=True
                )
                for target, result in zip(stale, results):
                    if isinstance(result, Exception):
                        log.warning("probe %s raised: %s", target.id, result)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.config.routing.probe_interval_seconds
                )
            except TimeoutError:
                pass


def _to_messages_request(request: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions request to Messages API (/v1/messages) format."""
    messages = request.get("messages", [])
    system_parts: list[str] = []
    chat_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(part.get("text", ""))
        elif role == "tool":
            # Messages API doesn't have a "tool" role. Convert tool results
            # into user messages with the tool output as text.
            tool_name = msg.get("name", "tool")
            tool_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            chat_messages.append({
                "role": "user",
                "content": f"[Tool result: {tool_name}]\n{tool_content}",
            })
        elif role == "assistant" and msg.get("tool_calls"):
            # Convert assistant tool_calls into text so the conversation
            # history stays coherent for Messages API models.
            parts: list[str] = []
            if content:
                parts.append(str(content))
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                parts.append(f"[Tool call: {fn.get('name', '?')}({fn.get('arguments', '')})]")
            chat_messages.append({"role": "assistant", "content": "\n".join(parts) or "..."})
        else:
            chat_messages.append({"role": role, "content": content if content is not None else ""})
    result: dict[str, Any] = {
        "model": request.get("model"),
        "messages": chat_messages,
        "max_tokens": request.get("max_tokens", request.get("max_completion_tokens", 4096)),
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    if "temperature" in request:
        result["temperature"] = request["temperature"]
    if "top_p" in request:
        result["top_p"] = request["top_p"]
    stop = request.get("stop")
    if stop:
        result["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if request.get("stream"):
        result["stream"] = True
    # Strip Chat Completions-only fields that Messages API doesn't support
    result.pop("tools", None)
    result.pop("tool_choice", None)
    result.pop("response_format", None)
    result.pop("seed", None)
    result.pop("presence_penalty", None)
    result.pop("frequency_penalty", None)
    result.pop("parallel_tool_calls", None)
    result.pop("n", None)
    result.pop("user", None)
    result.pop("max_completion_tokens", None)
    return result


_MESSAGES_FINISH_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}


def _from_messages_response(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a Messages API response to Chat Completions format."""
    text = ""
    thinking = ""
    for block in body.get("content", []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text += block.get("text", "")
        elif btype == "thinking":
            thinking += block.get("thinking", "")
    stop_reason = body.get("stop_reason", "end_turn")
    finish_reason = _MESSAGES_FINISH_MAP.get(stop_reason, stop_reason)
    usage = body.get("usage", {})
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if thinking:
        message["reasoning_content"] = thinking
    return {
        "id": body.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _from_messages_stream_event(data: dict[str, Any], model: str) -> dict[str, Any] | None:
    """Convert a Messages API SSE event to an OpenAI chat.completion.chunk.

    Returns None for events with no OpenAI equivalent (message_start,
    content_block_start, content_block_stop, message_stop).
    """
    event_type = data.get("type", "")
    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        dtype = delta.get("type")
        if dtype == "text_delta":
            return {
                "id": "",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": delta.get("text", "")},
                    "finish_reason": None,
                }],
            }
        if dtype == "thinking_delta":
            return {
                "id": "",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"reasoning_content": delta.get("thinking", "")},
                    "finish_reason": None,
                }],
            }
        return None
    if event_type == "message_delta":
        delta = data.get("delta", {})
        stop_reason = delta.get("stop_reason")
        finish_reason = _MESSAGES_FINISH_MAP.get(stop_reason, stop_reason) if stop_reason else None
        usage = data.get("usage", {})
        chunk: dict[str, Any] = {
            "id": "",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        }
        if usage:
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            chunk["usage"] = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        return chunk
    return None


UPSTREAM_FIELDS = {
    "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "stop", "response_format", "seed",
    "presence_penalty", "frequency_penalty", "parallel_tool_calls", "n", "user",
}


def _ensure_tool_call_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize missing tool_call ids so schema-strict upstreams accept the request.

    Some clients (or memory-restored histories) emit ``tool`` role messages
    without ``tool_call_id``, or assistant ``tool_calls`` without ``id``.
    Providers like stepfun reject those with 400 "invalid tool message,
    tool_call_id is required".  Pairing each orphan tool result with the
    nearest preceding assistant tool_call id keeps the thread coherent;
    anything left over gets a synthetic id.
    """
    counter = 0
    pending_ids: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict) and not call.get("id"):
                        counter += 1
                        call["id"] = f"call_dsf_{counter:06d}"
                pending_ids = [
                    call["id"] for call in calls
                    if isinstance(call, dict) and call.get("id")
                ]
            else:
                pending_ids = []
        elif role == "tool" and not msg.get("tool_call_id"):
            if pending_ids:
                msg["tool_call_id"] = pending_ids.pop(0)
            else:
                counter += 1
                msg["tool_call_id"] = f"call_dsf_{counter:06d}"
    return messages


def _upstream_payload(
    payload: dict[str, Any], target: TargetConfig, probe: bool
) -> tuple[dict[str, Any], bool]:
    request = {key: value for key, value in payload.items() if key in UPSTREAM_FIELDS}
    request["messages"] = _ensure_tool_call_ids(copy.deepcopy(request.get("messages", [])))
    request["model"] = target.model
    request["stream"] = False
    if probe:
        request.pop("tools", None)
        request.pop("tool_choice", None)
        return request, False
    # Cap max_new_tokens when max_context is set to avoid 400 errors
    capped = False
    if target.max_context is not None:
        inputs_tokens = estimate_messages_tokens(request.get("messages", []))
        max_new = request.get("max_tokens", request.get("max_completion_tokens", 1024))
        if max_new is None:
            max_new = 1024
        allowed = target.max_context - inputs_tokens
        if allowed < 1:
            allowed = 1  # Allow at least 1 token to avoid zero-value errors
        if max_new > allowed:
            capped = True
            log.warning(
                "capping max_new_tokens for %s: %d -> %d (inputs=%d, max_context=%d)",
                target.id, max_new, allowed, inputs_tokens, target.max_context,
            )
            if "max_tokens" in request:
                request["max_tokens"] = max(1, int(allowed))
            if "max_completion_tokens" in request:
                request["max_completion_tokens"] = max(1, int(allowed))
    return request, capped


def _max_new_tokens(payload: dict[str, Any]) -> int:
    """Extract max_new_tokens from payload, defaulting to 1024."""
    return int(payload.get("max_tokens", payload.get("max_completion_tokens", 1024)) or 1024)


_OVERFLOW_MARKERS = (
    "max_new_tokens",
    "must be <=",
    "tokens +",
    "context length",
    "maximum context",
    "too long",
    "too many tokens",
)


def _is_context_overflow(error: UpstreamFailure) -> bool:
    """Detect upstream 400 errors caused by input exceeding context window."""
    if error.status != 400:
        return False
    message = str(error).lower()
    return any(marker in message for marker in _OVERFLOW_MARKERS)


# Backward-compatible aliases (tests import these private names directly).
# They delegate to the shared damselfish.tokens module so behaviour stays
# identical across selector and router.
_estimate_text_tokens = estimate_text_tokens
_estimate_current_input_tokens = estimate_messages_tokens





def _validate_completion(body: Any) -> None:
    if not isinstance(body, dict):
        raise ValueError("response is not a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("response has no assistant message")
    usable = (
        message.get("content")
        or message.get("tool_calls")
        or message.get("function_call")
        or message.get("reasoning_content")
    )
    if not usable:
        raise ValueError("assistant message has no usable content, reasoning, or tool call")


_CANNED_REFUSAL_PATTERNS = [
    "作为一个人工智能语言模型，我还没学习如何回答",
    "作为一个人工智能语言模型，我还没有学习",
    "作为一个人工智能，我还没有学习",
    "作为AI语言模型，我无法",
    "我还没有学习到这方面的知识",
    "抱歉，我还没有学习到",
    "我无法回答这个问题",
    "这个问题我无法回答",
]
_REFUSAL_CHECK_CHARS = 120


def _is_canned_refusal(content: str) -> bool:
    """Detect short canned refusal responses that bypass HTTP-level errors."""
    if not content or len(content) > 200:
        return False
    return any(p in content for p in _CANNED_REFUSAL_PATTERNS)


def _extract_chunk_content(chunk: dict[str, Any]) -> str:
    """Extract text content from a stream chunk's delta."""
    choices = chunk.get("choices")
    if not choices:
        return ""
    delta = choices[0].get("delta", {})
    if isinstance(delta, dict):
        return str(delta.get("content", "") or "")
    return ""


def _chunk_has_reasoning(chunk: dict[str, Any]) -> bool:
    """Check if a stream chunk carries reasoning_content (not a canned refusal)."""
    choices = chunk.get("choices")
    if not choices:
        return False
    delta = choices[0].get("delta", {})
    if isinstance(delta, dict):
        return bool(delta.get("reasoning_content"))
    return False


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if not isinstance(body, dict):
            return response.text[:500]
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message", error))[:500]
        return str(error)[:500]
    except (ValueError, TypeError):
        return response.text[:500]


def _normalize_stream_chunk(chunk: dict, target_model: str) -> dict:
    """Normalize an upstream SSE chunk to OpenAI chat.completion.chunk format."""
    choices = chunk.get("choices", [])
    normalized_choices = []
    for c in choices:
        delta = c.get("delta", c.get("message", {}))
        normalized_choices.append({
            "index": c.get("index", 0),
            "delta": delta,
            "finish_reason": c.get("finish_reason"),
        })
    return {
        "id": chunk.get("id", ""),
        "object": "chat.completion.chunk",
        "created": chunk.get("created", int(time.time())),
        "model": target_model,
        "choices": normalized_choices,
    }

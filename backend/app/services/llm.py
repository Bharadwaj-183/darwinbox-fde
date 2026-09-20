from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.migration import LLMCache

logger = logging.getLogger("migration-agent.llm")


class LLMUnavailable(RuntimeError):
    """Raised when the configured LLM cannot complete a reasoning request in time."""


class LLMClient:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    @staticmethod
    def _cache_key(provider: str, model: str, system: str, payload: dict[str, Any]) -> str:
        raw = json.dumps(
            {"provider": provider, "model": model, "system": system, "payload": payload},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    async def structured_json(self, *, system: str, user: dict[str, Any], purpose: str = "agent_reasoning") -> dict[str, Any]:
        request_hash = self._cache_key(self.settings.llm_provider, self.settings.llm_model, system, user)
        cached = self.db.execute(select(LLMCache).where(LLMCache.request_hash == request_hash)).scalar_one_or_none()
        if cached:
            logger.info("llm_cache_hit purpose=%s model=%s request_hash=%s", purpose, self.settings.llm_model, request_hash[:12])
            return cached.response

        if not self.settings.openrouter_api_key:
            logger.warning("llm_skipped purpose=%s model=%s reason=api_key_not_configured", purpose, self.settings.llm_model)
            raise LLMUnavailable("AI reasoning key is not configured")

        logger.info("llm_call_start purpose=%s provider=%s model=%s request_hash=%s", purpose, self.settings.llm_provider, self.settings.llm_model, request_hash[:12])
        started_at = time.perf_counter()
        try:
            response = await self._call_openai_compatible(system, user, purpose=purpose)
        except LLMUnavailable as exc:
            logger.warning("llm_call_unavailable purpose=%s model=%s request_hash=%s elapsed_ms=%d error=%s", purpose, self.settings.llm_model, request_hash[:12], int((time.perf_counter()-started_at)*1000), exc)
            raise
        except Exception as exc:
            logger.warning("llm_call_failed purpose=%s model=%s request_hash=%s elapsed_ms=%d error=%s", purpose, self.settings.llm_model, request_hash[:12], int((time.perf_counter()-started_at)*1000), exc)
            raise LLMUnavailable(str(exc)) from exc

        logger.info("llm_call_success purpose=%s model=%s request_hash=%s elapsed_ms=%d", purpose, self.settings.llm_model, request_hash[:12], int((time.perf_counter()-started_at)*1000))
        self.db.add(
            LLMCache(
                request_hash=request_hash,
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                response=response,
            )
        )
        self.db.commit()
        return response

    async def _call_openai_compatible(self, system: str, payload: dict[str, Any], *, purpose: str = "agent_reasoning") -> dict[str, Any]:
        base = self.settings.llm_base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": "Darwinbox Migration Copilot",
        }
        request = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 1800,
        }

        # Keep each reasoning attempt bounded. Two 45s attempts + small backoff
        # keeps the total wait in the user's requested ~1–2 minute window.
        timeout = httpx.Timeout(
            timeout=self.settings.llm_timeout_seconds,
            connect=min(10.0, self.settings.llm_timeout_seconds),
            read=self.settings.llm_timeout_seconds,
            write=15.0,
            pool=10.0,
        )
        last_error: str | None = None

        for attempt in range(1, self.settings.max_llm_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{base}/chat/completions",
                        headers=headers,
                        json=request,
                    )

                logger.info("llm_http_response purpose=%s model=%s attempt=%d status=%d", purpose, self.settings.llm_model, attempt, response.status_code)
                if response.status_code >= 400:
                    body = response.text.strip()
                    compact = body[:500] if body else "no response body"
                    message = f"AI provider HTTP {response.status_code}: {compact}"
                    transient = response.status_code == 408 or response.status_code == 429 or response.status_code >= 500
                    if transient and attempt < self.settings.max_llm_retries:
                        last_error = message
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    raise LLMUnavailable(message)

                data = response.json()
                served_model = data.get("model") or self.settings.llm_model
                usage = data.get("usage") or {}
                logger.info("llm_response_received purpose=%s requested_model=%s served_model=%s usage=%s", purpose, self.settings.llm_model, served_model, usage)
                choices = data.get("choices") or []
                if not choices:
                    raise LLMUnavailable("AI provider returned no choices")
                message = choices[0].get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise LLMUnavailable("AI provider returned an empty response")

                parsed = self._parse_json_content(content)
                if not isinstance(parsed, dict):
                    raise LLMUnavailable("AI provider returned JSON, but not an object")
                return parsed

            except LLMUnavailable:
                raise
            except (httpx.TimeoutException, httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                last_error = self._friendly_exception(exc)
                if attempt < self.settings.max_llm_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
                else:
                    raise LLMUnavailable(last_error) from exc

        raise LLMUnavailable(last_error or "AI reasoning unavailable after retries")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```")
            cleaned = cleaned.removesuffix("```").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Some providers prepend a short explanation. Prefer the first JSON
            # object rather than failing the whole migration unnecessarily.
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise

    @staticmethod
    def _friendly_exception(exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "AI reasoning timed out before a response was received."
        return str(exc) or exc.__class__.__name__

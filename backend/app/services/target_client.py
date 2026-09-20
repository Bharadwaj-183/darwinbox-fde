from __future__ import annotations

import httpx


class TargetClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        body = response.text.strip()
        if body:
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if detail:
                    body = str(detail)
            except ValueError:
                pass
        return f"Target API HTTP {response.status_code}: {body[:800] if body else 'no response body'}"

    async def push(self, entity_name: str, record: dict, idempotency_key: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/entities/{entity_name}",
                json=record,
                headers={"Idempotency-Key": idempotency_key},
            )
            if response.is_error:
                raise RuntimeError(self._error_message(response))
            return response.json()

    async def delete(self, entity_name: str, record_key: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(f"{self.base_url}/entities/{entity_name}/{record_key}")
            if response.is_error:
                raise RuntimeError(self._error_message(response))
            return response.json()

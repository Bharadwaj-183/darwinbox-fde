from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Header, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | target-api | %(message)s",
)
logger = logging.getLogger("target-api")

app = FastAPI(title="Migration Target API", version="0.2.0")
STORE_PATH = Path(os.getenv("TARGET_STORE_PATH", "./data/target_store.json"))
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
STORE_LOCK = Lock()


def _load_store() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, tuple[str, str]], set[str]]:
    if not STORE_PATH.exists():
        return {}, {}, set()
    try:
        payload = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        # The simulated demo failure is intentionally process-scoped.
        # Target records and idempotency state persist, but a fresh target-API
        # process should be able to exercise the first-failure/retry scenario
        # again without requiring manual deletion of target_store.json.
        return (
            payload.get("records", {}),
            {k: tuple(v) for k, v in (payload.get("created_by_key", {}) or {}).items()},
            set(),
        )
    except Exception as exc:
        logger.warning("store_load_failed path=%s error=%s; starting with empty store", STORE_PATH, exc)
        return {}, {}, set()


records, created_by_key, failed_once = _load_store()


def _save_store() -> None:
    # Do not persist failed_once: it is a demo-control flag and should reset
    # whenever the target API process restarts. Persistent target data and
    # idempotency information remain intact.
    payload = {
        "records": records,
        "created_by_key": {key: list(value) for key, value in created_by_key.items()},
    }
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "store": str(STORE_PATH)}


@app.post("/entities/{entity_name}")
def create_or_update_entity(
    entity_name: str,
    payload: dict[str, Any],
    idempotency_key: str | None = Header(default=None),
):
    request_id = str(uuid.uuid4())
    natural_key = _natural_key(payload)
    idem = idempotency_key or hashlib.sha256(f"{entity_name}:{natural_key}".encode()).hexdigest()

    logger.info(
        "request_received request_id=%s entity=%s record_key=%s idempotency_key=%s fields=%s",
        request_id, entity_name, natural_key, idem, sorted(payload.keys()),
    )

    with STORE_LOCK:
        bucket = records.setdefault(entity_name, {})

        # Demo-only behavior: E-FAIL should produce one simulated failure
        # each time the target API process starts. This check intentionally
        # happens before idempotency replay so an old persisted E-FAIL record
        # cannot prevent the demo failure from being exercised again after a
        # fresh target-API restart.
        if natural_key.endswith("FAIL") and natural_key not in failed_once:
            failed_once.add(natural_key)
            _save_store()
            logger.warning(
                "simulated_failure request_id=%s entity=%s record_key=%s reason=demo_failure_once_per_process retry_expected=true",
                request_id, entity_name, natural_key,
            )
            raise HTTPException(status_code=500, detail={
                "error_code": "SIMULATED_TARGET_FAILURE",
                "message": "Simulated target API failure; retry should succeed",
                "retryable": True,
                "entity": entity_name,
                "record_key": natural_key,
                "request_id": request_id,
            })

        if idem in created_by_key:
            _, existing_key = created_by_key[idem]
            logger.info("idempotent_replay request_id=%s entity=%s record_key=%s", request_id, entity_name, existing_key)
            return {"status": "idempotent_replay", "entity": entity_name, "record_key": existing_key, "request_id": request_id}

        bucket[natural_key] = payload
        created_by_key[idem] = (entity_name, natural_key)
        _save_store()

    logger.info("record_accepted request_id=%s entity=%s record_key=%s status=accepted", request_id, entity_name, natural_key)
    return {"status": "accepted", "entity": entity_name, "record_key": natural_key, "request_id": request_id}


@app.get("/entities/{entity_name}")
def list_entities(entity_name: str):
    with STORE_LOCK:
        return {"entity": entity_name, "records": records.get(entity_name, {})}


@app.get("/entities")
def list_all_entities():
    with STORE_LOCK:
        return {"entities": records}


@app.get("/entities/{entity_name}/{record_key}")
def get_entity(entity_name: str, record_key: str):
    with STORE_LOCK:
        record = records.get(entity_name, {}).get(record_key)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@app.delete("/entities/{entity_name}/{record_key}")
def delete_entity(entity_name: str, record_key: str):
    with STORE_LOCK:
        bucket = records.setdefault(entity_name, {})
        existed = bucket.pop(record_key, None) is not None
        if existed:
            for key, value in list(created_by_key.items()):
                if value == (entity_name, record_key):
                    created_by_key.pop(key, None)
            _save_store()
    logger.info("record_deleted entity=%s record_key=%s existed=%s", entity_name, record_key, existed)
    return {"deleted": existed, "entity": entity_name, "record_key": record_key}


def _natural_key(payload: dict[str, Any]) -> str:
    for key in ("id", "employee_id", "customer_id", "department_id", "code", "number"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return hashlib.sha256(str(sorted(payload.items())).encode()).hexdigest()[:20]

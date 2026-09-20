import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def setup_function():
    main.records.clear()
    main.created_by_key.clear()
    main.failed_once.clear()


def test_failure_then_retry_succeeds():
    payload = {"employee_id": "E-FAIL", "name": "Demo Failure"}
    first = client.post("/entities/employee", json=payload, headers={"Idempotency-Key": "test-fail"})
    assert first.status_code == 500
    second = client.post("/entities/employee", json=payload, headers={"Idempotency-Key": "test-fail"})
    assert second.status_code == 200
    assert second.json()["status"] == "accepted"


def test_idempotency_prevents_duplicate_write():
    payload = {"employee_id": "E1", "name": "John"}
    first = client.post("/entities/employee", json=payload, headers={"Idempotency-Key": "same-key"})
    second = client.post("/entities/employee", json=payload, headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "idempotent_replay"


def test_multi_entity_listing():
    client.post("/entities/employee", json={"employee_id": "E1"}, headers={"Idempotency-Key": "employee-1"})
    client.post("/entities/department", json={"department_id": "D1"}, headers={"Idempotency-Key": "dept-1"})
    response = client.get("/entities")
    assert response.status_code == 200
    assert set(response.json()["entities"]) == {"employee", "department"}


def test_demo_failure_repeats_after_process_restart_even_with_persisted_record(tmp_path, monkeypatch):
    import importlib

    store_path = tmp_path / "target_store.json"
    monkeypatch.setenv("TARGET_STORE_PATH", str(store_path))

    fresh = importlib.reload(main)
    client1 = TestClient(fresh.app)
    payload = {"employee_id": "E-FAIL", "name": "Demo Failure"}

    first = client1.post("/entities/employee", json=payload, headers={"Idempotency-Key": "migration-1"})
    assert first.status_code == 500
    second = client1.post("/entities/employee", json=payload, headers={"Idempotency-Key": "migration-1"})
    assert second.status_code == 200

    # Same persistent target data, but a fresh process. The next E-FAIL request
    # should still trigger the one-time demo failure without deleting the store.
    restarted = importlib.reload(fresh)
    client2 = TestClient(restarted.app)
    third = client2.post("/entities/employee", json=payload, headers={"Idempotency-Key": "migration-2"})
    assert third.status_code == 500
    fourth = client2.post("/entities/employee", json=payload, headers={"Idempotency-Key": "migration-2"})
    assert fourth.status_code == 200

    monkeypatch.delenv("TARGET_STORE_PATH", raising=False)
    importlib.reload(restarted)

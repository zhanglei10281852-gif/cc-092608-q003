from __future__ import annotations

import pytest

from app.database import get_connection
from app.network.rules import DEFAULT_RULES


def prepare(client):
    scenario = client.post(
        "/api/network/scenarios",
        json={"code": "gdh-rail", "name": "广深高铁", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 10, "capacity_mbps": 3000},
    )
    assert scenario.status_code == 201, scenario.text
    segment = client.post(
        "/api/network/scenarios/gdh-rail/segments",
        json={"code": "gz-sz-01", "name": "广州南至虎门", "sequence_no": 1, "expected_dwell_seconds": 900, "capacity_mbps": 1200},
    )
    assert segment.status_code == 201, segment.text
    app = client.post(
        "/api/network/applications",
        json={"app_code": "video-call", "name": "视频通话", "category": "video_call", "latency_target_ms": 100, "packet_loss_target": 0.01, "min_downlink_mbps": 8, "min_uplink_mbps": 4, "default_priority": 70},
    )
    assert app.status_code == 201, app.text
    policy = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert policy.status_code == 201, policy.text
    published = client.post(
        f"/api/network/policies/{policy.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"},
    )
    assert published.status_code == 200, published.text


def sample_payload(sample_key: str, **overrides):
    payload = {
        "sample_key": sample_key,
        "scenario_code": "gdh-rail",
        "segment_code": "gz-sz-01",
        "app_code": "video-call",
        "subscriber_hash": "subscriber-000000000001",
        "device_class": "phone",
        "train_speed_kmh": 300,
        "latency_ms": 350,
        "packet_loss": 0.08,
        "downlink_mbps": 1.5,
        "uplink_mbps": 0.5,
        "observed_at": "2026-09-26T05:30:00Z",
    }
    payload.update(overrides)
    return payload


def healthy_payload(sample_key: str, **overrides):
    return sample_payload(sample_key, latency_ms=40, packet_loss=0.002, downlink_mbps=20, uplink_mbps=8, **overrides)


def table_counts() -> dict[str, int]:
    connection = get_connection()
    return {
        "samples": connection.execute("SELECT COUNT(*) FROM experience_samples").fetchone()[0],
        "incidents": connection.execute("SELECT COUNT(*) FROM quality_incidents").fetchone()[0],
        "batches": connection.execute("SELECT COUNT(*) FROM sample_batches").fetchone()[0],
    }


def test_batch_all_valid_writes_samples_and_incidents(client):
    prepare(client)
    items = [
        sample_payload("batch-sample-001"),
        healthy_payload("batch-sample-002"),
        sample_payload("batch-sample-003", segment_code=None, subscriber_hash="subscriber-000000000002"),
    ]
    response = client.post("/api/network/samples/batch", json={"items": items})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["batch_id"].startswith("batch-")
    assert body["accepted"] == 3
    assert [item["index"] for item in body["items"]] == [0, 1, 2]
    assert [item["status"] for item in body["items"]] == ["created", "created", "created"]
    assert [item["incident_id"] is not None for item in body["items"]] == [True, False, True]
    assert table_counts() == {"samples": 3, "incidents": 2, "batches": 1}
    connection = get_connection()
    for item in body["items"]:
        sample = connection.execute("SELECT * FROM experience_samples WHERE id=?", (item["sample_id"],)).fetchone()
        assert sample is not None and sample["sample_key"] == item["sample_key"]
        incident = connection.execute("SELECT * FROM quality_incidents WHERE sample_id=?", (item["sample_id"],)).fetchone()
        if item["incident_id"] is None:
            assert incident is None
        else:
            assert incident is not None and incident["id"] == item["incident_id"]
            assert incident["scenario_id"] == sample["scenario_id"]
            assert incident["segment_id"] == sample["segment_id"]
            assert incident["app_id"] == sample["app_id"]
    summary = client.get("/api/network/summary")
    assert summary.status_code == 200
    assert summary.json()["samples"] == 3
    assert summary.json()["incidents"] == {"open": 2}


@pytest.mark.parametrize(
    "overrides,status_code",
    [
        ({"scenario_code": "missing-scenario"}, 404),
        ({"segment_code": "missing-segment"}, 404),
        ({"app_code": "missing-app"}, 404),
        ({"observed_at": "not-a-timestamp"}, 422),
    ],
)
def test_batch_invalid_tail_item_leaves_no_trace(client, overrides, status_code):
    prepare(client)
    items = [
        sample_payload("batch-tail-001"),
        healthy_payload("batch-tail-002"),
        sample_payload("batch-tail-003", **overrides),
    ]
    response = client.post("/api/network/samples/batch", json={"items": items})
    assert response.status_code == status_code, response.text
    context = response.json()["error"]["context"]
    assert context["index"] == 2
    assert context["sample_key"] == "batch-tail-003"
    assert table_counts() == {"samples": 0, "incidents": 0, "batches": 0}
    summary = client.get("/api/network/summary")
    assert summary.json()["samples"] == 0
    assert summary.json()["incidents"] == {}


def test_batch_duplicate_replays_original_response(client):
    prepare(client)
    items = [sample_payload("batch-dup-001"), healthy_payload("batch-dup-002")]
    first = client.post("/api/network/samples/batch", json={"items": items})
    assert first.status_code == 202, first.text
    before = table_counts()
    assert before == {"samples": 2, "incidents": 1, "batches": 1}
    second = client.post("/api/network/samples/batch", json={"items": items})
    assert second.status_code == 202
    assert second.json() == first.json()
    assert table_counts() == before
    single = client.post("/api/network/samples", json=items[0])
    assert single.status_code == 202
    assert single.json()["sample_id"] == first.json()["items"][0]["sample_id"]
    assert table_counts() == before


def test_batch_conflicting_sample_key_rolls_back_whole_batch(client):
    prepare(client)
    first = client.post("/api/network/samples/batch", json={"items": [sample_payload("batch-conflict-001")]})
    assert first.status_code == 202, first.text
    before = table_counts()
    items = [healthy_payload("batch-conflict-002"), sample_payload("batch-conflict-001", latency_ms=999)]
    response = client.post("/api/network/samples/batch", json={"items": items})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["context"]["sample_key"] == "batch-conflict-001"
    assert table_counts() == before


def test_batch_mixed_duplicate_and_new_items(client):
    prepare(client)
    first = client.post("/api/network/samples/batch", json={"items": [sample_payload("batch-mix-001")]})
    assert first.status_code == 202, first.text
    original = first.json()["items"][0]
    items = [sample_payload("batch-mix-001"), healthy_payload("batch-mix-002")]
    response = client.post("/api/network/samples/batch", json={"items": items})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["batch_id"] != first.json()["batch_id"]
    reused, created = body["items"]
    assert reused["status"] == "duplicate"
    assert reused["sample_id"] == original["sample_id"]
    assert reused["incident_id"] == original["incident_id"]
    assert created["status"] == "created"
    assert created["incident_id"] is None
    assert table_counts() == {"samples": 2, "incidents": 1, "batches": 2}
    replay = client.post("/api/network/samples/batch", json={"items": items})
    assert replay.status_code == 202
    assert replay.json() == body
    assert table_counts() == {"samples": 2, "incidents": 1, "batches": 2}


def test_batch_rejects_repeated_sample_keys(client):
    prepare(client)
    items = [sample_payload("batch-repeat-001"), healthy_payload("batch-repeat-001")]
    response = client.post("/api/network/samples/batch", json={"items": items})
    assert response.status_code == 422
    assert table_counts() == {"samples": 0, "incidents": 0, "batches": 0}

import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.main import create_app
from tests.helpers import T0


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def client(engine: Engine, clock: FakeClock) -> Iterator[TestClient]:
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    settings = Settings(database_url=url, anthropic_api_key=None)
    app = create_app(settings, clock=clock)
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


def create(client: TestClient, permit_type: str = "deck") -> dict[str, Any]:
    ordinance_id = client.get("/api/ordinances").json()[0]["id"]
    r = client.post(
        "/api/applications",
        json={
            "ordinance_id": ordinance_id,
            "permit_type": permit_type,
            "project_address": "12 Pine St",
            "contractor_name": "Ana Builder",
            "contractor_email": "ana@example.com",
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def tick_all(client: TestClient, app: dict[str, Any], category: str) -> dict[str, Any]:
    for item in app["checklist"]:
        if item["category"] == category:
            r = client.patch(
                f"/api/applications/{app['id']}/checklist/{item['id']}", json={"done": True}
            )
            assert r.status_code == 200
            app = r.json()
    return app


def act(client: TestClient, app: dict[str, Any], action: str, **extra: Any) -> Any:
    return client.post(
        f"/api/applications/{app['id']}/transitions",
        json={"action": action, "expected_version": app["version"], **extra},
    )


def test_seeded_ordinance_is_listed(client: TestClient) -> None:
    [o] = client.get("/api/ordinances").json()
    assert o["jurisdiction"] == "Lakeport (sample)"
    assert o["permit_types"] == ["deck", "fence", "shed", "roof"]
    assert o["sections"] == 13


def test_requirements_filter_by_permit_type(client: TestClient) -> None:
    oid = client.get("/api/ordinances").json()[0]["id"]
    reqs = client.get(f"/api/ordinances/{oid}/requirements", params={"permit_type": "fence"}).json()
    assert {r["permit_type"] for r in reqs} == {"fence", "*"}
    assert client.get("/api/ordinances/999/requirements").status_code == 404


def test_search(client: TestClient) -> None:
    hits = client.get("/api/search", params={"q": "deck guard height"}).json()
    assert hits[0]["section"] == "14-201"
    assert any("guards" in s for s in hits[0]["excerpt"])
    assert client.get("/api/search", params={"q": "x"}).status_code == 422


def test_ingest_new_ordinance(client: TestClient) -> None:
    text = "§ 1 Pools.\nA pool permit application must include a fence plan. The permit fee for a pool is $80."
    r = client.post("/api/ordinances", json={"jurisdiction": "Elm", "title": "Pools", "text": text})
    assert r.status_code == 201
    body = r.json()
    assert body["extractor"] == "rules" and body["requirements"] == 2
    bad = client.post(
        "/api/ordinances", json={"jurisdiction": "Elm", "title": "Bad", "text": "no sections " * 5}
    )
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_input"


def test_application_validation(client: TestClient) -> None:
    r = client.post(
        "/api/applications",
        json={
            "ordinance_id": 1,
            "permit_type": "deck",
            "project_address": "12 Pine St",
            "contractor_name": "Ana",
            "contractor_email": "not-an-email",
        },
    )
    assert r.status_code == 422
    assert client.get("/api/applications/999").status_code == 404


def test_end_to_end_workflow_with_reminders(client: TestClient, clock: FakeClock) -> None:
    app = create(client)
    assert app["state"] == "draft"
    assert {a["action"] for a in app["actions"]} == {"submit", "withdraw"}

    blocked = act(client, app, "submit")
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "guard_failed"
    assert blocked.json()["details"]

    app = tick_all(client, app, "document")
    app = act(client, app, "submit").json()
    assert app["state"] == "submitted"

    # A second tab still holding the old version gets a conflict.
    stale = client.post(
        f"/api/applications/{app['id']}/transitions",
        json={"action": "start_review", "expected_version": app["version"] - 1},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    for action in ("start_review", "approve"):
        app = act(client, app, action).json()
    app = tick_all(client, app, "fee")
    clock.now = T0 + timedelta(days=5)
    app = act(client, app, "issue").json()
    assert app["state"] == "issued" and app["permit_number"].startswith("LP-2026-")
    assert {j["status"] for j in app["jobs"]} == {"queued"}

    # Nothing is due yet.
    assert client.post("/api/jobs/run-due").json()["claimed"] == 0
    # Fast-forward 151 days: the start-work reminder fires.
    future = (T0 + timedelta(days=5 + 151)).isoformat()
    run = client.post("/api/jobs/run-due", params={"as_of": future}).json()
    assert (run["claimed"], run["done"]) == (1, 1)
    [note] = client.get("/api/notifications", params={"application_id": app["id"]}).json()
    assert "expires in 30 days" in note["subject"]
    assert client.get("/api/jobs/stats").json() == {"done": 1, "queued": 1}

    invalid = act(client, client.get(f"/api/applications/{app['id']}").json(), "approve")
    assert invalid.status_code == 409 and invalid.json()["code"] == "invalid_transition"


def test_as_of_requires_demo_mode(engine: Engine) -> None:
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    app = create_app(Settings(database_url=url, demo_mode=False, seed_on_startup=False))
    with TestClient(app) as c:
        r = c.post("/api/jobs/run-due", params={"as_of": "2030-01-01T00:00:00"})
        assert r.status_code == 403
    app.state.engine.dispose()


def test_list_applications_filters_by_state(client: TestClient) -> None:
    a = create(client)
    create(client, "fence")
    act(client, a, "withdraw")
    assert [x["id"] for x in client.get("/api/applications", params={"state": "withdrawn"}).json()] == [
        a["id"]
    ]
    assert len(client.get("/api/applications").json()) == 2

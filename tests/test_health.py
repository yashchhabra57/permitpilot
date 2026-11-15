from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite://")))
    assert client.get("/health").json() == {"status": "ok"}


def test_dashboard_is_served() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite://")))
    r = client.get("/")
    assert r.status_code == 200 and "PermitPilot" in r.text
    assert client.get("/static/app.js").status_code == 200

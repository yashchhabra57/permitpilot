from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite://")))
    assert client.get("/health").json() == {"status": "ok"}

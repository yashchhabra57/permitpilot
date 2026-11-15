"""End-to-end smoke test against a real uvicorn process.

    python scripts/smoke.py

Starts the server on a free port with a throwaway SQLite file, walks one deck permit from draft
to closed over HTTP, fast-forwards the reminder worker, and prints what happened.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # "§" on Windows
    port = free_port()
    db = Path(tempfile.mkdtemp()) / "smoke.db"
    env = {**os.environ, "PERMITPILOT_DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    env.pop("PERMITPILOT_ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(port)],
        cwd=ROOT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=10) as c:
            for _ in range(50):
                try:
                    if c.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.2)
            else:
                print("server did not start")
                return 1
            run_flow(c)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("SMOKE OK")
    return 0


def check(r: httpx.Response, status: int = 200) -> Any:
    if r.status_code != status:
        raise SystemExit(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text}")
    return r.json()


def run_flow(c: httpx.Client) -> None:
    [ordinance] = check(c.get("/api/ordinances"))
    print(f"ordinance: {ordinance['title']} ({ordinance['requirements']} requirements)")

    hit = check(c.get("/api/search", params={"q": "how deep must deck footings go"}))[0]
    print(f"search: § {hit['section']} -> {hit['excerpt'][0]}")

    app = check(
        c.post(
            "/api/applications",
            json={
                "ordinance_id": ordinance["id"],
                "permit_type": "deck",
                "project_address": "12 Pine St",
                "contractor_name": "Ana Builder",
                "contractor_email": "ana@example.com",
            },
        ),
        201,
    )
    print(f"application #{app['id']}: {len(app['checklist'])} checklist items")

    blocked = c.post(
        f"/api/applications/{app['id']}/transitions",
        json={"action": "submit", "expected_version": app["version"]},
    )
    print(f"submit before documents -> {blocked.status_code} {blocked.json()['code']}")

    def tick(category: str) -> None:
        nonlocal app
        for item in app["checklist"]:
            if item["category"] == category and not item["done"]:
                app = check(
                    c.patch(
                        f"/api/applications/{app['id']}/checklist/{item['id']}",
                        json={"done": True},
                    )
                )

    def act(action: str, **extra: Any) -> None:
        nonlocal app
        app = check(
            c.post(
                f"/api/applications/{app['id']}/transitions",
                json={"action": action, "expected_version": app["version"], **extra},
            )
        )
        print(f"  {action:<20} -> {app['state']}")

    tick("document")
    act("submit")
    act("start_review")
    act("request_corrections", note="Show footing depth on framing plan")
    act("submit")
    act("start_review")
    act("approve")
    tick("fee")
    act("issue")
    print(f"permit {app['permit_number']} expires {app['expires_at']}")

    issued = datetime.fromisoformat(app["issued_at"])
    as_of = (issued + timedelta(days=151)).replace(tzinfo=UTC).isoformat()
    run = check(c.post("/api/jobs/run-due", params={"as_of": as_of}))
    print(f"worker as of +151 days: claimed={run['claimed']} done={run['done']}")
    for n in check(c.get("/api/notifications", params={"application_id": app["id"]})):
        print(f"  notification -> {n['recipient']}: {n['subject']}")

    inspection = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3)
    act("schedule_inspection", inspection_at=inspection.isoformat())
    tick("inspection")
    act("pass_inspection")
    print(f"job stats: {check(c.get('/api/jobs/stats'))}")


if __name__ == "__main__":
    sys.exit(main())

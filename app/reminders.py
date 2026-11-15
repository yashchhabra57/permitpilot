"""Reminder job handler.

A reminder is scheduled weeks ahead, and a lot can change before it fires: the contractor may
have resubmitted, the permit may be closed. So the handler re-checks the application state when
it runs and skips reminders that no longer apply.

It is also idempotent. If a worker writes the notification and then crashes before marking the
job done, the job is claimed again after its lease expires. The handler first checks whether a
notification for this job already exists, so the contractor gets one message, not two.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import Handler, PermanentJobError
from app.models import Application, Job, Notification


@dataclass(frozen=True)
class Template:
    states: frozenset[str]
    subject: str
    body: str


TEMPLATES: dict[str, Template] = {
    "corrections_due": Template(
        frozenset({"corrections_requested"}),
        "Corrections due in 7 days for {address}",
        "The city asked for corrections on your {permit_type} permit at {address}. Revised "
        "documents are due within 30 days of the request or the application is considered "
        "abandoned (§ 14-601).",
    ),
    "start_work_deadline": Template(
        frozenset({"issued"}),
        "Permit {permit_number} expires in 30 days unless work starts",
        "Permit {permit_number} for {address} expires on {expires} if work has not started "
        "(§ 14-602). Book a first inspection to show work is under way.",
    ),
    "final_inspection_deadline": Template(
        frozenset({"issued", "inspection_scheduled"}),
        "Request the final inspection for {permit_number} within 30 days",
        "Permit {permit_number} for {address} expires if the final inspection is not requested "
        "within 365 days of issuance (§ 14-602).",
    ),
    "inspection_tomorrow": Template(
        frozenset({"inspection_scheduled"}),
        "Inspection tomorrow at {address}",
        "Your {permit_type} inspection is scheduled for {inspection}. Post the permit card where "
        "it is visible from the street (§ 14-603).",
    ),
}


def _render(text: str, app: Application) -> str:
    return text.format(
        address=app.project_address,
        permit_type=app.permit_type,
        permit_number=app.permit_number or "(pending)",
        expires=app.expires_at.date().isoformat() if app.expires_at else "?",
        inspection=app.inspection_at.strftime("%Y-%m-%d %H:%M") if app.inspection_at else "?",
    )


def handle_reminder(session: Session, job: Job, now: datetime) -> None:
    try:
        payload = json.loads(job.payload)
        app_id = int(payload["application_id"])
        template = TEMPLATES[payload["template"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise PermanentJobError(f"bad reminder payload: {job.payload}") from exc

    app = session.get(Application, app_id)
    if app is None or app.state not in template.states:
        return  # no longer relevant; finishing the job without sending is correct

    already_sent = session.scalar(select(Notification.id).where(Notification.job_id == job.id))
    if already_sent is not None:
        return

    session.add(
        Notification(
            application_id=app.id,
            job_id=job.id,
            recipient=app.contractor_email,
            subject=_render(template.subject, app),
            body=_render(template.body, app),
            created_at=now,
        )
    )


HANDLERS: dict[str, Handler] = {"reminder": handle_reminder}

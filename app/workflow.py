"""The permit application state machine.

    draft --submit--> submitted --start_review--> in_review --approve--> approved --issue--> issued
                         ^                           |  |                                     | ^
                         |                           |  +--reject--> rejected                 | |
                         +------submit------ corrections_requested <--request_corrections   | |
                                                                                             v |
                       closed <--pass_inspection-- inspection_scheduled <--schedule_inspection |
                                                       +--fail_inspection---------------------+
    Any non-terminal state --withdraw--> withdrawn

Each transition has a guard (a condition that must hold) and effects (what else happens in the
same database transaction: audit event, permit number, reminder jobs). Guards read the checklist:
you cannot submit until every required document is attached, cannot be issued until fees are
paid, and cannot close until every required inspection has passed.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.jobs import cancel_by_dedupe_prefix, cancel_pending, enqueue
from app.models import Application, ApplicationEvent, ChecklistItem

TERMINAL = frozenset({"closed", "rejected", "withdrawn"})
STATES = (
    "draft",
    "submitted",
    "in_review",
    "corrections_requested",
    "approved",
    "issued",
    "inspection_scheduled",
    "closed",
    "rejected",
    "withdrawn",
)

# Lakeport sample rules (Chapter 14-6xx). A real deployment would load these per jurisdiction.
CORRECTIONS_WINDOW_DAYS = 30
START_WORK_DAYS = 180
FINAL_INSPECTION_DAYS = 365
REMINDER_LEAD_DAYS = 7
EXPIRY_LEAD_DAYS = 30


class WorkflowError(Exception):
    def __init__(self, code: str, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


@dataclass
class TransitionInput:
    note: str | None = None
    inspection_at: datetime | None = None


Guard = Callable[[Application, TransitionInput, datetime], list[str]]
Effect = Callable[[Session, Application, TransitionInput, datetime], None]


@dataclass(frozen=True)
class Transition:
    sources: frozenset[str]
    target: str
    guards: tuple[Guard, ...] = ()
    effects: tuple[Effect, ...] = ()
    note_required: bool = False


# Which checklist categories gate which action.
GATES: dict[str, str] = {"submit": "document", "issue": "fee", "pass_inspection": "inspection"}


def open_items(app: Application, category: str) -> list[ChecklistItem]:
    return [i for i in app.checklist if i.category == category and i.mandatory and not i.done]


def _gate(action: str) -> Guard:
    category = GATES[action]

    def guard(app: Application, _: TransitionInput, __: datetime) -> list[str]:
        return [f"[{i.citation}] {i.description}" for i in open_items(app, category)]

    return guard


def _inspection_in_future(app: Application, data: TransitionInput, now: datetime) -> list[str]:
    if data.inspection_at is None:
        return ["inspection_at is required"]
    if data.inspection_at <= now:
        return ["inspection_at must be in the future"]
    if app.expires_at is not None and data.inspection_at > app.expires_at:
        return ["inspection_at is after the permit expires"]
    return []


def _remind(
    session: Session, app: Application, template: str, run_at: datetime, round_key: str
) -> None:
    enqueue(
        session,
        "reminder",
        {"application_id": app.id, "template": template},
        run_at,
        dedupe_key=f"app:{app.id}:{template}:{round_key}",
        application_id=app.id,
    )


def _on_corrections(session: Session, app: Application, _: TransitionInput, now: datetime) -> None:
    due = now + timedelta(days=CORRECTIONS_WINDOW_DAYS - REMINDER_LEAD_DAYS)
    _remind(session, app, "corrections_due", due, f"v{app.version}")


def _on_resubmit(session: Session, app: Application, _: TransitionInput, now: datetime) -> None:
    cancel_pending(session, app.id, {"reminder"})


def _on_issue(session: Session, app: Application, _: TransitionInput, now: datetime) -> None:
    app.permit_number = f"LP-{now.year}-{app.id:05d}"
    app.issued_at = now
    app.expires_at = now + timedelta(days=START_WORK_DAYS)
    start_reminder = app.expires_at - timedelta(days=EXPIRY_LEAD_DAYS)
    final_reminder = now + timedelta(days=FINAL_INSPECTION_DAYS - EXPIRY_LEAD_DAYS)
    _remind(session, app, "start_work_deadline", start_reminder, "issue")
    _remind(session, app, "final_inspection_deadline", final_reminder, "issue")


def _on_schedule(session: Session, app: Application, data: TransitionInput, now: datetime) -> None:
    assert data.inspection_at is not None  # checked by guard
    app.inspection_at = data.inspection_at
    # Work has started once an inspection is booked, so the 180-day start deadline is moot.
    cancel_pending_template(session, app, "start_work_deadline")
    _remind(
        session,
        app,
        "inspection_tomorrow",
        data.inspection_at - timedelta(days=1),
        f"v{app.version}",
    )


def _on_failed_inspection(
    session: Session, app: Application, _: TransitionInput, now: datetime
) -> None:
    app.inspection_at = None
    cancel_pending_template(session, app, "inspection_tomorrow")


def _on_finish(session: Session, app: Application, _: TransitionInput, now: datetime) -> None:
    cancel_pending(session, app.id)


def cancel_pending_template(session: Session, app: Application, template: str) -> None:
    cancel_by_dedupe_prefix(session, f"app:{app.id}:{template}:")


_NON_TERMINAL = frozenset(s for s in STATES if s not in TERMINAL)

TRANSITIONS: dict[str, Transition] = {
    "submit": Transition(
        frozenset({"draft", "corrections_requested"}),
        "submitted",
        guards=(_gate("submit"),),
        effects=(_on_resubmit,),
    ),
    "start_review": Transition(frozenset({"submitted"}), "in_review"),
    "request_corrections": Transition(
        frozenset({"in_review"}),
        "corrections_requested",
        effects=(_on_corrections,),
        note_required=True,
    ),
    "approve": Transition(frozenset({"in_review"}), "approved"),
    "reject": Transition(
        frozenset({"in_review"}), "rejected", effects=(_on_finish,), note_required=True
    ),
    "issue": Transition(
        frozenset({"approved"}), "issued", guards=(_gate("issue"),), effects=(_on_issue,)
    ),
    "schedule_inspection": Transition(
        frozenset({"issued"}),
        "inspection_scheduled",
        guards=(_inspection_in_future,),
        effects=(_on_schedule,),
    ),
    "fail_inspection": Transition(
        frozenset({"inspection_scheduled"}),
        "issued",
        effects=(_on_failed_inspection,),
        note_required=True,
    ),
    "pass_inspection": Transition(
        frozenset({"inspection_scheduled"}),
        "closed",
        guards=(_gate("pass_inspection"),),
        effects=(_on_finish,),
    ),
    "withdraw": Transition(_NON_TERMINAL, "withdrawn", effects=(_on_finish,)),
}


@dataclass
class AvailableAction:
    action: str
    target: str
    blocked_by: list[str] = field(default_factory=list)


def available_actions(app: Application, now: datetime) -> list[AvailableAction]:
    """What the UI can offer from the current state, and what blocks each option."""
    out = []
    for name, t in TRANSITIONS.items():
        if app.state not in t.sources:
            continue
        blocked: list[str] = []
        for guard in t.guards:
            if guard is _inspection_in_future:
                continue  # needs user input; validated on submit
            blocked.extend(guard(app, TransitionInput(), now))
        out.append(AvailableAction(name, t.target, blocked))
    return out


def apply_transition(
    session: Session,
    app: Application,
    action: str,
    expected_version: int,
    now: datetime,
    data: TransitionInput | None = None,
) -> ApplicationEvent:
    """Validate and apply one transition. Does not commit; the caller commits once."""
    data = data or TransitionInput()
    t = TRANSITIONS.get(action)
    if t is None:
        raise WorkflowError("unknown_action", f"unknown action {action!r}")
    if app.version != expected_version:
        raise WorkflowError(
            "stale_version",
            f"application changed since you loaded it (now version {app.version})",
        )
    if app.state not in t.sources:
        raise WorkflowError(
            "invalid_transition", f"cannot {action} an application that is {app.state}"
        )
    if t.note_required and not (data.note and data.note.strip()):
        raise WorkflowError("note_required", f"{action} needs a note explaining why")
    problems: list[str] = []
    for guard in t.guards:
        problems.extend(guard(app, data, now))
    if problems:
        raise WorkflowError("guard_failed", f"cannot {action} yet", problems)

    from_state = app.state
    app.state = t.target
    app.updated_at = now
    for effect in t.effects:
        effect(session, app, data, now)
    event = ApplicationEvent(
        application=app,
        action=action,
        from_state=from_state,
        to_state=t.target,
        note=data.note.strip() if data.note else None,
        at=now,
    )
    session.add(event)
    return event

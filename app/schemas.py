"""Request and response bodies (Pydantic). Validation errors become 422 responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OrdinanceIn(BaseModel):
    jurisdiction: str = Field(min_length=2, max_length=120)
    title: str = Field(min_length=2, max_length=200)
    text: str = Field(min_length=20, max_length=500_000)


class OrdinanceOut(BaseModel):
    id: int
    jurisdiction: str
    title: str
    permit_types: list[str]
    sections: int
    requirements: int


class IngestOut(OrdinanceOut):
    extractor: str
    fallback_reason: str | None


class RequirementOut(BaseModel):
    id: int
    permit_type: str
    category: str
    description: str
    quote: str
    section: str
    extractor: str


class SearchHitOut(BaseModel):
    ordinance_id: int
    jurisdiction: str
    section: str
    heading: str
    score: float
    excerpt: list[str]


class ApplicationIn(BaseModel):
    ordinance_id: int
    permit_type: str = Field(min_length=2, max_length=40)
    project_address: str = Field(min_length=3, max_length=200)
    contractor_name: str = Field(min_length=2, max_length=120)
    contractor_email: str = Field(pattern=EMAIL_PATTERN, max_length=200)


class ChecklistItemOut(ORM):
    id: int
    category: str
    description: str
    citation: str
    mandatory: bool
    done: bool
    done_at: datetime | None


class EventOut(ORM):
    action: str
    from_state: str
    to_state: str
    note: str | None
    at: datetime


class ActionOut(BaseModel):
    action: str
    target: str
    blocked_by: list[str]


class JobOut(ORM):
    id: int
    kind: str
    status: str
    run_at: datetime
    attempts: int
    max_attempts: int
    last_error: str | None
    dedupe_key: str | None
    application_id: int | None


class ApplicationSummary(ORM):
    id: int
    ordinance_id: int
    permit_type: str
    project_address: str
    contractor_name: str
    state: str
    version: int
    permit_number: str | None
    updated_at: datetime


class ApplicationOut(ApplicationSummary):
    contractor_email: str
    issued_at: datetime | None
    expires_at: datetime | None
    inspection_at: datetime | None
    created_at: datetime
    progress: dict[str, int]
    checklist: list[ChecklistItemOut]
    events: list[EventOut]
    actions: list[ActionOut]
    jobs: list[JobOut]


class ChecklistPatch(BaseModel):
    done: bool


class TransitionIn(BaseModel):
    action: str
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)
    inspection_at: datetime | None = None


class RunDueOut(BaseModel):
    as_of: datetime
    claimed: int
    done: int
    retried: int
    dead: int
    errors: list[str]


class NotificationOut(ORM):
    id: int
    application_id: int | None
    recipient: str
    subject: str
    body: str
    created_at: datetime


class ErrorOut(BaseModel):
    code: str
    message: str
    details: list[str] = []

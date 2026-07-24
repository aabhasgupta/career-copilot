from datetime import datetime, timedelta, timezone

from copilot.db.models import Job
from copilot.formatting import format_posted, format_salary, posting_age_days


def _job(**kwargs) -> Job:
    defaults = dict(title="Engineer", source="adzuna", apply_url="x", dedupe_hash="h")
    defaults.update(kwargs)
    return Job(**defaults)


def test_format_salary_unknown_when_neither_bound_set():
    assert format_salary(_job()) == "unknown"


def test_format_salary_renders_range():
    assert format_salary(_job(salary_min=100000, salary_max=150000)) == "$100,000-$150,000"


def test_posting_age_days_unknown_when_no_posted_at():
    assert posting_age_days(_job(posted_at=None)) is None
    assert format_posted(_job(posted_at=None)) == "unknown"


def test_posting_age_days_handles_naive_datetime_as_utc():
    # SQLite round-trips DateTime(timezone=True) columns as naive - a bug fixed
    # live during Phase 2 verification (TypeError comparing naive vs aware).
    naive_five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).replace(tzinfo=None)
    assert posting_age_days(_job(posted_at=naive_five_days_ago)) == 5


def test_format_posted_relative_labels():
    now = datetime.now(timezone.utc)
    assert format_posted(_job(posted_at=now)) == "today"
    assert format_posted(_job(posted_at=now - timedelta(days=1))) == "1d ago"
    assert format_posted(_job(posted_at=now - timedelta(days=9))) == "9d ago"

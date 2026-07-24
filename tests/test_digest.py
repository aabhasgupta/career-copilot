import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot.db.models import Company, Job
from copilot.notify.digest import HIGH_FIT_THRESHOLD, build_digest, mark_digest_sent


def _job(**kwargs) -> Job:
    defaults = dict(
        title="Engineer",
        source="adzuna",
        apply_url="https://example.com/apply",
        dedupe_hash=kwargs.pop("dedupe_hash", "h") + str(id(kwargs)),
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _session(tmp_path: Path):
    from copilot.db import get_engine, get_session, init_db

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    return get_session(engine)


def test_build_digest_none_when_nothing_new(tmp_path: Path):
    with _session(tmp_path) as session:
        assert build_digest(session, state_path=tmp_path / "state.json") is None


def test_build_digest_counts_new_jobs_and_highlights_high_fit(tmp_path: Path):
    with _session(tmp_path) as session:
        company = Company(name="Acme")
        session.add(company)
        session.flush()
        session.add(
            Job(
                company_id=company.id,
                title="ML Engineer",
                location="Remote",
                source="adzuna",
                apply_url="https://example.com/1",
                dedupe_hash="h1",
                fit_score=85,
            )
        )
        session.add(
            Job(
                company_id=company.id,
                title="Backend Engineer",
                location="Austin, TX",
                source="adzuna",
                apply_url="https://example.com/2",
                dedupe_hash="h2",
                fit_score=40,
            )
        )
        session.commit()

        result = build_digest(session, state_path=tmp_path / "state.json")
        assert result is not None
        assert result.new_count == 2
        assert len(result.high_fit_jobs) == 1
        assert result.high_fit_jobs[0].title == "ML Engineer"
        assert f"{HIGH_FIT_THRESHOLD}" in result.subject
        assert "ML Engineer" in result.body_html
        assert "Backend Engineer" not in result.body_html  # only high-fit jobs get a row


def test_build_digest_respects_last_sent_state(tmp_path: Path):
    state_path = tmp_path / "state.json"
    with _session(tmp_path) as session:
        company = Company(name="Acme")
        session.add(company)
        session.flush()
        old_job = Job(
            company_id=company.id,
            title="Old Job",
            source="adzuna",
            apply_url="https://example.com/old",
            dedupe_hash="old",
            fit_score=90,
        )
        session.add(old_job)
        session.commit()
        old_job.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        session.commit()

        mark_digest_sent(state_path)
        state = json.loads(state_path.read_text())
        assert "last_sent_at" in state

        # A job created before the last digest was sent must not reappear.
        result = build_digest(session, state_path=state_path)
        assert result is None


def test_no_high_fit_jobs_shows_fallback_message(tmp_path: Path):
    with _session(tmp_path) as session:
        company = Company(name="Acme")
        session.add(company)
        session.flush()
        session.add(
            Job(
                company_id=company.id,
                title="Unscored Job",
                source="adzuna",
                apply_url="https://example.com/3",
                dedupe_hash="h3",
            )
        )
        session.commit()

        result = build_digest(session, state_path=tmp_path / "state.json")
        assert result is not None
        assert result.high_fit_jobs == []
        assert "None scored" in result.body_html

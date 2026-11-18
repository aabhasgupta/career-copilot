from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copilot.config import Profile
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Company, Job
from copilot.resume import ContactInfo, ResumeProfile, WorkExperience, resume_content_hash
from copilot.scoring.rubric import (
    BatchScores, JobScore, JobToScore, _job_block, is_score_stale, resume_block, score_and_apply,
)

RESUME = ResumeProfile(
    full_name="Jane Doe",
    contact=ContactInfo(email="jane@example.com"),
    summary="ML engineer.",
    seniority="senior",
    years_of_experience=8,
    skills=["Python", "PyTorch"],
    domains=["fintech"],
    work_experience=[
        WorkExperience(
            title="ML Engineer",
            company="Acme",
            start="2020-01",
            end=None,
            highlights=["Built a fraud model."],
        )
    ],
    education=[],
)


def test_resume_block_includes_key_facts():
    block = resume_block(RESUME)
    assert "Jane Doe" in block
    assert "senior" in block
    assert "PyTorch" in block
    assert "fintech" in block
    assert "Built a fraud model." in block


def test_job_block_includes_id_and_truncates_jd():
    job = JobToScore(
        id=42,
        title="ML Engineer",
        company="Acme",
        location="Chicago, IL",
        remote=True,
        salary_min=150000,
        salary_max=180000,
        jd_text="x" * 10000,
    )
    block = _job_block(job)
    assert "job_id: 42" in block
    assert "Chicago, IL" in block
    assert "(remote)" in block
    assert "150000-180000" in block
    # Truncated well below the full 10000 chars
    assert len(block) < 7000


def test_job_block_handles_missing_jd_text():
    job = JobToScore(
        id=1,
        title="Engineer",
        company="Acme",
        location=None,
        remote=None,
        salary_min=None,
        salary_max=None,
        jd_text=None,
    )
    block = _job_block(job)
    assert "no description text available" in block
    assert "unknown location" in block
    assert "unknown posting age" in block  # default, unset in this test


def test_job_block_includes_posting_age_and_channel():
    job = JobToScore(
        id=1, title="Engineer", company="Acme", location=None, remote=None,
        salary_min=None, salary_max=None, jd_text=None,
        source="greenhouse", posting_age_days=3,
    )
    block = _job_block(job)
    assert "posted 3d ago" in block
    assert "direct employer ATS" in block


def test_job_block_labels_aggregator_and_unknown_channels():
    from copilot.scoring.rubric import _channel_label

    assert _channel_label("jsearch") == "aggregator repost"
    assert _channel_label("adzuna") == "aggregator repost"
    assert _channel_label("greenhouse") == "direct employer ATS"
    assert _channel_label("remotive") == "job board (remotive)"


def make_profile() -> Profile:
    return Profile.model_validate(
        {
            "identity": {"full_name": "Test User", "email": "t@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["ML Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "t@hotmail.com"},
        }
    )


def _score_response(scores: list[JobScore]):
    return SimpleNamespace(usage=None, parsed_output=BatchScores(scores=scores))


def test_score_and_apply_writes_all_dimensions_and_commits(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        job = Job(
            company_id=acme.id, title="ML Engineer", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
        )
        session.add(job)
        session.commit()

        with patch("copilot.llm.Anthropic") as mock_client:
            mock_client.return_value.messages.parse.return_value = _score_response([
                JobScore(
                    job_id=job.id, skill_match_score=80, experience_level_score=70,
                    domain_fit_score=60, location_fit_score=90, visa_feasibility_score=50,
                    fit_score=75, reasoning="Strong match.", visa_signal="unknown",
                    response_likelihood_score=65, response_likelihood_reasoning="Fresh direct-ATS posting.",
                )
            ])
            result = score_and_apply(session, make_profile(), RESUME, [job])

        assert result.scored == 1
        assert result.missing == 0
        assert job.fit_score == 75
        assert job.fit_reasoning == "Strong match."
        assert job.skill_match_score == 80
        assert job.visa_feasibility_score == 50
        assert job.response_likelihood_score == 65
        assert job.response_likelihood_reasoning == "Fresh direct-ATS posting."

    # Committed for real - a fresh session sees the same values.
    with get_session(engine) as session2:
        reloaded = session2.get(Job, job.id)
        assert reloaded.fit_score == 75


def test_score_and_apply_reports_missing_when_model_skips_a_job(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        j1 = Job(company_id=acme.id, title="A", source="greenhouse",
                  apply_url="https://acme.com/1", dedupe_hash="h1")
        j2 = Job(company_id=acme.id, title="B", source="greenhouse",
                  apply_url="https://acme.com/2", dedupe_hash="h2")
        session.add_all([j1, j2])
        session.commit()

        with patch("copilot.llm.Anthropic") as mock_client:
            mock_client.return_value.messages.parse.return_value = _score_response([
                JobScore(
                    job_id=j1.id, skill_match_score=1, experience_level_score=1,
                    domain_fit_score=1, location_fit_score=1, visa_feasibility_score=1,
                    fit_score=1, reasoning="x", visa_signal="unknown",
                    response_likelihood_score=1, response_likelihood_reasoning="x",
                )
                # j2 deliberately absent - model didn't return a score for it.
            ])
            result = score_and_apply(session, make_profile(), RESUME, [j1, j2])

        assert result.scored == 1
        assert result.missing == 1
        assert j1.fit_score == 1
        assert j2.fit_score is None


def test_score_and_apply_empty_list_is_a_noop():
    result = score_and_apply(None, make_profile(), RESUME, [])
    assert result.scored == 0
    assert result.missing == 0


def test_score_and_apply_stamps_resume_hash(tmp_path: Path):
    """D47: every score needs a record of which resume version produced it,
    so staleness can be detected later with zero LLM cost."""
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        job = Job(
            company_id=acme.id, title="ML Engineer", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
        )
        session.add(job)
        session.commit()
        profile = make_profile()

        with patch("copilot.llm.Anthropic") as mock_client:
            mock_client.return_value.messages.parse.return_value = _score_response([
                JobScore(
                    job_id=job.id, skill_match_score=80, experience_level_score=70,
                    domain_fit_score=60, location_fit_score=90, visa_feasibility_score=50,
                    fit_score=75, reasoning="Strong match.", visa_signal="unknown",
                    response_likelihood_score=65, response_likelihood_reasoning="Fresh.",
                )
            ])
            score_and_apply(session, profile, RESUME, [job])

        assert job.scored_resume_hash == resume_content_hash(profile.resume_path)


def test_is_score_stale_treats_null_hash_as_stale():
    """A job scored before this tracking existed has no recorded hash at
    all - never claimed fresh without positive confirmation (D47)."""
    job = Job(fit_score=75.0, scored_resume_hash=None)
    assert is_score_stale(job, "some-hash")


def test_is_score_stale_false_when_hash_matches():
    job = Job(fit_score=75.0, scored_resume_hash="abc")
    assert not is_score_stale(job, "abc")


def test_is_score_stale_true_when_hash_differs():
    job = Job(fit_score=75.0, scored_resume_hash="abc")
    assert is_score_stale(job, "xyz")


def test_is_score_stale_false_when_never_scored():
    job = Job(fit_score=None, scored_resume_hash=None)
    assert not is_score_stale(job, "abc")


def test_confirm_scores_still_fresh_stamps_without_rescoring(tmp_path: Path):
    """D48: the user's override for a cosmetic-only resume edit - no LLM
    call, fit_score/reasoning untouched, only the staleness marker moves."""
    from copilot.scoring.rubric import confirm_scores_still_fresh

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        scored = Job(
            company_id=acme.id, title="A", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
            fit_score=70.0, fit_reasoning="Good match.", scored_resume_hash="old",
        )
        never_scored = Job(
            company_id=acme.id, title="B", source="greenhouse",
            apply_url="https://acme.com/2", dedupe_hash="h2",
        )
        session.add_all([scored, never_scored])
        session.commit()

        updated = confirm_scores_still_fresh(session, "new")

        assert updated == 1
        assert scored.scored_resume_hash == "new"
        assert scored.fit_score == 70.0  # untouched
        assert scored.fit_reasoning == "Good match."  # untouched
        assert never_scored.scored_resume_hash is None  # never scored, left alone


def test_confirm_scores_still_fresh_is_a_noop_when_already_current(tmp_path: Path):
    from copilot.scoring.rubric import confirm_scores_still_fresh

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        session.add(Job(
            company_id=acme.id, title="A", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
            fit_score=70.0, scored_resume_hash="current",
        ))
        session.commit()

        assert confirm_scores_still_fresh(session, "current") == 0


def test_stale_score_filter_matches_null_hash_at_sql_level(tmp_path: Path):
    """Real bug caught while building this: SQL's != is NULL (not TRUE) for
    a NULL column, so a plain `Job.scored_resume_hash != current_hash`
    silently excludes every job scored before this column existed - exactly
    the jobs the feature most needs to flag. stale_score_filter must name
    the NULL case explicitly, and this has to be checked against a real
    query, not just the Python-level is_score_stale helper."""
    from sqlalchemy import select

    from copilot.scoring.rubric import stale_score_filter

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        never_hashed = Job(
            company_id=acme.id, title="A", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
            fit_score=70.0, scored_resume_hash=None,
        )
        fresh = Job(
            company_id=acme.id, title="B", source="greenhouse",
            apply_url="https://acme.com/2", dedupe_hash="h2",
            fit_score=80.0, scored_resume_hash="current",
        )
        stale = Job(
            company_id=acme.id, title="C", source="greenhouse",
            apply_url="https://acme.com/3", dedupe_hash="h3",
            fit_score=60.0, scored_resume_hash="old",
        )
        session.add_all([never_hashed, fresh, stale])
        session.commit()

        matched = session.scalars(
            select(Job.title).where(stale_score_filter("current"))
        ).all()

    assert set(matched) == {"A", "C"}

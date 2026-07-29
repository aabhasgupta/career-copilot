from pathlib import Path

from sqlalchemy import select

from copilot.dashboard import _apply_job_filters
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Company, Job, SponsorshipStatus, VisaSignal

BASE_F = {
    "title": "", "location": "", "company": "", "min_fit": "", "age_ceiling": None,
    "hide_tracked": False, "remote_only": False, "visa": "", "industry": "",
    "salary_floor": "", "employment_type": "",
}


def _f(**overrides):
    return {**BASE_F, **overrides}


def _seeded_session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    session = get_session(engine)

    stripe = Company(name="Stripe", industry="fintech", sponsorship_status=SponsorshipStatus.sponsors)
    acme = Company(name="Acme", industry="tech", sponsorship_status=SponsorshipStatus.unknown)
    session.add_all([stripe, acme])
    session.flush()

    jobs = [
        Job(  # remote, sponsors (via company), scored high, full-time, salary in range
            company_id=stripe.id, title="ML Engineer", location="Remote US", remote=True,
            employment_type="Full-time", salary_min=150000, salary_max=180000,
            source="greenhouse", apply_url="https://x/1", dedupe_hash="h1",
            fit_score=80, visa_signal=VisaSignal.unknown,
        ),
        Job(  # on-site Chicago, explicit visa yes, contractor, unscored, no salary
            company_id=acme.id, title="Data Scientist", location="Chicago, IL", remote=False,
            employment_type="Contractor", salary_min=None, salary_max=None,
            source="lever", apply_url="https://x/2", dedupe_hash="h2",
            fit_score=None, visa_signal=VisaSignal.explicit_yes,
        ),
        Job(  # explicit no-sponsor, unknown company sponsorship, low salary
            company_id=acme.id, title="Backend Engineer", location="Austin, TX", remote=None,
            employment_type=None, salary_min=90000, salary_max=100000,
            source="jsearch", apply_url="https://x/3", dedupe_hash="h3",
            fit_score=40, visa_signal=VisaSignal.explicit_no,
        ),
    ]
    session.add_all(jobs)
    session.commit()
    return session, jobs


def test_remote_only_matches_flag_or_location_text(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(remote_only=True))).all()
    assert {j.title for j in result} == {"ML Engineer"}


def test_visa_sponsor_includes_explicit_yes_and_company_sponsors(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(visa="sponsor"))).all()
    # ML Engineer via company.sponsorship_status=sponsors, Data Scientist via explicit_yes.
    assert {j.title for j in result} == {"ML Engineer", "Data Scientist"}


def test_visa_hide_no_excludes_only_explicit_no(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(visa="hide_no"))).all()
    assert {j.title for j in result} == {"ML Engineer", "Data Scientist"}


def test_industry_filter_excludes_unclassified_and_non_matching(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(industry="fintech"))).all()
    assert {j.title for j in result} == {"ML Engineer"}


def test_company_filter_is_case_insensitive_substring(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(company="strip"))).all()
    assert {j.title for j in result} == {"ML Engineer"}


def test_title_filter_matches_substring(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(title="scientist"))).all()
    assert {j.title for j in result} == {"Data Scientist"}


def test_salary_floor_keeps_unknown_salary_jobs_visible(tmp_path: Path):
    """Matches the existing Preferences precedent (min_salary keeps jobs
    that don't state pay) - a per-view salary floor shouldn't be stricter
    than the permanent discovery-time filter it sits alongside."""
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(salary_floor="150000"))).all()
    # ML Engineer clears the floor; Data Scientist has no salary at all (kept
    # per the "never assume low pay" rule); Backend Engineer's 90-100k doesn't clear it.
    assert {j.title for j in result} == {"ML Engineer", "Data Scientist"}


def test_employment_type_excludes_unlabeled_jobs(tmp_path: Path):
    """Unlike salary/fit, an unlabeled employment_type is excluded, not kept -
    checked against real production data (docs/DECISIONS.md): keeping
    unknowns made "Contract" return 110 jobs when only ~22 were actually
    labeled contract, defeating the point of picking a specific type."""
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(employment_type="contract"))).all()
    assert {j.title for j in result} == {"Data Scientist"}


def test_employment_type_full_time_bucket(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(_apply_job_filters(select(Job), _f(employment_type="full_time"))).all()
    assert {j.title for j in result} == {"ML Engineer"}


def test_filters_compose_together(tmp_path: Path):
    session, jobs = _seeded_session(tmp_path)
    result = session.scalars(
        _apply_job_filters(select(Job), _f(visa="sponsor", remote_only=True))
    ).all()
    assert {j.title for j in result} == {"ML Engineer"}

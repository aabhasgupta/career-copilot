from pathlib import Path

from copilot.autocomplete import SEED_COMPANIES, SEED_LOCATIONS, SEED_TITLES, get_suggestions
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Company, Job


def _seeded_session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    session = get_session(engine)
    stripe = Company(name="Stripe")
    session.add(stripe)
    session.flush()
    session.add(
        Job(
            company_id=stripe.id,
            title="Fraud Data Scientist",
            source="greenhouse",
            apply_url="https://stripe.com/jobs/1",
            dedupe_hash="h1",
        )
    )
    session.commit()
    return session


def test_get_suggestions_merges_db_titles_with_seed_list(tmp_path: Path):
    session = _seeded_session(tmp_path)
    result = get_suggestions(session)
    assert "Fraud Data Scientist" in result["titles"]  # from the DB
    assert "Software Engineer" in result["titles"]  # from the static seed


def test_get_suggestions_merges_db_companies_with_seed_list(tmp_path: Path):
    session = _seeded_session(tmp_path)
    result = get_suggestions(session)
    assert "Stripe" in result["companies"]  # from the DB
    assert "Meta" in result["companies"]  # from the static seed
    assert "Microsoft" in result["companies"]  # from the static seed


def test_get_suggestions_companies_have_no_duplicates(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    session = get_session(engine)
    # A DB company that happens to collide with a seed company shouldn't double up.
    meta = Company(name="Meta")
    session.add(meta)
    session.flush()
    session.add(
        Job(
            company_id=meta.id,
            title="Software Engineer",
            source="greenhouse",
            apply_url="https://meta.com/jobs/1",
            dedupe_hash="h1",
        )
    )
    session.commit()
    result = get_suggestions(session)
    assert result["companies"].count("Meta") == 1
    assert len(SEED_COMPANIES) > 0


def test_get_suggestions_locations_are_static_seed_only(tmp_path: Path):
    session = _seeded_session(tmp_path)
    result = get_suggestions(session)
    assert result["locations"] == SEED_LOCATIONS
    assert "Remote" in result["locations"]
    assert "United States" in result["locations"]


def test_get_suggestions_includes_industry_vocabulary(tmp_path: Path):
    session = _seeded_session(tmp_path)
    result = get_suggestions(session)
    assert len(result["industries"]) > 0


def test_get_suggestions_titles_have_no_duplicates(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    session = get_session(engine)
    stripe = Company(name="Stripe")
    session.add(stripe)
    session.flush()
    # A DB title that happens to collide with a seed title shouldn't double up.
    session.add(
        Job(
            company_id=stripe.id,
            title="Software Engineer",
            source="greenhouse",
            apply_url="https://stripe.com/jobs/1",
            dedupe_hash="h1",
        )
    )
    session.commit()
    result = get_suggestions(session)
    assert result["titles"].count("Software Engineer") == 1
    assert len(SEED_TITLES) > 0

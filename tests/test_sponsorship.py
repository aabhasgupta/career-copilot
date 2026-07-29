from pathlib import Path

import pytest

from copilot.db.models import Company, SponsorshipStatus
from copilot.scoring.sponsorship import (
    FISCAL_YEAR,
    FilingStats,
    current_cached_year,
    load_filings,
    match_company,
    normalize_company_name,
    probe_newer_year,
    refresh_h1b_data,
    sync_sponsorship_data,
)

CSV_HEADER = (
    '"Fiscal Year",Employer,"Initial Approval","Initial Denial",'
    '"Continuing Approval","Continuing Denial",NAICS,"Tax ID",State,City,ZIP\n'
)


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "h1b.csv"
    path.write_text(CSV_HEADER + "".join(rows))
    return path


def test_normalize_strips_suffixes_and_filler_words():
    assert normalize_company_name("JPMorgan Chase & Co.") == "jpmorgan chase"
    assert normalize_company_name("Capital One") == "capital one"
    assert normalize_company_name("CAPITAL ONE NATIONAL ASSOCIATION") == "capital one"
    assert normalize_company_name("Stripe Inc") == "stripe"
    assert normalize_company_name("AMAZON COM SERVICES LLC") == "amazon com services"


def test_load_filings_aggregates_duplicate_employer_rows(tmp_path: Path):
    path = _write_csv(
        tmp_path,
        [
            '2023,"STRIPE INC",7,0,48,4,52,5600,CA,"SOUTH SAN FRANCISCO",94080\n',
            '2023,"STRIPE INC",0,0,6,0,52,5600,CA,"S SAN FRAN",94080\n',
            '2023,,1,0,0,0,51,8070,DE,WILMINGTON,19801\n',  # blank employer, skipped
        ],
    )
    filings = load_filings(path)
    stats = filings["stripe"]
    assert stats.initial_approval == 7
    assert stats.continuing_approval == 54
    assert stats.total_approved == 61


def test_match_company_exact():
    filings = {"stripe": FilingStats(initial_approval=7, continuing_approval=54)}
    stats = match_company("Stripe", filings)
    assert stats is not None
    assert stats.total_approved == 61


def test_match_company_prefix_combines_related_entities():
    filings = {
        "amazon advertising": FilingStats(continuing_approval=3),
        "amazon com services": FilingStats(continuing_approval=1),
        "unrelated company": FilingStats(continuing_approval=999),
    }
    stats = match_company("Amazon", filings)
    assert stats is not None
    assert stats.total_approved == 4  # only the amazon-prefixed entries


def test_match_company_rejects_short_non_distinctive_names():
    # "Blend" is 5 characters and a single word - below the distinctiveness
    # threshold, so it must not prefix-match "blend labs" and risk a false
    # positive on a coincidentally similar name.
    filings = {"blend labs": FilingStats(continuing_approval=10)}
    assert match_company("Blend", filings) is None


def test_match_company_no_match_returns_none():
    filings = {"some other co": FilingStats(continuing_approval=5)}
    assert match_company("Nonexistent Company", filings) is None


def test_sync_sponsorship_data_updates_matched_leaves_unmatched_unknown(tmp_path: Path):
    from copilot.db import get_engine, get_session, init_db

    csv_path = _write_csv(
        tmp_path,
        [
            '2023,"STRIPE INC",7,0,48,4,52,5600,CA,"SOUTH SAN FRANCISCO",94080\n',
        ],
    )
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        session.add(Company(name="Stripe"))
        session.add(Company(name="A Totally Unknown Startup"))
        session.commit()

        summary = sync_sponsorship_data(session, cache_path=csv_path)
        assert summary.companies_checked == 2
        assert summary.matched == 1

        stripe = session.query(Company).filter_by(name="Stripe").one()
        assert stripe.h1b_filing_count == 55
        assert stripe.sponsorship_status == SponsorshipStatus.sponsors
        assert "55 H1B petitions approved" in stripe.sponsorship_evidence

        unknown = session.query(Company).filter_by(name="A Totally Unknown Startup").one()
        assert unknown.h1b_filing_count is None
        assert unknown.sponsorship_status == SponsorshipStatus.unknown


def test_sync_sponsorship_data_labels_evidence_with_explicit_year(tmp_path: Path):
    from copilot.db import get_engine, get_session, init_db

    csv_path = _write_csv(
        tmp_path, ['2023,"STRIPE INC",7,0,48,4,52,5600,CA,"SOUTH SAN FRANCISCO",94080\n']
    )
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        session.add(Company(name="Stripe"))
        session.commit()
        sync_sponsorship_data(session, cache_path=csv_path, year=2025)
        stripe = session.query(Company).filter_by(name="Stripe").one()
        assert "FY2025" in stripe.sponsorship_evidence


def test_current_cached_year_returns_default_when_nothing_cached(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("copilot.scoring.sponsorship.DATA_DIR", tmp_path)
    assert current_cached_year() == FISCAL_YEAR


def test_current_cached_year_reads_newest_cached_filename(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("copilot.scoring.sponsorship.DATA_DIR", tmp_path)
    (tmp_path / "h1b_data_hub_fy2023.csv").write_text("x")
    (tmp_path / "h1b_data_hub_fy2025.csv").write_text("x")
    (tmp_path / "h1b_data_hub_fy2024.csv").write_text("x")
    assert current_cached_year() == 2025


def test_probe_newer_year_returns_newest_reachable(monkeypatch):
    reachable = {2025}
    monkeypatch.setattr(
        "copilot.scoring.sponsorship._year_is_published", lambda year: year in reachable
    )
    monkeypatch.setattr("copilot.scoring.sponsorship.date", _FixedDate)
    assert probe_newer_year(2023) == 2025


def test_probe_newer_year_returns_none_when_nothing_newer(monkeypatch):
    monkeypatch.setattr("copilot.scoring.sponsorship._year_is_published", lambda year: False)
    monkeypatch.setattr("copilot.scoring.sponsorship.date", _FixedDate)
    assert probe_newer_year(2023) is None


def test_refresh_h1b_data_downloads_newer_year_when_found(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("copilot.scoring.sponsorship.DATA_DIR", tmp_path)
    monkeypatch.setattr("copilot.scoring.sponsorship.probe_newer_year", lambda after_year: 2025)
    downloaded = {}
    monkeypatch.setattr(
        "copilot.scoring.sponsorship.download_data",
        lambda year, force=False: downloaded.setdefault("call", (year, force)),
    )
    year, found_newer = refresh_h1b_data()
    assert year == 2025
    assert found_newer is True
    assert downloaded["call"] == (2025, True)


def test_refresh_h1b_data_reverifies_current_year_when_nothing_newer(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("copilot.scoring.sponsorship.DATA_DIR", tmp_path)
    (tmp_path / "h1b_data_hub_fy2023.csv").write_text("x")
    monkeypatch.setattr("copilot.scoring.sponsorship.probe_newer_year", lambda after_year: None)
    downloaded = {}
    monkeypatch.setattr(
        "copilot.scoring.sponsorship.download_data",
        lambda year, force=False: downloaded.setdefault("call", (year, force)),
    )
    year, found_newer = refresh_h1b_data()
    assert year == 2023
    assert found_newer is False
    assert downloaded["call"] == (2023, True)


class _FixedDate:
    @staticmethod
    def today():
        import datetime

        return datetime.date(2026, 7, 27)

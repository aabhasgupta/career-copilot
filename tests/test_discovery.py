from copilot.db.models import ATSType, SalarySource
from copilot.discovery.adzuna import _normalize as normalize_adzuna
from copilot.discovery.ats import detect_ats
from copilot.discovery.dedupe import dedupe_hash
from copilot.discovery.jsearch import _normalize as normalize_jsearch
from copilot.discovery.models import DiscoveredJob


def test_dedupe_hash_stable_and_case_insensitive():
    a = dedupe_hash("Acme Inc", "Software Engineer", "Chicago, IL")
    b = dedupe_hash(" acme inc ", "SOFTWARE ENGINEER", "chicago, il")
    assert a == b


def test_dedupe_hash_differs_on_location():
    a = dedupe_hash("Acme Inc", "Software Engineer", "Chicago, IL")
    b = dedupe_hash("Acme Inc", "Software Engineer", "Austin, TX")
    assert a != b


def test_detect_ats_greenhouse():
    ats_type, slug = detect_ats("https://boards.greenhouse.io/acme/jobs/12345")
    assert ats_type == ATSType.greenhouse
    assert slug == "acme"


def test_detect_ats_lever():
    ats_type, slug = detect_ats("https://jobs.lever.co/acme/abcd-1234")
    assert ats_type == ATSType.lever
    assert slug == "acme"


def test_detect_ats_unknown_for_aggregator_proxy():
    ats_type, slug = detect_ats("https://www.adzuna.com/land/ad/12345?utm_medium=api")
    assert ats_type == ATSType.unknown
    assert slug is None


def test_normalize_adzuna_marks_predicted_salary_as_estimate():
    result = {
        "title": "Machine Learning Engineer",
        "company": {"display_name": "Orchard Robotics"},
        "location": {"display_name": "San Francisco, California"},
        "redirect_url": "https://www.adzuna.com/land/ad/123",
        "salary_min": 150000,
        "salary_max": 160000,
        "salary_is_predicted": "1",
        "created": "2026-07-19T12:51:56Z",
        "description": "Build ML systems.",
    }
    job = normalize_adzuna(result, "San Francisco")
    assert job.salary_source == SalarySource.aggregator_estimate
    assert job.salary_currency == "USD"
    assert job.company_name == "Orchard Robotics"


def test_normalize_adzuna_no_salary_is_unknown():
    result = {
        "title": "Data Scientist",
        "company": {"display_name": "Acme"},
        "location": {"display_name": "Remote"},
        "redirect_url": "https://www.adzuna.com/land/ad/456",
    }
    job = normalize_adzuna(result, "Remote")
    assert job.salary_source == SalarySource.unknown
    assert job.salary_min is None
    assert job.remote is True  # "Remote" in location display name


def test_normalize_jsearch_basic_fields():
    result = {
        "job_title": "AI Engineer",
        "employer_name": "PwC",
        "job_location": "Chicago, IL",
        "job_is_remote": False,
        "job_employment_type": "Full-time",
        "job_min_salary": 63000,
        "job_max_salary": 142000,
        "job_description": "Build AI systems.",
        "job_apply_link": "https://www.builtinchicago.org/job/123",
        "job_posted_at_datetime_utc": "2026-07-17T00:00:00.000Z",
    }
    job = normalize_jsearch(result)
    assert job.title == "AI Engineer"
    assert job.company_name == "PwC"
    assert job.salary_source == SalarySource.posted
    assert job.salary_currency == "USD"
    assert job.source == "jsearch"


def test_hits_dealbreaker_matches_compiled_rules():
    from copilot.discovery.pipeline import _hits_dealbreaker
    from copilot.rules import CompiledRule, Matcher, RuleField

    job = DiscoveredJob(
        title="Staffing Agency Recruiter",
        company_name="Acme",
        source="adzuna",
        apply_url="https://example.com",
    )
    staffing_rule = CompiledRule(
        source="no staffing agencies",
        matchers=[Matcher(field=RuleField.text, patterns=["staffing agency"])],
    )
    clearance_rule = CompiledRule(
        source="no clearance jobs",
        matchers=[Matcher(field=RuleField.text, patterns=["clearance"])],
    )
    assert _hits_dealbreaker(job, [staffing_rule])
    assert not _hits_dealbreaker(job, [clearance_rule])


def test_slug_candidates_strip_legal_suffixes():
    from copilot.discovery.ats_resolver import slug_candidates

    assert "accenturefederalservices" in slug_candidates("Accenture Federal Services")
    assert slug_candidates("Blend") == ["blend"]


def test_slug_candidates_full_name_only():
    from copilot.discovery.ats_resolver import slug_candidates

    candidates = slug_candidates("Palm Venture Studios")
    assert candidates[0] == "palmventurestudios"
    # The risky first-word guess is not part of the trusted candidate set
    assert "palm" not in candidates


def test_loose_slug_candidates_first_word_fallback():
    from copilot.discovery.ats_resolver import loose_slug_candidates

    assert loose_slug_candidates("Inabia Solutions and Consulting, Inc.") == ["inabia"]
    # Single-word names have no separate fallback
    assert loose_slug_candidates("Blend") == []


def test_titles_match_normalized_containment():
    from copilot.discovery.pipeline import _titles_match

    assert _titles_match("Machine Learning Engineer", "Senior Machine Learning Engineer")
    assert _titles_match("AI Engineer", "ai engineer")
    assert not _titles_match("Machine Learning Engineer", "Account Executive")


def test_strip_html_unescapes_greenhouse_content():
    from copilot.discovery.ats_boards import _strip_html

    raw = "&lt;div&gt;&lt;p&gt;Build ML systems.&lt;/p&gt;&lt;/div&gt;"
    assert _strip_html(raw) == "Build ML systems."
    assert _strip_html(None) is None


def test_below_salary_floor_unknown_never_trips():
    from copilot.discovery.pipeline import _below_salary_floor

    assert _below_salary_floor(None, 140000, 150000)
    assert _below_salary_floor(120000, None, 150000)
    assert not _below_salary_floor(None, None, 150000)  # unknown: kept (D10)
    assert not _below_salary_floor(None, 150000, 150000)
    assert not _below_salary_floor(100000, 200000, 150000)  # best case above floor
    assert not _below_salary_floor(None, 100000, None)  # no floor set


def test_prune_jobs_deletes_violators_keeps_applied(tmp_path, monkeypatch):
    from copilot.config import Profile
    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import Application, Company, Job
    from copilot.discovery.pipeline import prune_jobs
    import copilot.rules as rules_mod

    # Hermetic: use the no-LLM fallback compilation instead of cache/API
    monkeypatch.setattr(
        rules_mod,
        "load_dealbreaker_rules",
        lambda profile, cache_path=None: (
            rules_mod._fallback(profile.search.dealbreakers),
            [],
        ),
    )

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {
                "titles": ["Engineer"],
                "min_salary": 150000,
                "dealbreakers": ["clearance required"],
            },
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
        }
    )
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        company = Company(name="Acme")
        session.add(company)
        session.flush()

        def job(dedupe, **kw):
            j = Job(
                company_id=company.id, title="Engineer", source="adzuna",
                apply_url="x", dedupe_hash=dedupe, **kw,
            )
            session.add(j)
            return j

        keep = job("a", salary_max=200000)
        keep_unknown = job("b")
        low = job("c", salary_max=100000)
        breaker = job("d", jd_text="Active clearance required for this role")
        low_but_applied = job("e", salary_max=90000)
        session.flush()
        session.add(Application(job_id=low_but_applied.id))
        session.commit()

        summary = prune_jobs(profile, session)
        assert summary.below_salary_floor == 1
        assert summary.dealbreakers == 1
        remaining = {j.dedupe_hash for j in session.query(Job).all()}
        assert remaining == {"a", "b", "e"}


def test_normalize_remotive_salary_and_eligibility():
    from copilot.discovery.remote_boards import (
        _normalize_remotive,
        _parse_salary_text,
        _us_eligible,
    )

    assert _parse_salary_text("$120k-$150k") == (120000, 150000)
    assert _parse_salary_text("$36k") == (36000, 36000)
    assert _parse_salary_text("Competitive + 401k") == (None, None)
    assert _parse_salary_text(None) == (None, None)

    assert _us_eligible("USA Only")
    assert _us_eligible("Worldwide")
    assert _us_eligible(None)
    assert not _us_eligible("Europe")
    assert not _us_eligible("Philippines")

    job = _normalize_remotive(
        {
            "title": "ML Engineer",
            "company_name": "Acme",
            "candidate_required_location": "USA",
            "salary": "$140k-$180k",
            "url": "https://remotive.com/j/1",
            "publication_date": "2026-07-16T13:28:02",
            "description": "<p>Build ML systems.</p>",
        }
    )
    assert job.remote is True
    assert job.salary_min == 140000 and job.salary_max == 180000
    assert job.jd_text == "Build ML systems."
    assert job.source == "remotive"


def test_normalize_remoteok_zero_salary_is_unknown():
    from copilot.db.models import SalarySource
    from copilot.discovery.remote_boards import _normalize_remoteok

    job = _normalize_remoteok(
        {
            "position": "LLM Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "salary_min": 0,
            "salary_max": 0,
            "epoch": 1784246400,
            "url": "https://remoteok.com/j/1",
            "description": "<b>Great role</b>",
        }
    )
    assert job.salary_min is None and job.salary_max is None
    assert job.salary_source == SalarySource.unknown
    assert job.source == "remoteok"


def test_is_non_us_blocks_countries_keeps_us():
    from copilot.discovery.locations import is_non_us

    assert is_non_us("Remote Canada")
    assert is_non_us("London, United Kingdom")
    assert is_non_us("IL-Israel-Remote")
    assert is_non_us("Bengaluru, Karnataka, India")
    assert is_non_us("Remote - EMEA")
    assert not is_non_us("Americas, Europe, Israel")  # Americas = US-eligible

    assert not is_non_us("Chicago, IL")
    assert not is_non_us("Peru, IL")  # Illinois town, not the country
    assert not is_non_us("Albuquerque, New Mexico")
    assert not is_non_us("Remote US")
    assert not is_non_us("Anywhere")
    assert not is_non_us(None)


def test_is_non_us_bare_canadian_cities():
    from copilot.discovery.locations import is_non_us

    assert is_non_us("Toronto")
    assert is_non_us("Vancouver")
    assert not is_non_us("Seattle")


def test_quota_exhaustion_reported_once_and_source_skipped(tmp_path, monkeypatch):
    import httpx

    import copilot.discovery.pipeline as pipeline
    from copilot.config import Profile
    from copilot.db import get_engine, get_session, init_db

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["AI Engineer", "Data Scientist"], "locations": ["United States"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
        }
    )

    calls = {"jsearch": 0}

    def quota_hit(*args, **kwargs):
        calls["jsearch"] += 1
        req = httpx.Request("GET", "https://jsearch.p.rapidapi.com/search-v2")
        raise httpx.HTTPStatusError(
            "429 Too Many Requests", request=req, response=httpx.Response(429, request=req)
        )

    def one_job(title, location, *args, **kwargs):
        return [
            DiscoveredJob(
                title=title, company_name="Acme", location=location,
                source="adzuna", apply_url=f"https://x.com/{title}",
            )
        ]

    monkeypatch.setattr(pipeline, "search_jsearch", quota_hit)
    monkeypatch.setattr(pipeline, "search_adzuna", one_job)
    import copilot.discovery.remote_boards as rb
    monkeypatch.setattr(rb, "search_remotive", lambda titles: [])
    monkeypatch.setattr(rb, "fetch_remoteok", lambda titles: [])

    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    with get_session(engine) as session:
        summary = pipeline.run_discovery(
            profile, session,
            adzuna_app_id="id", adzuna_app_key="key", jsearch_api_key="key",
        )

    # Quota reported once as informational, not per query, and not an error;
    # JSearch skipped after the first hit; Adzuna still ingested both titles.
    assert summary.quota_exhausted == ["JSearch"]
    assert calls["jsearch"] == 1
    assert summary.errors == []
    assert summary.added == 2


def test_upsert_job_merges_adzuna_location_split_siblings(tmp_path):
    """Real bug, caught live: Adzuna splits one multi-location posting
    (Capital One "Sr. Manager, AI Engineer") into several results, each
    tagged with a different hyper-local neighborhood name instead of a real
    city - without this, each one became its own Job row."""
    from copilot.db import get_engine, get_session, init_db
    from copilot.discovery.pipeline import _get_or_create_company, _upsert_job

    jd_text = "x" * 150  # same real JD text both times - the actual signal
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    with get_session(engine) as session:
        first = DiscoveredJob(
            title="Sr. Manager, AI Engineer", company_name="Capital One",
            location="Pimmit, Fairfax County", source="adzuna",
            apply_url="https://adzuna.com/1", jd_text=jd_text,
        )
        second = DiscoveredJob(
            title="Sr. Manager, AI Engineer", company_name="Capital One",
            location="Prince, Manhattan", source="adzuna",
            apply_url="https://adzuna.com/2", jd_text=jd_text,
        )
        company = _get_or_create_company(session, first)
        assert _upsert_job(session, first, company) is True
        assert _upsert_job(session, second, company) is False  # merged, not a new row
        session.commit()

        from sqlalchemy import select

        from copilot.db.models import Job
        rows = session.scalars(select(Job).where(Job.company_id == company.id)).all()

    assert len(rows) == 1
    assert rows[0].location == "Pimmit, Fairfax County | Prince, Manhattan"


def test_upsert_job_does_not_merge_different_postings_with_short_jd(tmp_path):
    """The merge is gated on substantial JD-text equality specifically to
    avoid merging two genuinely different, short/empty-JD postings that
    happen to share a title."""
    from copilot.db import get_engine, get_session, init_db
    from copilot.discovery.pipeline import _get_or_create_company, _upsert_job

    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    with get_session(engine) as session:
        first = DiscoveredJob(
            title="Data Scientist", company_name="Acme", location="Austin, TX",
            source="adzuna", apply_url="https://adzuna.com/1", jd_text="short",
        )
        second = DiscoveredJob(
            title="Data Scientist", company_name="Acme", location="Denver, CO",
            source="adzuna", apply_url="https://adzuna.com/2", jd_text="short",
        )
        company = _get_or_create_company(session, first)
        assert _upsert_job(session, first, company) is True
        assert _upsert_job(session, second, company) is True  # distinct, not merged

        from sqlalchemy import select

        from copilot.db.models import Job
        rows = session.scalars(select(Job).where(Job.company_id == company.id)).all()

    assert len(rows) == 2


def test_detect_ats_workday_from_myworkdayjobs_url():
    ats_type, slug = detect_ats(
        "https://ntrs.wd1.myworkdayjobs.com/northerntrust/job/Chicago-IL/Foo_R123"
    )
    assert ats_type == ATSType.workday
    assert slug == "ntrs:northerntrust"


def _mock_client(handler):
    import httpx

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_smartrecruiters_rejects_empty_result():
    from copilot.discovery.ats_resolver import _probe_smartrecruiters

    import httpx

    def handler(request):
        return httpx.Response(200, json={"totalFound": 0, "content": []})

    with _mock_client(handler) as client:
        assert not _probe_smartrecruiters("nosuchcompany", "No Such Company", client)


def test_probe_smartrecruiters_rejects_name_collision():
    """SmartRecruiters returns 200 for any slug - a real posting existing
    under that slug is not enough, the echoed company name must actually
    match (same lesson as the Greenhouse "capital" collision, D3)."""
    from copilot.discovery.ats_resolver import _probe_smartrecruiters

    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [{"name": "Some Role", "company": {"name": "Unrelated Co"}}],
            },
        )

    with _mock_client(handler) as client:
        assert not _probe_smartrecruiters("geico", "GEICO", client)


def test_probe_smartrecruiters_accepts_matching_company():
    from copilot.discovery.ats_resolver import _probe_smartrecruiters

    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [{"name": "Licensed Insurance Agent", "company": {"name": "GEICO"}}],
            },
        )

    with _mock_client(handler) as client:
        assert _probe_smartrecruiters("geico", "GEICO", client)


def test_resolve_company_ats_finds_smartrecruiters_match():
    from copilot.discovery.ats_resolver import resolve_company_ats

    import httpx

    def handler(request):
        if "greenhouse" in str(request.url) or "lever" in str(request.url):
            return httpx.Response(404)
        if "ashbyhq" in str(request.url):
            return httpx.Response(404)
        if "smartrecruiters" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [{"name": "Role", "company": {"name": "GEICO"}}],
                },
            )
        return httpx.Response(404)

    with _mock_client(handler) as client:
        result = resolve_company_ats("GEICO", client)
    assert result == (ATSType.smartrecruiters, "geico")


def test_fetch_smartrecruiters_skips_postings_outside_title_filter():
    """Full-detail fetches cost a second call each - with a title_filter,
    only postings that could match should trigger that second call."""
    from copilot.discovery.ats_boards import fetch_board_postings

    import httpx

    detail_calls = []

    def handler(request):
        url = str(request.url)
        if request.url.path.endswith("/postings"):
            return httpx.Response(
                200,
                json={
                    "totalFound": 2,
                    "content": [
                        {"id": "1", "name": "Machine Learning Engineer"},
                        {"id": "2", "name": "Licensed Insurance Agent"},
                    ],
                },
            )
        detail_calls.append(url)
        return httpx.Response(
            200,
            json={
                "location": {"fullLocation": "Chicago, IL"},
                "postingUrl": "https://jobs.smartrecruiters.com/acme/1",
                "jobAd": {"sections": {"jobDescription": {"text": "Build models."}}},
                "releasedDate": "2026-07-01T00:00:00.000Z",
            },
        )

    with _mock_client(handler) as client:
        postings = fetch_board_postings(
            ATSType.smartrecruiters, "acme", client, ["Machine Learning Engineer"]
        )

    assert len(postings) == 1
    assert postings[0].title == "Machine Learning Engineer"
    assert postings[0].jd_text == "Build models."
    assert len(detail_calls) == 1  # the non-matching posting never got a detail call


def test_fetch_workday_parses_posting_detail():
    from copilot.discovery.ats_boards import fetch_board_postings

    import httpx

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Machine Learning Engineer",
                            "externalPath": "/job/Chicago-IL/MLE_R1",
                            "locationsText": "Chicago, IL",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": "Machine Learning Engineer",
                    "location": "Chicago, IL",
                    "remoteType": "Remote",
                    "externalUrl": "https://acme.wd1.myworkdayjobs.com/acme/job/MLE_R1",
                    "jobDescription": "<p>Build models.</p>",
                    "startDate": "2026-07-01",
                }
            },
        )

    with _mock_client(handler) as client:
        postings = fetch_board_postings(
            ATSType.workday, "acme:acme", client, ["Machine Learning Engineer"]
        )

    assert len(postings) == 1
    posting = postings[0]
    assert posting.apply_url == "https://acme.wd1.myworkdayjobs.com/acme/job/MLE_R1"
    assert posting.jd_text == "Build models."
    assert posting.remote is True
    assert posting.posted_at is not None

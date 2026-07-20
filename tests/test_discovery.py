from copilot.db.models import ATSType, SalarySource
from copilot.discovery.adzuna import _normalize as normalize_adzuna
from copilot.discovery.ats import detect_ats
from copilot.discovery.dedupe import dedupe_hash
from copilot.discovery.jsearch import _normalize as normalize_jsearch
from copilot.discovery.pipeline import _hits_dealbreaker
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


def test_hits_dealbreaker_matches_title_or_jd_text():
    job = DiscoveredJob(
        title="Staffing Agency Recruiter",
        company_name="Acme",
        source="adzuna",
        apply_url="https://example.com",
    )
    assert _hits_dealbreaker(job, ["staffing agency"])
    assert not _hits_dealbreaker(job, ["clearance required"])


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

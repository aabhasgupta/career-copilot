from copilot.resume import ContactInfo, ResumeProfile, WorkExperience
from copilot.scoring.rubric import JobToScore, _job_block, _resume_block

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
    block = _resume_block(RESUME)
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

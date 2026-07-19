"""Suggest target job titles by combining the resume profile with a live web
search for what's currently in demand. Unlike resume.py's extraction (which
only reports facts already in the document), this is a genuine research step:
Claude decides what to search for and how many searches to run, then proposes
titles it wouldn't have generated from the resume alone.

This output is a judgment call, not a fact - the CLI always shows it and asks
before writing anything to profile.yaml (see cli.py's `suggest-titles` command).
"""

from __future__ import annotations

import json
import re

from anthropic import Anthropic
from pydantic import BaseModel

from copilot.config import Profile
from copilot.resume import ResumeProfile

MAX_PAUSE_RESUMES = 3

SUGGEST_PROMPT_TEMPLATE = """A candidate has this background:

Seniority: {seniority} ({years:g} years of experience)
Skills: {skills}
Domains worked in: {domains}
Recent titles held: {recent_titles}
Summary: {summary}

Using web search to check current job postings and market trends (not just
your training knowledge), suggest 5-8 target job titles for this candidate's
job search. Each title should be:
- A real, actively-used title you can find in current postings (not internal
  jargon or a title nobody actually posts jobs under)
- A genuine fit for this candidate's actual skills and experience level
- Chosen because there is real market demand for it right now, not just
  theoretically plausible

Rank by a combination of fit and demand. For each title give one-line reasoning
covering both why it fits this candidate and what you found searching for
current demand.

End your response with exactly one fenced JSON code block, and nothing after
it, in this shape:

```json
[
  {{"title": "...", "reasoning": "...", "demand_signal": "..."}}
]
```
"""


class TitleSuggestion(BaseModel):
    title: str
    reasoning: str
    demand_signal: str = ""


def _extract_json_block(text: str) -> list[dict]:
    match = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
    if not match:
        raise RuntimeError(
            "Could not find a JSON suggestions block in the model's response."
        )
    return json.loads(match.group(1))


def suggest_target_titles(profile: Profile, resume: ResumeProfile) -> list[TitleSuggestion]:
    """Ask Claude to research and propose target titles. Runs live web searches."""
    client = Anthropic()
    prompt = SUGGEST_PROMPT_TEMPLATE.format(
        seniority=resume.seniority,
        years=resume.years_of_experience,
        skills=", ".join(resume.skills),
        domains=", ".join(resume.domains) or "none stated",
        recent_titles=", ".join(w.title for w in resume.work_experience[:3]) or "none stated",
        summary=resume.summary,
    )

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    messages: list[dict] = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=profile.llm.model, max_tokens=4096, tools=tools, messages=messages
    )
    # Web search is server-executed; a pause_turn just means Claude's internal
    # search loop hit its iteration cap and wants to keep going - resend as-is
    # per the documented resume pattern (no new user message).
    resumes = 0
    while response.stop_reason == "pause_turn" and resumes < MAX_PAUSE_RESUMES:
        messages.append({"role": "assistant", "content": response.content})
        response = client.messages.create(
            model=profile.llm.model, max_tokens=4096, tools=tools, messages=messages
        )
        resumes += 1

    text = "".join(block.text for block in response.content if block.type == "text")
    raw_suggestions = _extract_json_block(text)
    return [TitleSuggestion.model_validate(item) for item in raw_suggestions]

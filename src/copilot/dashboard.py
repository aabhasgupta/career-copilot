"""Local web dashboard (D11). First slice: a preferences editor for
profile.yaml's search section, so tuning titles/locations/salary/preferences
is a form on localhost instead of a YAML edit.

Design constraints:
- Same source of truth: reads and writes profile.yaml through the same
  comment-preserving ruamel helpers the CLI uses. No second config store.
- Validate before write: the merged result must pass Profile.model_validate
  or nothing is saved and the error is shown.
- Localhost only; single user; no auth (D4). Fully self-contained page -
  no CDN fonts/scripts.
"""

from __future__ import annotations

import html
from pathlib import Path

import yaml as pyyaml
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot.config import Profile
from copilot.industry import Industry
from copilot.profile_fill import update_search_preferences

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Career Copilot</title>
<style>
  :root {{
    --ink: #16182d; --muted: #6b7186; --line: #e4e6ef;
    --accent: #4f5bd5; --accent-2: #7a5cd6; --bg: #f3f4fa;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "SF Pro Text", "Segoe UI", system-ui,
          sans-serif; margin: 0; background: var(--bg); color: var(--ink); }}
  header {{
    background: linear-gradient(120deg, #171a38 0%, #2c2a6b 55%, #4f3d8f 100%);
    color: #fff; padding: 2.6rem 1.5rem 2.9rem;
  }}
  .shell {{ max-width: 780px; margin: 0 auto; }}
  .brand {{ font-size: 2rem; font-weight: 750; letter-spacing: -.02em; }}
  .brand .mark {{
    display: inline-block; margin-right: .55rem; transform: translateY(2px);
  }}
  .brand em {{ font-style: normal; color: #b9c0ff; }}
  .tagline {{ margin-top: .45rem; color: #c9cdea; font-size: .95rem;
              letter-spacing: .01em; }}
  .tagline b {{ color: #fff; font-weight: 600; }}
  main {{ max-width: 780px; margin: -1.4rem auto 3rem; padding: 0 1.5rem; }}
  .card {{
    background: #fff; border: 1px solid var(--line); border-radius: 14px;
    padding: 1.4rem 1.5rem 1.5rem; margin-bottom: 1.1rem;
    box-shadow: 0 6px 18px rgba(26, 31, 71, .06);
  }}
  .card h2 {{
    margin: 0 0 .2rem; font-size: 1.02rem; letter-spacing: .01em;
  }}
  .card .sub {{ margin: 0 0 .9rem; color: var(--muted); font-size: .86rem; }}
  label {{ display: block; font-weight: 600; font-size: .9rem;
           margin: 1rem 0 .3rem; }}
  label:first-of-type {{ margin-top: 0; }}
  .hint {{ font-weight: 400; color: var(--muted); font-size: .82rem; }}
  textarea, input[type=number], select {{
    width: 100%; padding: .55rem .7rem; border: 1px solid var(--line);
    border-radius: 8px; font: inherit; font-size: .92rem; background: #fbfbfe;
  }}
  textarea {{ min-height: 5.2rem; resize: vertical; line-height: 1.5; }}
  textarea:focus, input:focus, select:focus {{
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(79, 91, 213, .15); background: #fff;
  }}
  .check {{ display: flex; align-items: center; gap: .55rem; margin-top: 1rem;
            font-weight: 600; font-size: .9rem; }}
  .check input {{ width: 1.05rem; height: 1.05rem; accent-color: var(--accent); }}
  .actions {{ display: flex; align-items: center; gap: 1rem; }}
  button {{
    padding: .65rem 2rem; border: 0; border-radius: 9px; color: #fff;
    background: linear-gradient(120deg, var(--accent), var(--accent-2));
    font: inherit; font-weight: 650; cursor: pointer;
    box-shadow: 0 4px 12px rgba(79, 91, 213, .35);
  }}
  button:hover {{ filter: brightness(1.07); }}
  .footnote {{ color: var(--muted); font-size: .8rem; }}
  .flash {{ padding: .7rem 1rem; border-radius: 10px; margin-bottom: 1.1rem;
            font-size: .9rem; }}
  .ok {{ background: #e7f6ec; color: #1d5e33; border: 1px solid #bfe5cb; }}
  .err {{ background: #fdecec; color: #8a2323; border: 1px solid #f2c7c7;
          white-space: pre-wrap; }}
</style>
</head>
<body>
<header>
  <div class="shell">
    <div class="brand"><span class="mark">&#x1F9ED;</span>Career <em>Copilot</em></div>
    <div class="tagline">discover &middot; rank &middot; prepare — <b>you always click submit</b></div>
  </div>
</header>
<main>
{flash}
<form method="post" action="/save">

<div class="card">
<h2>What to search for</h2>
<p class="sub">Discovery queries every title in every location on each
<code>copilot discover</code> run.</p>
<label>Target titles <span class="hint">one per line</span></label>
<textarea name="titles">{titles}</textarea>
<label>Search locations <span class="hint">one per line, e.g. "United States"</span></label>
<textarea name="locations">{locations}</textarea>
</div>

<div class="card">
<h2>Filters &mdash; drop jobs entirely</h2>
<p class="sub">Applied at discovery; run <code>copilot jobs prune</code> after
tightening to re-apply to stored jobs.</p>
<label>Salary floor (USD) <span class="hint">jobs with a known salary below this
are dropped; jobs that don't state pay are always kept. Blank = no floor.</span></label>
<input name="min_salary" type="number" value="{min_salary}">
<label>Dealbreakers <span class="hint">plain English, one per line - e.g.
"don't give me jobs based in Alabama", "no clearance jobs", "nothing at Meta".
Claude compiles these into precise filters on the next discover/prune.</span></label>
<textarea name="dealbreakers">{dealbreakers}</textarea>
</div>

<div class="card">
<h2>Preferences &mdash; reorder, never hide</h2>
<p class="sub">Listings sort by these instantly; unmatched jobs go last but
stay visible.</p>
<label>Location preference <span class="hint">one per line, most preferred first:
"remote", "within 30 miles of Place, ST", or plain location text</span></label>
<textarea name="location_preference">{location_preference}</textarea>
<label>Industry preference <span class="hint">one per line, most preferred first.
Vocabulary: {industry_vocab}</span></label>
<textarea name="industry_preference">{industry_preference}</textarea>
<label>Company preference <span class="hint">one per line, most preferred first -
their listings rank higher and their public job boards (Greenhouse/Lever/Ashby)
are watched directly</span></label>
<textarea name="company_preference">{company_preference}</textarea>
<label class="check"><input type="checkbox" name="deprioritize_staffing"
{staffing_checked}> Rank direct employers above staffing agencies</label>
</div>

<div class="actions">
  <button type="submit">Save</button>
  <span class="footnote">writes profile.yaml &middot; comments preserved &middot;
  validated before saving</span>
</div>
</form>
</main>
</body>
</html>"""


def _lines(values: list[str]) -> str:
    return html.escape("\n".join(values))


def _parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def create_app(profile_path: Path = Path("profile.yaml")) -> FastAPI:
    app = FastAPI(title="Career Copilot")

    def render(flash: str = "") -> str:
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        search = profile.search
        return _PAGE.format(
            flash=flash,
            titles=_lines(search.titles),
            locations=_lines(search.locations),
            min_salary=search.min_salary if search.min_salary is not None else "",
            dealbreakers=_lines(search.dealbreakers),
            location_preference=_lines(search.location_preference),
            industry_preference=_lines(search.industry_preference),
            company_preference=_lines(search.company_preference),
            industry_vocab=", ".join(i.value for i in Industry),
            staffing_checked="checked" if search.deprioritize_staffing else "",
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render()

    @app.get("/saved", response_class=HTMLResponse)
    def saved() -> str:
        return render(flash='<div class="flash ok">Saved. Stricter filters take '
                      "effect on stored jobs after <code>copilot jobs prune</code>.</div>")

    @app.post("/save")
    def save(
        titles: str = Form(""),
        locations: str = Form(""),
        min_salary: str = Form(""),
        dealbreakers: str = Form(""),
        location_preference: str = Form(""),
        industry_preference: str = Form(""),
        company_preference: str = Form(""),
        deprioritize_staffing: str | None = Form(None),
    ):
        updates = {
            "titles": _parse_lines(titles),
            "locations": _parse_lines(locations),
            "min_salary": int(min_salary) if min_salary.strip() else None,
            "dealbreakers": _parse_lines(dealbreakers),
            "location_preference": _parse_lines(location_preference),
            "industry_preference": _parse_lines(industry_preference),
            "company_preference": _parse_lines(company_preference),
            "deprioritize_staffing": deprioritize_staffing is not None,
        }

        # Validate the merged profile before touching the file.
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        raw["search"] = {**raw.get("search", {}), **updates}
        try:
            Profile.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - shown to the user, nothing written
            flash = f'<div class="flash err">Not saved:\n{html.escape(str(exc))}</div>'
            return HTMLResponse(render(flash))

        update_search_preferences(profile_path, updates)
        return RedirectResponse("/saved", status_code=303)

    return app

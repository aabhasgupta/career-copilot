# Applying with the base packet

Career Copilot prepares content; it never touches an application form. This is a short guide to
closing that last gap - getting `data/packet_base.md`'s content into an actual ATS form - either
by hand or with a free autofill tool. See docs/DECISIONS.md D22/D23 for why this is deliberately
out of scope for Career Copilot itself.

## Option 1: manual copy-paste

Open `data/packet_base.md` (or the same content on the dashboard's Preferences page) alongside the
job's apply page and copy the relevant fields across:

- Contact details -> name/email/phone/links fields
- Work authorization Q&A -> the "are you authorized to work" / "will you need sponsorship"
  questions almost every US ATS form asks
- Professional summary -> a "tell us about yourself" field or cover letter upload, if the form has
  one

This is the simplest option and works on every ATS, including ones without public APIs (Ashby,
Workday, company-custom forms).

## Option 2: Simplify (free autofill extension)

[Simplify](https://simplify.jobs/) is a free browser extension that autofills job applications on
Greenhouse, Lever, Workday, and most other major ATSes directly from a profile you fill in once.

1. Install the Simplify extension for your browser from simplify.jobs.
2. Fill in Simplify's own profile once, using `data/packet_base.md` as your source: contact
   details, work authorization answers, and resume upload.
3. From then on, when you land on a supported job's application page, click the Simplify button in
   your browser toolbar - it fills in the fields it recognizes. Review everything it filled before
   submitting; autofill tools occasionally mis-map a field, and you're always the one who clicks
   submit regardless (docs/DECISIONS.md D1).
4. For anything Simplify doesn't cover (posting-specific questions, custom fields), fill those in
   yourself - Career Copilot doesn't attempt to draft answers to questions it can't see (D22).

## After applying

Run `copilot jobs mark-applied <job-id>` (or click "Mark applied" on the job's detail page in the
dashboard) to record it. Career Copilot only ever records what you tell it happened - nothing here
is inferred or automated.

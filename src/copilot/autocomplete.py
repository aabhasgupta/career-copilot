"""Suggestion data for the Preferences page's chip inputs (chiplist.js's
data-suggest mode). All three fields stay freeform - these are suggestions,
never a restriction - see docs/DECISIONS.md for the reasoning.

Titles and companies are seeded from what's actually in this user's jobs DB
(real, market-relevant, grows with use) plus a small static list so there's
something useful before discovery has run. Locations has no DB signal worth
using (a job's stored location is where it happened to post, not a place the
user would search), so it's a static curated list only: US states, the two
special values the search actually understands ("Remote", "United States"),
and major metros in "City, ST" form.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.db.models import Company, Job
from copilot.industry import Industry

SEED_TITLES = [
    "Software Engineer", "Senior Software Engineer", "Staff Software Engineer",
    "Principal Software Engineer", "Backend Engineer", "Frontend Engineer",
    "Full Stack Engineer", "Data Engineer", "Senior Data Engineer", "Data Scientist",
    "Senior Data Scientist", "Machine Learning Engineer", "ML Engineer", "AI Engineer",
    "Applied Scientist", "Research Scientist", "DevOps Engineer", "Site Reliability Engineer",
    "Cloud Engineer", "Platform Engineer", "Infrastructure Engineer", "Security Engineer",
    "QA Engineer", "Test Engineer", "Engineering Manager", "Director of Engineering",
    "VP of Engineering", "Chief Technology Officer", "Solutions Architect",
    "Technical Program Manager", "Program Manager", "Project Manager", "Scrum Master",
    "Product Manager", "Senior Product Manager", "Technical Product Manager",
    "Chief Product Officer", "VP of Product", "Product Designer", "UX Designer",
    "UI Designer", "UX Researcher", "Business Analyst", "Data Analyst", "Business Intelligence Analyst",
    "Business Development Manager", "Business Development Representative", "Sales Engineer",
    "Account Executive", "Customer Success Manager", "Marketing Manager",
    "Growth Marketing Manager", "Product Marketing Manager", "Operations Manager",
    "Financial Analyst", "Quantitative Analyst", "Risk Analyst", "Compliance Analyst",
    "Supply Chain Analyst", "HR Business Partner", "Recruiter", "Technical Recruiter",
    "IT Support Specialist", "Network Engineer", "Systems Administrator",
    "Database Administrator", "Mobile Engineer", "iOS Engineer", "Android Engineer",
    "Management Consultant", "Strategy Consultant", "Chief of Staff",
]

_US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

_US_METROS = [
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Phoenix, AZ",
    "Philadelphia, PA", "San Antonio, TX", "San Diego, CA", "Dallas, TX", "Austin, TX",
    "San Jose, CA", "San Francisco, CA", "Seattle, WA", "Denver, CO", "Boston, MA",
    "Atlanta, GA", "Miami, FL", "Minneapolis, MN", "Portland, OR", "Detroit, MI",
    "Charlotte, NC", "Raleigh, NC", "Nashville, TN", "Salt Lake City, UT", "Pittsburgh, PA",
    "Columbus, OH", "Indianapolis, IN", "Washington, DC", "Baltimore, MD", "Sacramento, CA",
    "Kansas City, MO", "Orlando, FL", "Tampa, FL", "St. Louis, MO", "Cincinnati, OH",
    "Cleveland, OH", "Las Vegas, NV", "San Francisco Bay Area, CA",
]

SEED_LOCATIONS = ["Remote", "United States", *_US_STATES, *_US_METROS]

# Well-known employers a company-preference search is likely to name, so they
# suggest even before discovery has happened to surface them in the jobs DB
# (a real gap: Meta/Microsoft-caliber companies don't post through every
# aggregator query, so "in the DB" undercounts "worth ranking"). Grouped by
# the Industry vocabulary (industry.py) for maintainability, not shown to the
# user - the merged, deduped, alphabetized list is what's actually served.
SEED_COMPANIES = [
    # Big tech / software
    "Google", "Alphabet", "Microsoft", "Meta", "Apple", "Amazon", "Netflix",
    "Nvidia", "Salesforce", "Adobe", "Oracle", "SAP", "IBM", "Intel", "AMD",
    "Cisco", "VMware", "ServiceNow", "Workday", "Atlassian", "Intuit",
    "Uber", "Lyft", "Airbnb", "DoorDash", "Instacart", "Pinterest", "Snap",
    "Spotify", "LinkedIn", "X", "TikTok", "ByteDance", "Reddit", "Palantir",
    "Snowflake", "Databricks", "MongoDB", "Datadog", "HashiCorp", "GitLab",
    "GitHub", "Dropbox", "Box", "Zoom", "Slack", "Asana", "Notion", "Figma",
    "Canva", "Shopify", "Square", "Block", "PayPal", "Stripe", "Plaid",
    "Robinhood", "Coinbase", "Chime", "Affirm", "SoFi", "Twilio", "Okta",
    "CrowdStrike", "Palo Alto Networks", "Fortinet", "Cloudflare", "Akamai",
    "Qualcomm", "Broadcom", "Texas Instruments", "Micron", "Western Digital",
    "Dell", "HP", "Hewlett Packard Enterprise", "Lenovo", "Sony", "Samsung",
    "SpaceX", "Tesla", "Rivian", "Waymo", "Anthropic", "OpenAI", "DeepMind",
    # Consulting / professional services
    "McKinsey & Company", "Boston Consulting Group", "Bain & Company",
    "Deloitte", "PwC", "EY", "KPMG", "Accenture", "Capgemini", "Booz Allen Hamilton",
    "Cognizant", "Infosys", "TCS", "Wipro", "Slalom", "West Monroe", "ZS Associates",
    # Banking / finance
    "JPMorgan Chase", "Goldman Sachs", "Morgan Stanley", "Bank of America",
    "Citigroup", "Wells Fargo", "Capital One", "American Express", "US Bank",
    "PNC Bank", "Truist", "Charles Schwab", "Fidelity Investments",
    "BlackRock", "Vanguard", "State Street", "Northern Trust", "Citadel",
    "Two Sigma", "Jane Street", "D.E. Shaw", "Visa", "Mastercard",
    # Insurance
    "UnitedHealth Group", "Anthem", "Cigna", "Aetna", "Humana", "MetLife",
    "Prudential Financial", "Allstate", "Progressive", "GEICO", "State Farm",
    "Liberty Mutual", "Travelers", "AIG", "Chubb",
    # Healthcare / pharma
    "Johnson & Johnson", "Pfizer", "Moderna", "Merck", "AbbVie", "Eli Lilly",
    "Bristol Myers Squibb", "Amgen", "Gilead Sciences", "CVS Health",
    "Walgreens", "Kaiser Permanente", "Mayo Clinic", "Cleveland Clinic",
    "HCA Healthcare",
    # Retail / e-commerce / consumer
    "Walmart", "Target", "Costco", "Home Depot", "Lowe's", "Best Buy",
    "Nike", "Starbucks", "McDonald's", "Procter & Gamble", "Unilever",
    "Coca-Cola", "PepsiCo", "Nestle", "Kraft Heinz",
    # Manufacturing / industrial / energy
    "General Electric", "General Motors", "Ford", "Boeing", "Lockheed Martin",
    "Raytheon", "Northrop Grumman", "Honeywell", "3M", "Caterpillar",
    "John Deere", "ExxonMobil", "Chevron", "Shell", "ConocoPhillips",
    "NextEra Energy", "Duke Energy",
    # Telecom / media
    "AT&T", "Verizon", "T-Mobile", "Comcast", "Disney", "Warner Bros Discovery",
    "Paramount", "NBCUniversal",
    # Staffing / recruiting (relevant to the deprioritize-staffing toggle)
    "Robert Half", "Randstad", "ManpowerGroup", "Insight Global",
    "TEKsystems", "Kforce", "Adecco",
    # Government / defense contractors
    "IRS", "Department of Defense", "NASA", "Leidos", "SAIC", "General Dynamics",
]


def get_suggestions(session: Session) -> dict[str, list[str]]:
    db_titles = session.scalars(select(Job.title).distinct()).all()
    db_companies = session.scalars(select(Company.name).distinct()).all()
    titles = sorted({*SEED_TITLES, *(t for t in db_titles if t)}, key=str.lower)
    companies = sorted({*SEED_COMPANIES, *(c for c in db_companies if c)}, key=str.lower)
    return {
        "titles": titles,
        "locations": list(SEED_LOCATIONS),
        "companies": companies,
        "industries": [i.value for i in Industry],
    }

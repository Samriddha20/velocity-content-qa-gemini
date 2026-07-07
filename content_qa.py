"""
content_qa.py  —  Velocity Media Lab · Content Quality Review (single file)
===========================================================================
*** TESTING PROTOTYPE — uses Google GEMINI FLASH (free tier) instead of Claude ***
Everything (features, prompts, scoring, architecture) is identical to the Claude
version. The ONLY difference is the model layer (the _call_model function + model
name). To move to production later, swap that one function back to the Anthropic SDK.

Workflow this tool assumes:
  1. Writers run GRAMMARLY themselves before submitting  → grammar/spelling
     is already mostly clean. So the model does NOT deep-check grammar (saves tokens).
  2. The model does only the smart work: American English, readability, structure,
     SEO, AI-search optimization, formatting, competitor gaps, and personalized coaching.

Two ways to run it (same core logic):
    streamlit run content_qa.py                         # UI (use now)
    uvicorn content_qa:app --host 0.0.0.0 --port 8000   # API for n8n (later)

Install:
    pip install google-genai streamlit textstat gspread google-auth fastapi uvicorn
    export GEMINI_API_KEY="AIza..."   # free key from aistudio.google.com

Put company rules as .txt/.md in ./guidelines (a starter file is provided).
Put a Google service_account.json next to this file to enable the Sheets tab.

NOTE: the free Gemini tier may use your inputs to improve Google's models — fine for
testing with sample text, but review Google's terms before sending real client content.
"""

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import textstat

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "content_qa.db"
GUIDELINES_DIR = BASE_DIR / "guidelines"

# ===========================================================================
# CONFIG
# ===========================================================================
# Verify the model name against current Google docs (ai.google.dev/gemini-api/docs/models);
# names change. Free-tier Flash options include: "gemini-2.5-flash", "gemini-2.0-flash",
# "gemini-1.5-flash". If one errors as "not found", try another (Claude Code can fix it).
MODEL = "gemini-2.5-flash"
MAX_TOKENS = 8192
TEMPERATURE = 0.2

# Content types Velocity Media Lab produces (from the spec).
CONTENT_TYPES = ["Blog", "Landing Page", "Service Page", "City Page", "FAQ Page"]

# The 7 sub-scores from the spec's Final Evaluation (grammar kept light).
# Weights sum to 1.0 and produce the Overall score (computed in code, not by the LLM).
DIMENSION_WEIGHTS = {
    "grammar": 0.10,               # light — Grammarly already handled most of this
    "readability": 0.16,
    "american_english": 0.16,
    "structure": 0.18,
    "seo": 0.16,
    "ai_search_optimization": 0.12,
    "formatting": 0.12,
}

# Writer severity → how strict the review is.
#   max_feedback   : how many coaching points to surface (tougher writers get more)
#   ready / minor / moderate : Overall-score thresholds for the recommendation
SEVERITY_POLICY = {
    "Medium":   {"max_feedback": 5, "ready": 80, "minor": 68, "moderate": 55},
    "High":     {"max_feedback": 6, "ready": 85, "minor": 72, "moderate": 58},
    "Critical": {"max_feedback": 8, "ready": 90, "minor": 78, "moderate": 62},
}

# Seed data from the Team Lead's spec (only used on first run / empty DB).
SEED_WRITERS = {
    "Ankita": {"severity": "Medium", "weaknesses": [
        "Paragraphs becoming too long", "Unnecessarily lengthy explanations",
        "Readability", "Occasional grammar slips", "Occasional spelling slips",
        "Maintaining concise writing"]},
    "Pragya": {"severity": "High", "weaknesses": [
        "Frequent spelling mistakes", "Incorrect word choice", "Meaningless sentences",
        "Abrupt sentence endings", "Repeated ideas", "Weak proofreading",
        "Readability", "Indian English instead of natural American English"]},
    "Arshi": {"severity": "Critical", "weaknesses": [
        "Awkward sentence construction", "Poor readability", "Missing transition words",
        "Indian English instead of American English", "Very large paragraphs",
        "Missing paragraph spacing", "Missing content sections", "Random section order",
        "Duplicate or repeated sections", "Weak competitor research", "Grammar mistakes",
        "Overly long introductions", "Weak or unengaging introductions"]},
}


# ===========================================================================
# CHECKS — free, instant, deterministic measurements (no LLM, no cost)
# ===========================================================================
def _sentences(text): return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
def _words(text): return re.findall(r"\b[\w']+\b", text.lower())

_PASSIVE_RE = re.compile(
    r"\b(am|is|are|was|were|be|been|being)\b\s+(\w+ed|written|done|made|given|taken|"
    r"seen|known|shown|held|kept|built|sent|left|found|told)\b", re.IGNORECASE)


def passive_voice_pct(text):
    sents = _sentences(text)
    return round(100 * sum(1 for s in sents if _PASSIVE_RE.search(s)) / len(sents), 1) if sents else 0.0


def keyword_density_pct(text, keyword):
    if not keyword:
        return None
    words = _words(text)
    if not words:
        return 0.0
    kw = keyword.lower().split()
    count = sum(1 for w in words if w == kw[0]) if len(kw) == 1 else " ".join(words).count(" ".join(kw))
    return round(100 * count / len(words), 2)


def repeated_phrases(text, n=3, min_repeat=3, top=5):
    words = _words(text)
    grams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
    return [g for g, c in Counter(grams).most_common() if c >= min_repeat][:top]


def longest_paragraph_words(text):
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return max((len(_words(p)) for p in paras), default=0)


def run_checks(content, keyword=None):
    return {
        "word_count": len(_words(content)),
        "sentence_count": len(_sentences(content)),
        "flesch_reading_ease": round(textstat.flesch_reading_ease(content), 1) if content else 0,
        "flesch_kincaid_grade": round(textstat.flesch_kincaid_grade(content), 1) if content else 0,
        "passive_voice_pct": passive_voice_pct(content),
        "keyword_density_pct": keyword_density_pct(content, keyword),
        "repeated_phrases": repeated_phrases(content),
        "longest_paragraph_words": longest_paragraph_words(content),
    }


# ===========================================================================
# DB — SQLite: writers (+ severity), weakness memory, submissions
# ===========================================================================
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS writers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                severity TEXT NOT NULL DEFAULT 'Medium'
            );
            CREATE TABLE IF NOT EXISTS weaknesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                writer_id INTEGER NOT NULL REFERENCES writers(id) ON DELETE CASCADE,
                text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                writer_id INTEGER REFERENCES writers(id) ON DELETE SET NULL,
                writer_name TEXT, content_type TEXT, content_hash TEXT,
                overall INTEGER, recommendation TEXT, result_json TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sub_hash ON submissions(content_hash);
            """
        )


def seed_writers_if_empty():
    with _conn() as c:
        if c.execute("SELECT 1 FROM writers LIMIT 1").fetchone():
            return  # already has data — don't clobber edits
        for name, info in SEED_WRITERS.items():
            wid = c.execute("INSERT INTO writers(name, severity) VALUES (?, ?)",
                            (name, info["severity"])).lastrowid
            for w in info["weaknesses"]:
                c.execute("INSERT INTO weaknesses(writer_id, text) VALUES (?, ?)", (wid, w))


def get_or_create_writer(name, severity="Medium"):
    with _conn() as c:
        row = c.execute("SELECT id FROM writers WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        return c.execute("INSERT INTO writers(name, severity) VALUES (?, ?)", (name, severity)).lastrowid


def list_writers():
    with _conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM writers ORDER BY name")]


def get_severity(name):
    with _conn() as c:
        row = c.execute("SELECT severity FROM writers WHERE name = ?", (name,)).fetchone()
        return row["severity"] if row else "Medium"


def set_severity(name, severity):
    get_or_create_writer(name)
    with _conn() as c:
        c.execute("UPDATE writers SET severity = ? WHERE name = ?", (severity, name))


def get_weaknesses(name):
    with _conn() as c:
        return [r["text"] for r in c.execute(
            """SELECT w.text FROM weaknesses w JOIN writers wr ON wr.id = w.writer_id
               WHERE wr.name = ? ORDER BY w.id""", (name,))]


def add_weakness(name, text):
    wid = get_or_create_writer(name)
    with _conn() as c:
        c.execute("INSERT INTO weaknesses(writer_id, text) VALUES (?, ?)", (wid, text))


def remove_weakness(name, text):
    with _conn() as c:
        c.execute("""DELETE FROM weaknesses WHERE text = ?
                     AND writer_id = (SELECT id FROM writers WHERE name = ?)""", (text, name))


def save_submission(name, ctype, chash, overall, recommendation, result):
    wid = get_or_create_writer(name)
    with _conn() as c:
        return c.execute(
            """INSERT INTO submissions (writer_id, writer_name, content_type, content_hash,
               overall, recommendation, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wid, name, ctype, chash, overall, recommendation, json.dumps(result),
             datetime.now(timezone.utc).isoformat())).lastrowid


def already_reviewed(chash):
    with _conn() as c:
        return c.execute("SELECT 1 FROM submissions WHERE content_hash = ? LIMIT 1",
                         (chash,)).fetchone() is not None


def recent_submissions(limit=50):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM submissions ORDER BY id DESC LIMIT ?", (limit,))]


init_db()
seed_writers_if_empty()


# ===========================================================================
# COMPETITOR HOOK (Option 3) — reason now, wire real SERP data later
# ===========================================================================
def fetch_competitor_data(keyword, content_type):
    """
    Placeholder for a future SERP/SEO API (DataForSEO, Semrush, SerpAPI).

    RIGHT NOW: returns None → Claude reasons about likely Page-1 gaps from its own
    knowledge and clearly labels them ESTIMATED.

    LATER: fetch real top-ranking pages here and return e.g.
        {"top_pages": [{"title": ..., "sections": [...], "word_count": ...}, ...]}
    The prompt automatically switches to using REAL data and labels it LIVE.
    Nothing else in the code needs to change.
    """
    return None  # <- the single line you replace when you add a SERP API


# ===========================================================================
# LLM — production system prompt + single Claude call + scoring (swap provider here)
# ===========================================================================
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402


def _get_gemini_api_key():
    """GEMINI_API_KEY env var first (local dev), falling back to Streamlit Cloud
    secrets. Wrapped in try/except because this module is also imported by uvicorn
    (the FastAPI trigger), where st.secrets has no secrets.toml to read and would
    raise on access."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return None


_client = genai.Client(api_key=_get_gemini_api_key())

SYSTEM_PROMPT = """You are a Senior Content Quality Analyst and SEO Strategist at \
Velocity Media Lab. You review content the way a senior human reviewer would before it \
is delivered to a client — NOT as a grammar checker, but as a strategist deciding whether \
the piece is genuinely good enough to rank on Page 1 and satisfy both readers and \
AI-powered search engines.

IMPORTANT CONTEXT: writers run Grammarly before submitting, so basic grammar and spelling \
are already mostly clean. Do NOT exhaustively hunt for minor grammar/spelling issues. Give \
grammar a LIGHT pass and only flag clearly remaining errors. Spend your effort on the \
higher-value judgement work below.

For every submission, judge:
- AMERICAN ENGLISH: flag Indian/UK English, literal translations, awkward or non-native \
phrasing, unnatural expressions. It must read like a native U.S. copywriter wrote it.
- READABILITY: ~8th-grade level, conversational, varied sentence length, smooth flow, \
natural transitions, no giant walls of text, one clear purpose per paragraph.
- STRUCTURE: exactly one H1; strong hooking introduction (not generic, not a definition, \
not a repeat of the title); logical H2/H3 hierarchy; no missing, duplicate, or randomly \
ordered sections; a helpful conclusion; a natural CTA.
- SEO: search-intent coverage, natural keyword use and variation, semantic/topical \
completeness, heading optimization. Flag obvious keyword stuffing.
- AI SEARCH OPTIMIZATION (AEO/GEO/AI Overviews): direct answers, question-style headings \
where useful, concise quotable passages, clear definitions, entity-rich content, and \
bullets/tables where they improve clarity.
- FORMATTING: short paragraphs, good spacing, useful bullets/tables, consistent formatting.
- COMPETITOR / COMPLETENESS: would this deserve to rank alongside or above the current \
Page 1 results for this intent? What important sections or common user questions are \
missing versus what strong pages cover? Reward content that is BETTER, not merely correct. \
Consider E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness).

HARD RULES:
- Every finding MUST include a short exact quote from the content as "evidence". If you \
cannot quote it, do not report it.
- Classify each finding's severity as exactly "Critical", "Major", or "Minor":
    Critical = cannot be delivered (missing sections, broken structure, major readability \
or English problems, repeated content, missing conclusion or CTA).
    Major = needs revision before approval (long paragraphs, weak SEO, weak intro, awkward \
transitions, excessive fluff).
    Minor = quick edits (small wording, punctuation, capitalization).
- PERSONALIZE: the writer's known recurring weaknesses are provided. Put feedback about \
those FIRST in "prioritized_feedback", then the most severe remaining issues. Write each \
item as short, supportive coaching addressed to the writer by name.
- For the competitor check: if LIVE competitor data is provided, use it and set \
data_source to "live". Otherwise reason from your own knowledge and set data_source to \
"estimated".
- Never ask "is this content acceptable?" — always ask "is this the best version of this \
content compared to what is currently ranking?"
- Do NOT compute an overall score or the final recommendation; those are calculated \
separately. Only fill the fields in the schema.
- Return ONLY valid JSON in the exact schema. No markdown, no commentary outside the JSON."""

OUTPUT_SCHEMA = """{
  "subscores": {
    "grammar": <0-100>, "readability": <0-100>, "american_english": <0-100>,
    "structure": <0-100>, "seo": <0-100>, "ai_search_optimization": <0-100>,
    "formatting": <0-100>
  },
  "findings": [
    {"dimension": "<subscore key>", "severity": "Critical|Major|Minor",
     "evidence": "<short exact quote>", "issue": "<one sentence>",
     "fix": "<concrete fix>", "matches_known_weakness": true|false}
  ],
  "competitor_analysis": {
    "data_source": "estimated|live",
    "missing_sections": ["<sections strong pages include that this lacks>"],
    "unanswered_questions": ["<common user questions not answered>"],
    "value_gaps": ["<ways competitors offer more value>"],
    "verdict": "<would this rank alongside/above Page 1 today? why>"
  },
  "prioritized_feedback": [
    {"priority": <1=most important>, "text": "<coaching to the writer>"}
  ],
  "summary": "<Team Lead note: would you confidently deliver this to the client? why/why not>"
}"""


def build_review_prompt(*, content, content_type, writer_name, writer_severity,
                        writer_weaknesses, guidelines, targets, checks,
                        competitor_data, max_feedback):
    weaknesses_block = ("\n".join(f"- {w}" for w in writer_weaknesses)
                        if writer_weaknesses else "- (New writer, no history — use general best practices.)")
    targets_block = "\n".join(f"- {k}: {v}" for k, v in targets.items()) or "- (none)"
    checks_block = "\n".join(f"- {k}: {v}" for k, v in checks.items()) or "- (none)"

    if competitor_data:
        comp_block = ("LIVE competitor data (use this; set data_source='live'):\n"
                      + json.dumps(competitor_data, indent=2))
    else:
        comp_block = ("No live competitor data available. Reason from your own knowledge "
                      "about what strong Page-1 pages for this topic usually cover, and set "
                      "data_source='estimated'.")

    return f"""CONTENT TYPE: {content_type}
WRITER: {writer_name}  (severity level: {writer_severity})

THIS WRITER'S KNOWN RECURRING WEAKNESSES (prioritize these in your feedback):
{weaknesses_block}

TARGETS FOR THIS PIECE:
{targets_block}

OBJECTIVE MEASUREMENTS (already computed — trust these, do not recount):
{checks_block}

COMPETITOR CONTEXT:
{comp_block}

COMPANY GUIDELINES (base all judgements on these):
\"\"\"
{guidelines}
\"\"\"

CONTENT TO REVIEW:
\"\"\"
{content}
\"\"\"

TASK:
1. Score each of the 7 dimensions 0-100 in "subscores" (grammar = light pass only).
2. List concrete "findings", each with an exact evidence quote and a Critical/Major/Minor
   severity. Set "matches_known_weakness": true when it matches a weakness above.
3. Fill "competitor_analysis" (missing sections, unanswered questions, value gaps, verdict).
4. "prioritized_feedback": weaknesses of this writer FIRST, then most severe issues.
   Supportive coaching to {writer_name}. Provide UP TO {max_feedback} items.
5. Write a "summary" for the Team Lead answering: would you confidently deliver this?

Return ONLY the JSON object in exactly this shape:
{OUTPUT_SCHEMA}"""


def _call_model(system_prompt, user_message):
    """The ONLY provider-specific code. To move to Claude later, this is all you change."""
    resp = _client.models.generate_content(
        model=MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_TOKENS,
            response_mime_type="application/json",  # ask Gemini for clean JSON
            # gemini-2.5-flash spends max_output_tokens on internal "thinking" before
            # writing output, which was truncating the JSON before it printed. Not
            # needed for a structured-extraction task like this.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return resp.text


def _parse_json(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        text = text.replace("json", "", 1).strip("` \n")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            return json.loads(text[s:e + 1])
        raise


def compute_overall(subscores):
    total = sum(DIMENSION_WEIGHTS.get(k, 0) for k in subscores)
    if total == 0:
        return 0
    return round(sum(subscores.get(k, 0) * DIMENSION_WEIGHTS.get(k, 0) for k in subscores) / total)


def decide_recommendation(overall, findings, severity):
    """Computed in code (consistent), driven by writer severity + finding severities."""
    policy = SEVERITY_POLICY.get(severity, SEVERITY_POLICY["Medium"])
    sev_counts = Counter(f.get("severity") for f in findings)
    if sev_counts.get("Critical", 0) > 0:
        return "🔴 Major Rewrite Required"          # Critical finding = cannot deliver
    if overall >= policy["ready"] and sev_counts.get("Major", 0) == 0:
        return "✅ Ready to Deliver"
    if overall >= policy["minor"]:
        return "🟡 Minor Revisions Required"
    if overall >= policy["moderate"]:
        return "🟠 Moderate Revisions Required"
    return "🔴 Major Rewrite Required"


# ===========================================================================
# SHEETS — Google Sheets read/write (gspread)
# ---------------------------------------------------------------------------
# SETUP: Google Cloud -> enable Sheets API -> Service Account -> JSON key saved as
# service_account.json here -> Share the spreadsheet with the service account email.
# Headers: writer | content_type | keyword | content | status | score | recommendation | feedback
# ===========================================================================
SERVICE_ACCOUNT_FILE = str(BASE_DIR / "service_account.json")
STATUS_COL, SCORE_COL, REC_COL, FEEDBACK_COL = "status", "score", "recommendation", "feedback"


def _open_worksheet(spreadsheet_id, worksheet=0):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(spreadsheet_id)
    return sh.get_worksheet(worksheet) if isinstance(worksheet, int) else sh.worksheet(worksheet)


def read_unreviewed_rows(spreadsheet_id, worksheet=0):
    ws = _open_worksheet(spreadsheet_id, worksheet)
    pending = []
    for i, rec in enumerate(ws.get_all_records()):
        status = str(rec.get(STATUS_COL, "")).strip().lower()
        if status in ("", "pending") and str(rec.get("content", "")).strip():
            pending.append({
                "row_number": i + 2,
                "writer": str(rec.get("writer", "")).strip(),
                "content_type": str(rec.get("content_type", "Blog")).strip() or "Blog",
                "keyword": str(rec.get("keyword", "")).strip() or None,
                "content": str(rec.get("content", "")),
            })
    return pending


def write_result(spreadsheet_id, row_number, score, recommendation, feedback, worksheet=0):
    ws = _open_worksheet(spreadsheet_id, worksheet)
    headers = ws.row_values(1)

    def setcell(col, val):
        if col in headers:
            ws.update_cell(row_number, headers.index(col) + 1, val)

    setcell(STATUS_COL, "done")
    setcell(SCORE_COL, score)
    setcell(REC_COL, recommendation)
    setcell(FEEDBACK_COL, feedback)


# ===========================================================================
# CORE — the shared review orchestration (both triggers call this)
# ===========================================================================
def _content_hash(content):
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def load_guidelines(content_type=None):
    if not GUIDELINES_DIR.exists():
        return "(No guidelines provided yet.)"
    parts = [f"### {f.stem}\n{f.read_text(encoding='utf-8')}"
             for f in sorted(GUIDELINES_DIR.glob("*")) if f.suffix.lower() in (".txt", ".md")]
    return "\n\n".join(parts) or "(No guidelines provided yet.)"


def build_targets(content_type, keyword):
    # Per the spec there is NO fixed word count — targets are intent/tone notes.
    notes = {
        "Blog": "Informative & conversational. Thorough topic coverage over word count.",
        "Landing Page": "Persuasive, benefit-led, strong CTA, trust-building.",
        "Service Page": "Clear service explanation, benefits, trust signals, CTA.",
        "City Page": "Locally relevant, specific to the city, avoids generic filler.",
        "FAQ Page": "Direct question-and-answer, quotable, AI-search friendly.",
    }
    targets = {"intent": notes.get(content_type, "Clear, useful, well-structured."),
               "reading_level": "~8th grade", "english": "Natural American English"}
    if keyword:
        targets["seo_keyword"] = keyword
    return targets


def format_feedback_for_sheet(result):
    lines = [f"SCORE: {result.get('overall', '?')}/100   {result.get('recommendation', '')}"]
    for item in result.get("prioritized_feedback", []):
        lines.append(f"{item.get('priority', '?')}. {item.get('text', '')}")
    comp = result.get("competitor_analysis", {})
    if comp.get("verdict"):
        tag = comp.get("data_source", "estimated").upper()
        lines.append(f"\nCompetitor check ({tag}): {comp['verdict']}")
    if result.get("summary"):
        lines.append(f"\nSummary: {result['summary']}")
    return "\n".join(lines)


def review_one(*, writer, content, content_type="Blog", keyword=None, skip_if_reviewed=True):
    """End-to-end review of ONE submission. Called by Streamlit AND the n8n API."""
    content = content or ""
    chash = _content_hash(content)
    if skip_if_reviewed and already_reviewed(chash):
        prev = next((s for s in recent_submissions(200) if s["content_hash"] == chash), None)
        return {"skipped": True, "reason": "already reviewed",
                "overall": prev["overall"] if prev else None,
                "recommendation": prev["recommendation"] if prev else None}

    severity = get_severity(writer)
    policy = SEVERITY_POLICY.get(severity, SEVERITY_POLICY["Medium"])
    checks = run_checks(content, keyword=keyword)

    result = _parse_json(_call_model(SYSTEM_PROMPT, build_review_prompt(
        content=content, content_type=content_type, writer_name=writer,
        writer_severity=severity, writer_weaknesses=get_weaknesses(writer),
        guidelines=load_guidelines(content_type), targets=build_targets(content_type, keyword),
        checks=checks, competitor_data=fetch_competitor_data(keyword, content_type),
        max_feedback=policy["max_feedback"])))

    # Trim feedback to the writer's allowance, then score + recommend IN CODE.
    result["prioritized_feedback"] = sorted(
        result.get("prioritized_feedback", []), key=lambda x: x.get("priority", 99)
    )[:policy["max_feedback"]]
    result["overall"] = compute_overall(result.get("subscores", {}))
    result["recommendation"] = decide_recommendation(
        result["overall"], result.get("findings", []), severity)
    result["writer_severity"] = severity
    result["checks"] = checks
    result["skipped"] = False

    save_submission(writer, content_type, chash, result["overall"],
                    result["recommendation"], result)
    return result


def review_sheet(spreadsheet_id, worksheet=0):
    outcomes = []
    for row in read_unreviewed_rows(spreadsheet_id, worksheet):
        r = review_one(writer=row["writer"], content=row["content"],
                       content_type=row["content_type"], keyword=row["keyword"])
        if not r.get("skipped"):
            write_result(spreadsheet_id, row["row_number"], r.get("overall", 0),
                         r.get("recommendation", ""), format_feedback_for_sheet(r), worksheet)
        outcomes.append({"row": row["row_number"], "writer": row["writer"],
                         "score": r.get("overall"), "recommendation": r.get("recommendation"),
                         "skipped": r.get("skipped")})
    return outcomes


# ===========================================================================
# FASTAPI — Trigger #2 (n8n).  Run:  uvicorn content_qa:app --port 8000
# ===========================================================================
from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402


def _build_api_app():
    # Built inside a function, rather than a top-level `app = FastAPI(...)`
    # literal, because `streamlit run` statically scans the script for that
    # exact pattern and — if found — hijacks the run to serve this ASGI app
    # directly instead of the Streamlit UI. That broke health checks and the
    # UI on Streamlit Cloud. The indirection here is invisible to that scan.
    return FastAPI(title="Velocity Content QA")


app = _build_api_app()


class ReviewRequest(BaseModel):
    writer: str
    content: str
    content_type: str = "Blog"
    keyword: str | None = None


class SheetRequest(BaseModel):
    spreadsheet_id: str
    worksheet: str | int = 0


@app.post("/review")
def api_review(req: ReviewRequest):
    r = review_one(writer=req.writer, content=req.content,
                   content_type=req.content_type, keyword=req.keyword)
    return {"overall": r.get("overall"), "recommendation": r.get("recommendation"),
            "skipped": r.get("skipped", False), "subscores": r.get("subscores", {}),
            "competitor_analysis": r.get("competitor_analysis", {}),
            "feedback": format_feedback_for_sheet(r)}


@app.post("/review-sheet")
def api_review_sheet(req: SheetRequest):
    return {"reviewed": review_sheet(req.spreadsheet_id, req.worksheet)}


@app.get("/health")
def api_health():
    return {"ok": True}


# ===========================================================================
# STREAMLIT — Trigger #1 (UI).  Run:  streamlit run content_qa.py
# ===========================================================================
def _run_streamlit_ui():
    import streamlit as st

    st.set_page_config(page_title="Velocity Content QA", layout="wide")
    st.title("Velocity Media Lab — Content Quality Review")
    st.caption("Writers run Grammarly first · Claude handles the smart review")

    with st.sidebar:
        st.header("Writer memory")
        writers = list_writers()
        selected = st.selectbox("Writer", options=(writers or []) + ["+ new writer"])
        if selected == "+ new writer":
            new_name = st.text_input("New writer name")
            sev = st.selectbox("Severity", ["Medium", "High", "Critical"])
            if st.button("Add writer") and new_name.strip():
                get_or_create_writer(new_name.strip(), sev)
                st.rerun()
        else:
            cur = get_severity(selected)
            new_sev = st.selectbox("Severity", ["Medium", "High", "Critical"],
                                   index=["Medium", "High", "Critical"].index(cur))
            if new_sev != cur:
                set_severity(selected, new_sev)
                st.rerun()
            st.caption("Recurring weaknesses (prioritized in feedback):")
            for w in get_weaknesses(selected):
                c1, c2 = st.columns([5, 1])
                c1.write(f"• {w}")
                if c2.button("✕", key=f"del_{w}"):
                    remove_weakness(selected, w)
                    st.rerun()
            nw = st.text_input("Add a weakness", key="new_weakness")
            if st.button("Add weakness") and nw.strip():
                add_weakness(selected, nw.strip())
                st.rerun()

    tab_sheet, tab_paste = st.tabs(["📄 Review from Google Sheet", "✍️ Paste content"])

    with tab_sheet:
        st.write("Reviews every row where **status** is empty; writes score, recommendation & feedback back.")
        sid = st.text_input("Spreadsheet ID")
        if st.button("Review new submissions", type="primary", disabled=not sid):
            with st.spinner("Reviewing..."):
                try:
                    for o in review_sheet(sid) or []:
                        tag = "skipped" if o["skipped"] else f"{o['score']}/100 · {o['recommendation']}"
                        st.write(f"Row {o['row']} — {o['writer']}: {tag}")
                except Exception as e:
                    st.error(f"Sheet error: {e}")

    with tab_paste:
        a, b = st.columns(2)
        writer = a.selectbox("Writer", options=list_writers() or ["(add a writer first)"])
        ctype = b.selectbox("Content type", CONTENT_TYPES)
        keyword = st.text_input("Target SEO keyword (optional)")
        content = st.text_area("Content", height=280)
        if st.button("Review", type="primary", disabled=not content.strip()):
            with st.spinner("Reviewing..."):
                r = review_one(writer=writer, content=content, content_type=ctype,
                               keyword=keyword or None, skip_if_reviewed=False)
            top = st.columns([1, 2])
            top[0].metric("Overall", f"{r.get('overall', '?')}/100")
            top[1].subheader(r.get("recommendation", ""))
            st.caption(f"Writer severity: {r.get('writer_severity')}")

            st.subheader("Personalized feedback")
            for item in r.get("prioritized_feedback", []):
                st.markdown(f"**{item.get('priority')}.** {item.get('text')}")

            with st.expander("Sub-scores"):
                st.json(r.get("subscores", {}))
            with st.expander("Findings (by severity)"):
                for f in sorted(r.get("findings", []),
                                key=lambda x: {"Critical": 0, "Major": 1, "Minor": 2}.get(x.get("severity"), 3)):
                    flag = " ⭐ recurring" if f.get("matches_known_weakness") else ""
                    st.markdown(f"- **[{f.get('severity')}] {f.get('dimension')}**{flag}: "
                                f"{f.get('issue')} → *{f.get('fix')}*  \n  > {f.get('evidence')}")
            comp = r.get("competitor_analysis", {})
            if comp:
                with st.expander(f"Competitor check ({comp.get('data_source', 'estimated').upper()})"):
                    st.write(comp.get("verdict", ""))
                    if comp.get("missing_sections"):
                        st.markdown("**Missing sections:** " + ", ".join(comp["missing_sections"]))
                    if comp.get("unanswered_questions"):
                        st.markdown("**Unanswered questions:** " + ", ".join(comp["unanswered_questions"]))
            if r.get("summary"):
                st.info(r["summary"])


if __name__ == "__main__":
    _run_streamlit_ui()

# Velocity Content QA — TESTING PROTOTYPE (Gemini Flash, free tier)

Identical to the main Claude project — same features, prompts, scoring, and architecture.
**The only difference is the model:** this version uses **Google Gemini Flash (free tier)**
so you can test without paying. To move to production later, swap the `_call_model`
function (and model name) back to the Anthropic/Claude SDK — nothing else changes.

## Files

| File | What it is |
|---|---|
| `content_qa.py` | The whole app in one file (UI + API + logic). Uses Gemini. |
| `guidelines/velocity_standards.md` | Review rules the model reads on every review. |
| `requirements.txt` | Python packages (uses `google-genai`). |
| `.gitignore` | Keeps secrets/database out of git. |
| `content_qa.db` | Created automatically on first run. |

## 1. Get a FREE Gemini API key

- Go to **aistudio.google.com** and sign in with a Google account.
- Click **Get API key** → **Create API key** → copy it (starts with `AIza...`).
- No billing or credit card needed for the free tier.

## 2. Install & set the key

```bash
python -m venv venv
# Windows:  venv\Scripts\activate
# Mac:      source venv/bin/activate

pip install -r requirements.txt

# Windows (PowerShell):  setx GEMINI_API_KEY "AIza..."   (then reopen terminal)
# Mac / Linux:           export GEMINI_API_KEY="AIza..."
```

Check the `MODEL` name near the top of `content_qa.py` against
`ai.google.dev/gemini-api/docs/models` — if it errors as "not found", try another
free Flash model (e.g. `gemini-2.0-flash`).

## 3. Run

```bash
streamlit run content_qa.py
```
Opens in your browser with Ankita, Pragya, Arshi pre-loaded. Try the **"Paste content"** tab.

## Free-tier notes / honesty

- **Rate limits:** the free tier caps requests per minute/day — fine for testing, not high volume.
- **Privacy:** Google's free tier may use your inputs to improve its models. Fine for testing
  with sample text; review Google's terms before sending real client content.
- **Quality:** Gemini Flash is good, but slightly behind Claude at subtle American-English /
  tone judgment (the core of this tool). Expect the production Claude version to be sharper.
- Everything else — writer memory, severity, competitor estimate, scoring — behaves the same.

## Moving to the paid Claude version later

Only two things change in `content_qa.py`: the import/client (`google-genai` → `anthropic`)
and the `_call_model` function. Ask Claude Code to do it, or reuse the main Claude project files.

# Claude Code — first message for this prototype

This is the **Gemini Flash (free tier)** testing version of the Velocity content QA tool.
The app already exists in `content_qa.py`. You do NOT need to rebuild it.

## To get it running, paste this to Claude Code (with Claude Code started in this folder):

> This is a Streamlit + Gemini content-review app in `content_qa.py`. Read the README.md,
> then set up a Python virtual environment and install the packages from requirements.txt.
> Help me set my GEMINI_API_KEY (free key from aistudio.google.com). Ask before any global
> installs. Then run it with `streamlit run content_qa.py` and tell me the web address.

## If Gemini reports the model name is wrong

> The Gemini model name in content_qa.py errored. Look up a current free Flash model at
> ai.google.dev/gemini-api/docs/models and update the MODEL value, then run it again.

## When you're ready to switch this to the paid Claude API

> Switch this app from Google Gemini to the Anthropic Claude API. Change only the import,
> the client, the `_call_model` function, and the MODEL name to a Claude model; read the key
> from ANTHROPIC_API_KEY; keep everything else exactly the same. Update requirements.txt
> (swap `google-genai` for `anthropic`).

# AI Debugger

A small hackathon MVP Django app for turning pasted Python/Django tracebacks into a focused debugging brief.

The app gives you:

- Issue summary
- Likely root cause
- Suspected file/function/class
- Suggested minimal fix
- Optional patch diff
- Confidence score
- One regression test suggestion

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set an OpenAI API key for live analysis:

```bash
export OPENAI_API_KEY="your-api-key"
```

Optional model override:

```bash
export AI_DEBUGGER_MODEL="gpt-5.4-mini"
```

## Run

```bash
python manage.py runserver
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Demo

Click **Load Demo Example**, then **Analyze Bug**. If `OPENAI_API_KEY` is not set, the built-in demo still returns a deterministic structured analysis so the UI can be shown reliably.

## Notes

- No auth, no background jobs, and no database-backed features.
- The LLM call lives in `debugger/services/debugger.py`.
- Bad model output is handled with a friendly fallback and the raw response is shown instead of crashing the page.
- API keys are read from environment variables only.

## CI/CD

This repo includes a GitHub Actions workflow at `.github/workflows/ci.yml`.

On every push or pull request to `main`, it runs:

```bash
python manage.py check
python manage.py test
python manage.py collectstatic --noinput
```

## Deploy On Render

The simplest deployment path is Render:

1. Push this repository to GitHub.
2. In Render, create a new Blueprint from the repository.
3. Render will read `render.yaml`, run `./build.sh`, and start Gunicorn.
4. Add `OPENAI_API_KEY` in Render's environment variables.

The app does not rely on the database for product features, so the included Render config keeps deployment lightweight.

Useful production env vars:

```bash
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<generated secret>
DJANGO_ALLOWED_HOSTS=.onrender.com,your-domain.com
OPENAI_API_KEY=<your key>
AI_DEBUGGER_MODEL=gpt-5.4-mini
```

For a manual host, the important commands are:

```bash
pip install -r requirements.txt
python manage.py collectstatic --noinput
python -m gunicorn ai_debugger.wsgi:application --bind 0.0.0.0:8000
```

# JobPilot AI

AI-assisted job discovery, resume matching, application preparation, and application tracking.

## MVP
- Upload PDF/DOCX resume and extract profile text.
- Configure target roles, locations, minimum match score, and application mode.
- Import normalized job JSON.
- Score jobs against the resume/profile using a transparent weighted matcher.
- Generate tailored application answers from stored profile facts.
- Track applications and statuses.
- Playwright runner scaffold for permitted browser workflows; CAPTCHA, MFA, assessments, and legal declarations remain human checkpoints.

## Run
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000

## Environment
Copy `.env.example` to `.env` and optionally set `OPENAI_API_KEY`.

## Production
Use PostgreSQL instead of SQLite, object storage for resumes, HTTPS, encrypted secrets, authentication, and provider-specific application adapters. Do not automate around CAPTCHA/MFA or submit false information.

# NutritionPlan: Project Notes

> Living document. Read this first at the start of every new chat to get context.
> Update it whenever something meaningful changes (decisions, features, setup, open issues).

**Last updated:** 2026-09-15

---

## 1. Project Overview
- **What:** A personal, single-user Streamlit app for a doctor. The doctor photographs handwritten patient notes, and the app stores them per patient in their own Google Drive. An AI transcribes the notes into `Info.md` and generates a versioned nutrition plan (`Plan1.md`, `Plan2.md`, …) from the notes, the doctor's typed remarks and the previous plan.
- **User:** only the doctor. No multi-user features, sharing or deployment; it runs locally.
- **Location:** `/home/yuvraj/projects/NutritionPlan` (WSL2 / Linux)
- **Original spec:** the "Build Prompt: Personal Patient Case-Study & Nutrition Plan App" given in the first build chat (2026-09-15). The wireframe `NutritionPlanInterface.png` it mentions has **not** been added to the project.

## 2. Tech Stack
| Layer    | Choice | Notes |
|----------|--------|-------|
| UI       | Streamlit (Python 3.12), 1.63 installed | Screens controlled with `st.session_state.screen` |
| Storage  | Google Drive API v3 | Only storage. Scope: full `drive` |
| Auth     | Google OAuth 2.0, Desktop-app client | `credentials.json` plus cached `token.json` |
| AI       | Claude by default (`claude-opus-5`); Gemini/OpenAI selectable | Set with `AI_PROVIDER` in `.env` |
| Env      | `.venv` created with `uv` | `uv pip install --python .venv/bin/python -r requirements.txt` |

## 3. Project Structure
```
NutritionPlan/
├── app.py              # Streamlit UI: screens, workflows (create/update/plan)
├── drive.py            # OAuth login/token cache + Drive helpers (Drive class)
├── ai.py               # transcribe_notes(), generate_plan(), provider adapters
├── config.py           # reads .env
├── prompts/
│   ├── info_system.md  # transcription instructions
│   ├── plan_system.md  # plan-writing instructions ({{PLAN_TEMPLATE}} placeholder)
│   └── plan_template.md# plan sections (doctor can edit)
├── requirements.txt
├── .env.example        # copy to .env
├── .gitignore          # ignores .env, credentials.json, token.json, .venv
├── README.md           # setup: Drive API, OAuth, keys, run
└── Note.md
```

Drive layout (must match exactly): `NutritionPlan/<Patient>/Documents/{Photos/, Info.md, Extra_info.md}` and `NutritionPlan/<Patient>/Nutrition_Plan/PlanN.md`.

## 4. Setup & Run
See `README.md`. In short:
1. `cp .env.example .env` and set `ANTHROPIC_API_KEY`.
2. In Google Cloud: enable the Drive API, set up the OAuth consent screen (External, Testing, **add own Gmail as a test user**), create an OAuth client of type **Desktop app**, and save it as `credentials.json`.
3. `source .venv/bin/activate && streamlit run app.py`

## 5. Features
### Done (2026-09-15)
- Screen 1 Login: Google OAuth with a cached token, skipped automatically when the token is valid; creates `NutritionPlan/`.
- Screen 2 Search: case-insensitive exact match opens the patient; partial or duplicate matches show a pick-list with a "create new" option; no match goes to New Case Study.
- Screen 3 Patient options: Download Case files (per file plus ZIP), Open Case Study (Info, Extra info, photo gallery, plan dropdown), Update Case Study, Request Diet Plan, Back.
- Screen 4 New Case Study: uploader and camera, observations; creates the tree, uploads photos, writes Info.md and Extra_info.md, auto-generates Plan1.
- Screen 5 Update: add photos (default) or replace them (with a confirmation checkbox), append dated observations; regenerates Info.md when photos change; no plan generated.
- Plan generation: sends Info.md (photos only if Info.md is missing), Extra_info.md and the latest plan with the "Review this previous plan…" instruction; saves the next `PlanN.md` and never overwrites.
- Spinners/status for uploads and AI calls; Drive and AI errors shown on the page instead of crashing.

### Planned / Backlog
- _Nothing agreed yet._ Possible ideas: a button to regenerate Info.md on its own; HEIC photo support.

## 6. Key Decisions
| Date       | Decision | Reason |
|------------|----------|--------|
| 2026-09-15 | Keep project notes in `Note.md` | Carry context between chat sessions |
| 2026-09-15 | AI provider default **Claude `claude-opus-5`**; Gemini (`google-genai`) and OpenAI adapters included | Spec left provider as `[DECIDE]` and required it to be swappable |
| 2026-09-15 | Claude calls use streaming plus server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) | A safety decline is retried on a fallback model instead of failing |
| 2026-09-15 | Plan template = the spec's suggested sections, in `prompts/plan_template.md` | Spec left it as `[DECIDE: adjust to your practice]`; the doctor can edit it without touching code |
| 2026-09-15 | Full `drive` OAuth scope (not `drive.file`) | Lets the app see folders or files the doctor creates or moves manually in Drive |
| 2026-09-15 | Replaced photos are moved to **Drive trash**, not permanently deleted; new photos are uploaded *before* old ones are trashed | Safer destructive action (recoverable for 30 days) |
| 2026-09-15 | Uploaded photo names get a `YYYYMMDD-HHMMSS_NN_` prefix | Avoids name collisions (camera shots are all "image.jpg") and keeps them in chronological order |
| 2026-09-15 | Photos sent to the AI are EXIF-rotated, downscaled to 2400px and re-encoded as JPEG q90; originals stored untouched | Stays under provider image limits while keeping handwriting legible |
| 2026-09-15 | Plan generation uses Info.md whenever it exists | Info.md is always regenerated when photos change, so it is current; avoids re-sending every photo |
| 2026-09-15 | "Add" mode regenerates Info.md from **all** photos (existing + new) | Keeps one coherent transcription and summary |
| 2026-09-15 | App writes a header line on Info.md and each plan (timestamp, provider/model, "AI draft for clinician review") | Traceability |

## 7. Open Questions / Issues
- Not yet tested against real Google Drive or a real AI key; only smoke-tested with an in-memory fake Drive and a mocked AI (all checks passed). First real run still needed.
- Gemini/OpenAI default model names (`gemini-2.5-pro`, `gpt-5`) and adapters are untested; check them before switching providers.
- The user mentioned an earlier authentication failure; details unknown. README has a troubleshooting table (most likely causes: missing test user → 403 access_denied; Web client instead of Desktop → redirect_uri_mismatch; Testing-mode tokens expire after 7 days).
- WSL: if the browser doesn't open on Login, copy the sign-in URL printed in the Streamlit terminal.
- Privacy: patient data goes to Google Drive and the AI provider. The doctor should confirm this meets patient-consent and data-protection rules.

## 8. Changelog
- **2026-09-16**: Switched to **millionairenext01@gmail.com**. New Cloud project `nutritionplan-508807` with a Desktop OAuth client; the consent screen is **In production** (so no test-user list and no 7-day token expiry; sign-in shows the "unverified app" warning, which is expected). Signed in locally and confirmed via `about.get` that the app acts as millionairenext01. **First real end-to-end run succeeded**: patient "yuvraj" created from a photo → `Info.md` (3,990 chars, Gemini `gemini-3.6-flash`) → `Plan1.md`. Regenerated `.streamlit/secrets.toml` with the new token. Remaining for deployment: paste those secrets into the Streamlit app (`patient-case-study.streamlit.app`) and reboot it. Branding home page should become the Streamlit URL; privacy policy stays on GitHub because the app is password-protected.
- **2026-09-16**: **Google account `appstorageruk@gmail.com` was disabled by Google** ("created or used with multiple other accounts"), taking Cloud project `nutritionplan-508717` with it: token refresh now fails with `disabled_client: The OAuth client was disabled`. No patient data lost (the Drive folder was still empty). The Gemini API key still works. Moved `credentials.json` / `token.json` to `disabled_account_backup/` (git-ignored) and deleted the stale `.streamlit/secrets.toml`. Switching to **millionairenext01@gmail.com**: needs a new Cloud project, Drive API, OAuth consent (External/Testing + that address as test user), a new **Desktop** client saved as `credentials.json`, then a fresh local login and a regenerated secrets file. No code changes needed. Streamlit app `patient-case-study.streamlit.app` exists but has no secrets yet, so it shows "APP_PASSWORD is not set".
- **2026-09-15**: Google blocked **Publish app** until the Branding page has an app name, support email, homepage URL and privacy policy URL. Added `PRIVACY.md` (single-user tool; data stays in the owner's Drive; sent to the AI provider; Google API Limited Use statement) and pushed it. Branding URLs: homepage `https://github.com/yuvrajkhelkar01/NutritionPlan`, privacy `https://github.com/yuvrajkhelkar01/NutritionPlan/blob/main/PRIVACY.md`. Both depend on the repo staying **public**; if it's made private, host the policy somewhere else first.
- **2026-09-15**: Started Streamlit Cloud deployment. Pinned `requirements.txt` to the locally tested versions (streamlit 1.63.0, google-api-python-client 2.200.0, google-auth 2.58.0, google-auth-oauthlib 1.4.1, httplib2 0.32.0, google-genai 2.23.0, anthropic 1.6.0, Pillow 12.3.0, python-dotenv 1.2.3), verified with a clean Python 3.12 install, and pushed. The steps on share.streamlit.io are done by the user.
- **2026-09-15**: Pushed commit `3ef3fd0` to GitHub: <https://github.com/yuvrajkhelkar01/NutritionPlan> (branch `main`, remote `origin`). **The repo is public.** A private repo was recommended; the user decides. Push auth came from Windows Git Credential Manager (`/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager-core.exe`), passed with `git -c credential.helper=…` for that command only; no global git config was changed. Next: publish the OAuth app to production, then deploy on share.streamlit.io with `.streamlit/secrets.toml` pasted into Secrets.
- **2026-09-15**: **Prepared for Streamlit Community Cloud** (the user chose to deploy). `config.py` reads each setting from env/.env first, then `st.secrets`. New `GOOGLE_TOKEN_JSON` setting (the contents of token.json): when set, `drive.load_cached_credentials()` builds credentials from it, refreshes in memory only, skips the Login screen and hides Log out. Generated git-ignored `.streamlit/secrets.toml` (APP_PASSWORD, AI_PROVIDER, GEMINI_API_KEY, GEMINI_MODEL, GOOGLE_TOKEN_JSON) to paste into the Cloud Secrets box. `.gitignore` now also covers `credentials*.json` (there's a `credentials_old.json` holding the old Web client secret), `secrets.toml` and `.claude/settings.local.json`. README section 5 has the deploy steps. Tested cloud mode against real Drive with only the secret: pass. `git init` on `main`. No `gh` CLI and no SSH key, so the user pushes via VS Code "Publish to GitHub" (private). **Important:** the OAuth app must be switched to "In production"; in Testing mode the refresh token expires after 7 days (token issued 2026-09-15) and the cloud app would lose Drive access.
- **2026-09-15**: Added an **app password gate** (`APP_PASSWORD` in `.env` / Streamlit secrets). It is checked before anything else in every browser session; the app refuses to open while the password is empty; the check uses constant-time `hmac.compare_digest`; after 5 wrong attempts the app locks for 5 minutes, shared across sessions via `st.cache_resource`; the sidebar has a **Lock app** button. Reason: the user plans to make the app reachable from anywhere, and the Google OAuth token is shared by the app, so it does not identify visitors. Deployment discussion: plain "Deploy" on Streamlit Community Cloud is **not** enough, because the local-server OAuth flow doesn't work there and secrets aren't uploaded. Options offered: A) keep it local and reach it through Tailscale (no code changes), or B) cloud deploy (token and keys in secrets, private GitHub repo). The user hasn't chosen yet.
- **2026-09-15**: Project started. Created `Note.md`.
- **2026-09-15**: AI provider set to **Gemini**. The user created `.env` (`AI_PROVIDER=gemini`). `gemini-2.5-flash` returned 404 ("no longer available to new users"), so switched to **`gemini-3.6-flash`** in `.env` and as the default in `config.py`; a live test call with the key succeeded. Installed `google-genai` 2.23.0 and added it to `requirements.txt`. Disabled Gemini automatic function calling (unused; it printed a warning). Open: the real Gemini key is still in `.env.example` and should be removed from there.
- **2026-09-15**: **First real Google login succeeded** (after adding the account as a test user; the earlier block was `403 access_denied`). `token.json` saved with a refresh token; `NutritionPlan/` folder exists in Drive (no patients yet). Still to do: create `.env` and choose the AI provider (the user's Gemini key is currently in `.env.example`; `google-genai` is not installed).
- **2026-09-15**: Login fix. The user first created a *Web* OAuth client (redirect only `localhost:8501`), so it was replaced with a **Desktop app** client. Then Login hung under WSL: `webbrowser`/`gio` can't open a browser ("Operation not supported") and the URL was only printed to stdout. `drive.login(show_link)` now passes the URL to the UI through a custom `webbrowser` controller, and the app shows an **Open Google sign-in** button; the timeout is 180 s. Verified the local redirect round trip and the button rendering.
- **2026-09-15**: Built the full app per the spec: `app.py`, `drive.py`, `ai.py`, `config.py`, `prompts/`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`. Created `.venv` with dependencies (streamlit 1.63.0, anthropic 1.6.0). Smoke test (AppTest + fake Drive/AI) passed all checks: create, search, plan versioning, update add/replace, downloads, Claude request building.

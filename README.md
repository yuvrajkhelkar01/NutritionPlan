# NutritionPlan

A personal Streamlit app for one doctor. You photograph handwritten patient notes, and the app stores them per patient in your Google Drive. An AI transcribes the notes when you add photos. It drafts a nutrition plan, a basic exercise plan or homeopathic medicine recommendations only when you ask. If nothing was added to the case study since the last result, the saved result is shown and the AI is not called.

Every AI-generated plan is a draft. Review it before using it.

## Drive layout

The app creates this structure in your Drive:

```
NutritionPlan/
└── <PatientName>/
    ├── Documents/
    │   ├── Photos/          # uploaded case-note photos (jpg/png/pdf)
    │   ├── Info.md          # AI transcription + structured summary
    │   └── Extra_info.md    # your typed remarks, one dated section per update
    ├── Nutrition_Plan/
    │   └── NutritionPlan.md # overwritten on each request (Drive keeps version history)
    ├── Exercise_Plan/
    │   └── ExercisePlan.md  # overwritten on each request
    └── Homeopathic_Medicine/
        ├── MedicineList1.md
        └── MedicineList2.md …  # each generated or revised list is a new file
```

## 1. Install

Requires Python 3.10+.

```bash
cd NutritionPlan
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Google Drive setup (one time)

1. Go to <https://console.cloud.google.com/> and create a project (for example "NutritionPlan").
2. **Enable the API:** go to *APIs & Services → Library*, search for **Google Drive API**, and click **Enable**.
3. **OAuth consent screen:** go to *APIs & Services → OAuth consent screen* (called *Google Auth Platform* in newer consoles).
   - User type: **External**. App name: anything. Support and developer email: your Gmail.
   - Under **Test users** (*Audience* in newer consoles), **add your own Gmail address**. Without this, sign-in fails with `Error 403: access_denied`.
   - You can leave the app in **Testing** mode. You don't need to publish or verify it.
4. **Create credentials:** go to *APIs & Services → Credentials → Create credentials → OAuth client ID*.
   - Application type: **Desktop app**. Don't choose "Web application", which causes `redirect_uri_mismatch`.
   - Click **Download JSON**, rename the file to `credentials.json`, and put it in this folder.

On first login, Google warns that it "hasn't verified this app". Click **Continue**, because this is your own app. The token is saved to `token.json`, and later runs skip the login screen.

## 3. AI provider

Edit `.env`:

| Provider | Settings | Extra install |
|---|---|---|
| Claude (default) | `AI_PROVIDER=claude`, `ANTHROPIC_API_KEY=…` (from <https://console.anthropic.com/>) | – |
| Gemini | `AI_PROVIDER=gemini`, `GEMINI_API_KEY=…` | `pip install google-genai` |
| OpenAI | `AI_PROVIDER=openai`, `OPENAI_API_KEY=…` | `pip install openai` |

Model names can be changed with `CLAUDE_MODEL`, `GEMINI_MODEL` and `OPENAI_MODEL`.

## 3b. App password

Set `APP_PASSWORD` in `.env` (in Streamlit secrets when deployed). The app asks for it in every new browser session, before anything else, and **won't open at all while it's empty**. Use a long password that you don't use anywhere else.

- After 5 wrong attempts the app locks for 5 minutes, for everyone.
- **🔒 Lock app** in the sidebar locks it again. Reloading the page also asks for the password again.
- After changing the password, restart the app.

## 4. Run

```bash
source .venv/bin/activate
streamlit run app.py
```

Open the URL Streamlit prints (usually <http://localhost:8501>).

**Login:** click **Login**, then **Open Google sign-in**. After you allow access, Google redirects to a temporary `localhost` page and the app continues on its own. This also works under WSL with a Windows browser.

## 5. Deploy to Streamlit Community Cloud

The Google sign-in screen only works on your own computer. In the cloud, the app uses the login you already made locally, supplied as a secret.

1. **Sign in locally first** (sections 1–4), so `token.json` exists.
2. **Keep the Google login from expiring.** In Google Cloud → *Google Auth Platform → Audience*, click **Publish app** ("In production"). While the app is in *Testing*, Google expires the saved login after 7 days and the cloud app stops working. You don't need verification for personal use; you'll just keep seeing the "unverified app" warning when you sign in.
3. **Create the secrets file:** `.streamlit/secrets.toml` holds `APP_PASSWORD`, the AI settings and `GOOGLE_TOKEN_JSON` (the contents of `token.json`). It's git-ignored.
4. **Push the code to a private GitHub repository.** `.env`, `credentials*.json`, `token.json` and `secrets.toml` are git-ignored.
5. On <https://share.streamlit.io>: **Create app** → pick the repo, branch `main`, main file `app.py`. Under **Advanced settings**, choose Python 3.12 and paste the contents of `.streamlit/secrets.toml` into **Secrets**. Deploy.
6. Open the app, enter the password, and it goes straight to **Search for patient**.

If the cloud app later says the Google login expired: sign in again locally, then replace `GOOGLE_TOKEN_JSON` in the app's Secrets with the new `token.json`.

Anyone with the URL reaches the password screen, so use a strong `APP_PASSWORD`. You can also restrict viewers in the app's sharing settings on Streamlit Cloud.

## Using the app

- **Search**: finds patient folders by name, ignoring case. Partial matches appear as a pick-list.
- **New case study**: when no patient matches, upload photos (or use the camera) and add observations. The app creates the folders, transcribes the photos into `Info.md`, and writes `Extra_info.md`. No plan is generated.
- **Update Case Study**: adds photos (or replaces them all, after you confirm) and appends dated observations. `Info.md` is regenerated only when photos were added or replaced; observations alone make no AI call. Saving does not create a plan.
- **Request Diet Plan**: if `Info.md`, `Extra_info.md` or a photo changed since `NutritionPlan.md` was saved, the app reads them, sends them with the saved plan to the AI, and overwrites `NutritionPlan.md`. If nothing changed, the saved plan is shown without an AI call; use **Regenerate anyway** to force a new one.
- **Request Exercise Plan**: the same, for `Exercise_Plan/ExercisePlan.md`.
- Older numbered plans (`PlanN.md`, `ExercisePlanN.md`) from earlier app versions are left in Drive. The newest one is used until the first new plan is saved.
- **Homeopathic Medicines**: opens a page with two steps. (1) **Generate recommendations** from `Info.md` and `Extra_info.md`, plus optional input for this request. If nothing was added since the last list and the input box is empty, the last list is shown without an AI call (**Generate new recommendations anyway** forces one). (2) Type **your recommendations** and click **Create revised list**. The AI rewrites the list shown, applying your changes. Each list is a single table: Recommendation, Potency, Rate. Each list is saved as the next `MedicineListN.md` with your input quoted at the top, so earlier lists are kept. You can revise as many times as you like.
- **Download Case files / Open Patient Case Study**: download individual files or a ZIP, or read everything in the app.

Replaced photos go to Drive **trash**, so they can be recovered for 30 days.

## Customising the prompts

- `prompts/plan_template.md`: the sections every plan follows. Edit it to match your practice.
- `prompts/plan_system.md`: general instructions for plan writing.
- `prompts/exercise_template.md` / `prompts/exercise_system.md`: the same for exercise plans.
- `prompts/medicine_template.md` / `prompts/medicine_system.md`: the same for homeopathic medicine lists.
- `prompts/info_system.md`: how the handwritten notes are transcribed.

Changes take effect on the next AI call. You don't need to restart the app.

## Troubleshooting sign-in

| Symptom | Fix |
|---|---|
| `Error 403: access_denied` / "app has not completed verification" | Add your Gmail under **Test users** on the OAuth consent screen. |
| `redirect_uri_mismatch` | The OAuth client must be a **Desktop app**. Create a new one and replace `credentials.json`. |
| `OAuth client file not found` | Put `credentials.json` in the project folder, or set `GOOGLE_CREDENTIALS_FILE` in `.env`. |
| Logged out again after about 7 days | Testing-mode apps get refresh tokens that expire after 7 days. Log in again, or publish the app to "In production" on the consent screen. |
| "session expired or was revoked" | Click **Log out** in the sidebar and log in again. |

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI and screen flow |
| `drive.py` | Google OAuth and Drive helpers |
| `ai.py` | Transcription, nutrition/exercise plans and medicine recommendations (Claude / Gemini / OpenAI) |
| `config.py` | Reads `.env` |
| `prompts/` | Editable AI prompts and the plan templates |
| `Note.md` | Development notes |

`credentials.json`, `token.json` and `.env` hold secrets. Keep them private (they are git-ignored).

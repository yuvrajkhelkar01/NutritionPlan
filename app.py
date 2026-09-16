"""NutritionPlan: Streamlit UI.

Screens: Login → Search → Patient options | New case study → Update case study.
Patient data lives only in Google Drive (drive.py); AI calls are in ai.py.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import io
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime

import streamlit as st

import ai
import config
import drive
from ai import AIError, Attachment
from drive import DriveError, DriveFile, PatientFolders

st.set_page_config(page_title="NutritionPlan", page_icon="🥗")

UPLOAD_TYPES = ["jpg", "jpeg", "png", "pdf"]
MIME_BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


@dataclass(frozen=True)
class PlanKind:
    key: str  # ai.PLAN_PROMPTS key
    title: str  # document title, e.g. "Nutrition Plan"
    prefix: str  # file name prefix: <prefix>N.md
    folder: str  # PatientFolders attribute holding these plans

    def file_name(self, number: int) -> str:
        return f"{self.prefix}{number}.md"


NUTRITION = PlanKind("nutrition", "Nutrition Plan", drive.PLAN_PREFIX, "plans")
EXERCISE = PlanKind("exercise", "Exercise Plan", drive.EXERCISE_PLAN_PREFIX, "exercise_plans")
MEDICINE = PlanKind("medicine", "Homeopathic Medicine List", drive.MEDICINE_LIST_PREFIX, "medicine")
PLAN_KINDS = {k.key: k for k in (NUTRITION, EXERCISE, MEDICINE)}

DEFAULT_STATE = {
    "authenticated": False,  # passed the app password in this browser session
    "screen": "login",
    "drive": None,  # drive.Drive once signed in
    "root_id": None,  # id of NutritionPlan/ in Drive
    "patient": None,  # DriveFile of the selected patient folder
    "search_query": "",
    "matches": [],  # pick-list after an ambiguous search
    "view": None,  # panel on the patient screen: download | open | plan
    "last_plan": None,  # (plan kind key, number, markdown) of the plan just generated
    "cache": {},  # patient id -> data already fetched from Drive this session
    "flash": None,  # one-off success message
    "form_nonce": 0,  # bumped to clear upload/text widgets
    "camera_shots": [],  # [(sha1, Attachment)] captured with the camera
    "medicine_list": None,  # (number, markdown) of the medicine list shown on the medicine screen
}


# ================================================================ state & helpers


def init_state() -> None:
    for key, value in DEFAULT_STATE.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def go(screen: str, **updates) -> None:
    for key, value in updates.items():
        st.session_state[key] = value
    st.session_state.screen = screen
    st.rerun()


def open_patient(patient: DriveFile) -> None:
    go("patient", patient=patient, view=None, last_plan=None, matches=[])


def reset_form() -> None:
    st.session_state.form_nonce += 1
    st.session_state.camera_shots = []


class _Outcome:
    ok = False


@contextmanager
def guarded(action: str):
    """Show Drive/AI failures on the page instead of crashing. Check `.ok` afterwards."""
    outcome = _Outcome()
    try:
        yield outcome
    except (DriveError, AIError) as e:
        st.error(str(e))
    except Exception as e:  # keep the app usable whatever goes wrong
        st.error(f"Unexpected error while {action}: {e}")
    else:
        outcome.ok = True


def client() -> drive.Drive:
    return st.session_state.drive


def patient_cache() -> dict:
    return st.session_state.cache.setdefault(st.session_state.patient.id, {})


def invalidate_patient_cache() -> None:
    if st.session_state.patient:
        st.session_state.cache.pop(st.session_state.patient.id, None)


def folders() -> PatientFolders:
    cache = patient_cache()
    if "folders" not in cache:
        cache["folders"] = client().patient_folders(st.session_state.patient)
    return cache["folders"]


def file_bytes(file: DriveFile) -> bytes:
    store = patient_cache().setdefault("bytes", {})
    if file.id not in store:
        store[file.id] = client().download_bytes(file.id, file.name)
    return store[file.id]


def case_listing() -> dict:
    """Info.md, Extra_info.md, photos and plans for the selected patient (cached)."""
    cache = patient_cache()
    if "listing" not in cache:
        f = folders()
        docs = {x.name: x for x in client().list_children(f.documents, folders=False)}
        cache["listing"] = {
            "info": docs.get(drive.INFO_FILE),
            "extra": docs.get(drive.EXTRA_INFO_FILE),
            "photos": client().list_children(f.photos, folders=False),
            "plans": client().list_plans(f.plans, NUTRITION.prefix),
            "exercise_plans": client().list_plans(f.exercise_plans, EXERCISE.prefix),
            "medicine": client().list_plans(f.medicine, MEDICINE.prefix),
        }
    return cache["listing"]


def download_attachment(file: DriveFile) -> Attachment:
    return Attachment(file.name, file.mime_type, client().download_bytes(file.id, file.name))


def mime_for(filename: str) -> str:
    return MIME_BY_EXT.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ================================================================ document builders


def compose_info(name: str, photo_count: int, body: str | None) -> str:
    if body is None:
        return f"# Case Notes: {name}\n\n_No case-note photos uploaded yet._\n"
    return (
        f"# Case Notes: {name}\n\n"
        f"_Transcribed {timestamp()} from {photo_count} photo(s) by {ai.provider_label()}. "
        f"Check against the original photos._\n\n{body}\n"
    )


def extra_info_entry(notes: str) -> str:
    return f"## {date.today():%Y-%m-%d}\n\n{notes.strip()}\n"


def compose_plan(kind: PlanKind, number: int, name: str, body: str) -> str:
    return (
        f"# {kind.title} {number}: {name}\n\n"
        f"_Generated {timestamp()} by {ai.provider_label()}. AI draft for clinician review._\n\n{body}\n"
    )


def compose_medicine_list(number: int, name: str, body: str, doctor_input: str, revised_from: int | None) -> str:
    source = f", revised from {MEDICINE.file_name(revised_from)} using the doctor's input" if revised_from else ""
    text = (
        f"# {MEDICINE.title} {number}: {name}\n\n"
        f"_Generated {timestamp()} by {ai.provider_label()}{source}. AI draft for clinician review._\n\n"
    )
    if doctor_input.strip():
        quoted = "\n".join(f"> {line}".rstrip() for line in doctor_input.strip().splitlines())
        text += f"## Doctor's Input\n\n{quoted}\n\n"
    return text + f"{body}\n"


# ================================================================ Drive + AI workflows


def upload_photos(f: PatientFolders, photos: list[Attachment], log) -> list[Attachment]:
    """Upload with a timestamp prefix so names never collide; returns the photos under their Drive names."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = []
    for i, photo in enumerate(photos, 1):
        log(f"Uploading photo {i} of {len(photos)}: {photo.name}")
        name = f"{stamp}_{i:02d}_{photo.name}"
        client().upload_bytes(f.photos, name, photo.data, photo.mime_type)
        saved.append(Attachment(name, photo.mime_type, photo.data))
    return saved


def read_case(f: PatientFolders, log) -> tuple[str | None, str, list[Attachment]]:
    """(Info.md, Extra_info.md, photos). Photos are only downloaded when Info.md is missing."""
    log("Reading the case study from Drive…")
    info_md = client().read_text(f.documents, drive.INFO_FILE)
    extra_md = client().read_text(f.documents, drive.EXTRA_INFO_FILE) or ""
    photos = []
    if not (info_md and info_md.strip()):
        log("Info.md not found, so the photos will be sent instead…")
        photos = [download_attachment(x) for x in client().list_children(f.photos, folders=False)
                  if x.mime_type in ai.SUPPORTED_MIMES]
    return info_md, extra_md, photos


def save_next_version(kind: PlanKind, f: PatientFolders, compose, log) -> tuple[int, str]:
    """Save compose(number) as the next <prefix>N.md; returns (number, text)."""
    plans_id = getattr(f, kind.folder)
    latest = client().list_plans(plans_id, kind.prefix)  # listed just before saving in case one was added meanwhile
    number = latest[-1][0] + 1 if latest else 1
    text = compose(number)
    log(f"Saving {kind.file_name(number)} to Drive…")
    client().save_new_plan(plans_id, number, text, kind.prefix)
    return number, text


def generate_and_save_plan(kind: PlanKind, log) -> tuple[str, int, str]:
    f, name = folders(), st.session_state.patient.name
    plans_id = getattr(f, kind.folder)
    try:
        info_md, extra_md, photos = read_case(f, log)
        plans = client().list_plans(plans_id, kind.prefix)
        previous = None
        if plans:
            number, plan_file = plans[-1]
            previous = (number, client().download_bytes(plan_file.id, plan_file.name).decode("utf-8"))

        updating = f" (updating {kind.file_name(previous[0])})" if previous else ""
        log(f"Writing the {kind.title.lower()} with {ai.provider_label()}{updating}…")
        body = ai.generate_plan(name, info_md, extra_md, previous, photos, kind=kind.key)
        number, text = save_next_version(kind, f, lambda n: compose_plan(kind, n, name, body), log)
        return kind.key, number, text
    finally:
        invalidate_patient_cache()


def generate_and_save_medicines(doctor_input: str, current: tuple[int, str] | None, log) -> tuple[int, str]:
    """New homeopathic medicine list: fresh when `current` is None, otherwise `current` revised with the doctor's input."""
    f, name = folders(), st.session_state.patient.name
    try:
        info_md, extra_md, photos = read_case(f, log)
        if current:
            log(f"Revising {MEDICINE.file_name(current[0])} with your recommendations using {ai.provider_label()}…")
        else:
            log(f"Writing homeopathic medicine recommendations with {ai.provider_label()}…")
        body = ai.recommend_medicines(name, info_md, extra_md, doctor_input, current, photos)
        revised_from = current[0] if current else None
        return save_next_version(
            MEDICINE, f, lambda n: compose_medicine_list(n, name, body, doctor_input, revised_from), log
        )
    finally:
        invalidate_patient_cache()


def create_patient(name: str, photos: list[Attachment], notes: str, log) -> PatientFolders:
    exact, _ = client().search_patients(st.session_state.root_id, name)
    if exact:
        raise DriveError(f"A patient named '{exact[0].name}' already exists. Go back and search for them.")

    log("Creating patient folders…")
    f = client().create_patient(st.session_state.root_id, name)
    st.session_state.patient = f.patient
    patient_cache()["folders"] = f

    saved = upload_photos(f, photos, log) if photos else []
    if saved:
        log(f"Transcribing {len(saved)} photo(s) into Info.md…")
        client().write_text(f.documents, drive.INFO_FILE, compose_info(name, len(saved), ai.transcribe_notes(name, saved)))
    else:
        client().write_text(f.documents, drive.INFO_FILE, compose_info(name, 0, None))

    log("Writing Extra_info.md…")
    extra = f"# Extra Information: {name}\n\n" + (extra_info_entry(notes) if notes.strip() else "")
    client().write_text(f.documents, drive.EXTRA_INFO_FILE, extra)
    return f


def update_case_study(photos: list[Attachment], replace: bool, notes: str, log) -> None:
    f, name = folders(), st.session_state.patient.name
    try:
        if photos:
            existing = client().list_children(f.photos, folders=False)
            saved = upload_photos(f, photos, log)
            if replace:
                log(f"Moving {len(existing)} old photo(s) to Drive trash…")
                for old in existing:
                    client().trash(old)
                source = saved
            else:
                log("Downloading existing photos to rebuild Info.md…")
                kept = [download_attachment(x) for x in existing if x.mime_type in ai.SUPPORTED_MIMES]
                source = sorted(kept + saved, key=lambda a: a.name)
            log(f"Transcribing {len(source)} photo(s) into Info.md…")
            try:
                body = ai.transcribe_notes(name, source)
            except AIError as e:
                raise AIError(f"Photos were saved, but Info.md could not be regenerated: {e}") from e
            client().write_text(f.documents, drive.INFO_FILE, compose_info(name, len(source), body))

        if notes.strip():
            log("Adding observations to Extra_info.md…")
            current = client().read_text(f.documents, drive.EXTRA_INFO_FILE)
            if current is None:
                current = f"# Extra Information: {name}\n"
            client().write_text(f.documents, drive.EXTRA_INFO_FILE, current.rstrip() + "\n\n" + extra_info_entry(notes))
    finally:
        invalidate_patient_cache()


# ================================================================ shared widgets


def photo_inputs() -> list[Attachment]:
    """File uploader plus optional camera; returns all photos chosen so far."""
    nonce = st.session_state.form_nonce
    uploaded = st.file_uploader(
        "Upload images", type=UPLOAD_TYPES, accept_multiple_files=True, key=f"upload-{nonce}",
        help="Photos or scans of the case notes (JPG, PNG or PDF).",
    )
    photos = [Attachment(u.name, mime_for(u.name), u.getvalue()) for u in uploaded or []]

    if st.toggle("Take photos with camera", key=f"camera-on-{nonce}"):
        shot = st.camera_input("Photograph a notebook page", key=f"camera-{nonce}")
        shots = st.session_state.camera_shots
        if shot is not None:
            digest = hashlib.sha1(shot.getvalue()).hexdigest()
            if digest not in {d for d, _ in shots}:
                shots.append((digest, Attachment(f"camera_{len(shots) + 1:02d}.jpg", "image/jpeg", shot.getvalue())))
        if shots:
            left, right = st.columns([3, 1])
            left.caption(f"{len(shots)} camera photo(s) added. Clear the photo above to take another.")
            if right.button("Remove all", key=f"camera-clear-{nonce}"):
                st.session_state.camera_shots = []
                st.rerun()
    photos.extend(att for _, att in st.session_state.camera_shots)
    return photos


def show_flash() -> None:
    if st.session_state.flash:
        st.success(st.session_state.flash)
        st.session_state.flash = None


# ================================================================ screens


@st.cache_resource
def password_guard() -> dict:
    """Failed-attempt counter shared by every session, so reloading the page doesn't reset it."""
    return {"failures": 0, "locked_until": 0.0}


def password_gate() -> bool:
    """True once this browser session has entered APP_PASSWORD; otherwise renders the password screen."""
    if st.session_state.authenticated:
        return True
    st.title("🥗 NutritionPlan")
    if not config.APP_PASSWORD:
        st.error("APP_PASSWORD is not set. Add it to .env (or Streamlit secrets when deployed) and restart the app.")
        return False

    guard = password_guard()
    remaining = guard["locked_until"] - time.time()
    if remaining > 0:
        st.error(f"Too many wrong passwords. Try again in {int(remaining // 60) + 1} minute(s).")
        return False

    with st.form("password"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary")
    if not submitted:
        return False
    if hmac.compare_digest(password.encode("utf-8"), config.APP_PASSWORD.encode("utf-8")):
        guard["failures"] = 0
        st.session_state.authenticated = True
        st.rerun()

    guard["failures"] += 1
    time.sleep(1)  # slow down guessing
    if guard["failures"] >= MAX_FAILED_ATTEMPTS:
        guard["failures"] = 0
        guard["locked_until"] = time.time() + LOCKOUT_SECONDS
        st.error(f"Too many wrong passwords. Locked for {LOCKOUT_SECONDS // 60} minutes.")
    else:
        st.error("Wrong password.")
    return False


def sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🥗 NutritionPlan")
        st.caption(f"AI: {ai.provider_label()}")
        if st.button("🔒 Lock app"):
            st.session_state.authenticated = False
            st.rerun()
        if st.session_state.drive is not None:
            st.caption("Signed in to Google Drive")
            # With GOOGLE_TOKEN_JSON the login comes from secrets, so there is nothing to log out of.
            if not config.GOOGLE_TOKEN_JSON and st.button("Log out"):
                drive.logout()
                for key, value in DEFAULT_STATE.items():
                    st.session_state[key] = copy.deepcopy(value)
                st.rerun()


def screen_login() -> None:
    with guarded("loading the saved Google login") as outcome:
        creds = drive.load_cached_credentials()
    if not outcome.ok:
        return
    if creds is None:
        st.title("NutritionPlan")
        st.write("Sign in with your Google account to open patient case studies stored in your Google Drive.")
        if not st.button("Login", type="primary"):
            return

        def show_link(url: str) -> None:
            st.link_button("Open Google sign-in", url, type="primary")
            st.caption("The sign-in page opens in a new tab. Choose your Google account and allow Drive access; "
                       "this page continues automatically when you're done.")

        with guarded("signing in") as outcome, st.spinner(
            f"Waiting for you to finish Google sign-in (up to {drive.LOGIN_TIMEOUT_SECONDS // 60} minutes)…"
        ):
            creds = drive.login(show_link)
        if not outcome.ok:
            return

    with guarded("connecting to Google Drive") as outcome, st.spinner("Connecting to Google Drive…"):
        drive_client = drive.Drive(creds)
        root_id = drive_client.ensure_root()
    if outcome.ok:
        go("search", drive=drive_client, root_id=root_id)
    else:
        st.info("If this keeps failing, use **Log out** in the sidebar and sign in again.")


def screen_search() -> None:
    st.title("Search for patient")
    with st.form("search"):
        query = st.text_input("Patient name", value=st.session_state.search_query)
        submitted = st.form_submit_button("Search", type="primary")

    if submitted:
        query = query.strip()
        st.session_state.matches = []
        if not query:
            st.warning("Enter a patient name.")
            return
        st.session_state.search_query = query
        with guarded("searching for the patient") as outcome, st.spinner("Searching Drive…"):
            exact, partial = client().search_patients(st.session_state.root_id, query)
        if not outcome.ok:
            return
        if len(exact) == 1:
            open_patient(exact[0])
        elif exact or partial:
            st.session_state.matches = exact + partial
        else:
            go("new")

    matches = st.session_state.matches
    if matches:
        st.subheader("Did you mean…")
        choice = st.radio("Matching patients", matches, format_func=lambda p: p.name, index=None)
        left, right = st.columns(2)
        if left.button("Open selected patient", type="primary", disabled=choice is None):
            open_patient(choice)
        if right.button(f"Create new patient “{st.session_state.search_query}”"):
            go("new", matches=[])


def screen_patient() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    show_flash()
    st.title(patient.name)

    row1 = st.columns(2)
    row2 = st.columns(2)
    row3 = st.columns(2)
    row4 = st.columns(2)
    if row1[0].button("⬇️ Download Case files", width="stretch"):
        st.session_state.view = "download"
    if row1[1].button("📖 Open Patient Case Study", width="stretch"):
        st.session_state.view = "open"
    requested = None
    if row2[0].button("🥗 Request Diet Plan", type="primary", width="stretch"):
        requested = NUTRITION
    if row2[1].button("🏃 Request Exercise Plan", type="primary", width="stretch"):
        requested = EXERCISE
    if row3[0].button("💊 Homeopathic Medicines", type="primary", width="stretch"):
        reset_form()
        go("medicine", view=None, medicine_list=None)
    if row4[0].button("✏️ Update Case Study", width="stretch"):
        reset_form()
        go("update")
    if row4[1].button("← Back to search", width="stretch"):
        go("search", patient=None, view=None, last_plan=None)
    st.divider()

    if requested:
        st.session_state.view = None
        label = requested.title.lower()
        with guarded(f"generating the {label}") as outcome, st.status(f"Generating {label}…", expanded=True) as status:
            st.session_state.last_plan = generate_and_save_plan(requested, st.write)
            status.update(label=f"Saved {requested.file_name(st.session_state.last_plan[1])}", state="complete", expanded=False)
        if outcome.ok:
            st.session_state.view = "plan"

    view = st.session_state.view
    if view == "download":
        render_downloads()
    elif view == "open":
        render_case_study()
    elif view == "plan" and st.session_state.last_plan:
        render_new_plan()


def render_downloads() -> None:
    st.subheader("Case files")
    name = st.session_state.patient.name
    with guarded("loading case files") as outcome, st.spinner("Loading files from Drive…"):
        cache = patient_cache()
        if "tree" not in cache:
            cache["tree"] = client().walk(st.session_state.patient.id)
        files = cache["tree"]
        payload = {path: file_bytes(f) for path, f in files if not f.mime_type.startswith(GOOGLE_NATIVE_PREFIX)}
        if "zip" not in cache:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, data in payload.items():
                    zf.writestr(f"{name}/{path}", data)
            cache["zip"] = buf.getvalue()
    if not outcome.ok:
        return
    if not files:
        st.info("This patient has no files yet.")
        return

    st.download_button("Download all as ZIP", cache["zip"], file_name=f"{name}.zip", mime="application/zip", type="primary", on_click="ignore")
    for path, f in files:
        if path in payload:
            st.download_button(f"{path}", payload[path], file_name=f.name, mime=f.mime_type, key=f"dl-{f.id}", on_click="ignore")
        else:
            st.caption(f"{path} (Google Docs file, open it in Drive)")


def render_case_study() -> None:
    with guarded("loading the case study") as outcome, st.spinner("Loading case study…"):
        listing = case_listing()

        st.subheader("Case notes (Info.md)")
        with st.container(border=True):
            st.markdown(file_bytes(listing["info"]).decode("utf-8") if listing["info"] else "_Info.md has not been created yet._")

        st.subheader("Extra observations (Extra_info.md)")
        with st.container(border=True):
            st.markdown(file_bytes(listing["extra"]).decode("utf-8") if listing["extra"] else "_No observations yet._")

        photos = listing["photos"]
        st.subheader(f"Photos ({len(photos)})")
        images = [p for p in photos if p.mime_type in ("image/jpeg", "image/png")]
        others = [p for p in photos if p not in images and not p.mime_type.startswith(GOOGLE_NATIVE_PREFIX)]
        if not photos:
            st.caption("No photos uploaded.")
        columns = st.columns(3)
        for i, image in enumerate(images):
            columns[i % 3].image(file_bytes(image), caption=image.name, width="stretch")
        for other in others:
            st.download_button(f"{other.name}", file_bytes(other), file_name=other.name, mime=other.mime_type, key=f"open-{other.id}", on_click="ignore")

        st.subheader("Nutrition plan")
        render_plan_versions(NUTRITION, listing["plans"], "Request Diet Plan")

        st.subheader("Exercise plan")
        render_plan_versions(EXERCISE, listing["exercise_plans"], "Request Exercise Plan")

        st.subheader("Homeopathic medicines")
        render_plan_versions(MEDICINE, listing["medicine"], "Homeopathic Medicines")
    if not outcome.ok:
        st.caption("Try again, or go back to search.")


def render_plan_versions(kind: PlanKind, plans: list[tuple[int, DriveFile]], button: str) -> None:
    if not plans:
        st.info(f"Nothing saved yet. Use **{button}** to create one.")
        return
    by_number = dict(plans)
    numbers = sorted(by_number, reverse=True)
    number = st.selectbox(
        "Version", numbers, key=f"version-{kind.key}",
        format_func=lambda n: kind.file_name(n) + (" (latest)" if n == numbers[0] else ""),
    )
    plan_file = by_number[number]
    text = file_bytes(plan_file).decode("utf-8")
    with st.container(border=True):
        st.markdown(text)
    st.download_button(f"Download {plan_file.name}", text, file_name=plan_file.name, mime="text/markdown", key=f"dl-{kind.key}", on_click="ignore")


def render_new_plan() -> None:
    key, number, text = st.session_state.last_plan
    file_name = PLAN_KINDS[key].file_name(number)
    st.success(f"{file_name} saved to Drive.")
    with st.container(border=True):
        st.markdown(text)
    st.download_button(f"Download {file_name}", text, file_name=file_name, mime="text/markdown", type="primary", on_click="ignore")


def screen_new() -> None:
    name = st.session_state.search_query
    nonce = st.session_state.form_nonce
    st.title("New case study")
    st.markdown(f"No patient named ***{name}*** found. Create new case study?")

    photos = photo_inputs()
    notes = st.text_area("Extra observations (text from doctor)", key=f"notes-{nonce}", height=160)

    left, right = st.columns(2)
    create = left.button("Create patient", type="primary", width="stretch")
    if right.button("← Back to search", width="stretch"):
        reset_form()
        go("search")
    if not create:
        return
    if not photos and not notes.strip():
        st.warning("Add at least one photo or some observations.")
        return

    with guarded("creating the patient") as outcome, st.status(f"Creating case study for {name}…", expanded=True) as status:
        create_patient(name, photos, notes, st.write)
        st.session_state.last_plan = generate_and_save_plan(NUTRITION, st.write)
        status.update(label="Case study created and Plan1.md saved", state="complete", expanded=False)

    if outcome.ok:
        reset_form()
        go("patient", view="plan", flash=f"Created case study for {name}.")
    elif st.session_state.patient is not None:
        # The folder exists even though a later step failed; let the doctor continue from the patient page.
        if st.button("Open patient page"):
            reset_form()
            open_patient(st.session_state.patient)


def screen_update() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    nonce = st.session_state.form_nonce
    st.title("Update case study")
    st.markdown(f"Patient: **{patient.name}**")

    photos = photo_inputs()
    replace = st.toggle(
        "Replace existing photos", value=False, key=f"replace-{nonce}",
        help="Off: new photos are added to the existing ones. On: existing photos are deleted and Info.md is rebuilt from the new photos only.",
    )
    confirmed = True
    if replace:
        st.warning("All existing photos will be deleted (moved to Drive trash) and Info.md will be regenerated from the new photos only.")
        confirmed = st.checkbox("Yes, replace all existing photos", key=f"confirm-{nonce}")
    notes = st.text_area("Extra observations (text from doctor)", key=f"notes-{nonce}", height=160)

    left, right = st.columns(2)
    save = left.button("Save", type="primary", width="stretch")
    if right.button("Cancel", width="stretch"):
        reset_form()
        go("patient", view=None)
    if not save:
        return
    if replace and not photos:
        st.warning("Upload the new photos that should replace the existing ones.")
        return
    if replace and not confirmed:
        st.warning("Tick the confirmation box to replace the existing photos.")
        return
    if not photos and not notes.strip():
        st.warning("Nothing to save. Add photos or observations.")
        return

    with guarded("saving the case study") as outcome, st.status("Saving case study…", expanded=True) as status:
        update_case_study(photos, replace, notes, st.write)
        status.update(label="Case study saved", state="complete", expanded=False)
    if outcome.ok:
        reset_form()
        go("patient", view=None, last_plan=None, flash="Case study updated. Use Request Diet Plan or Request Exercise Plan to generate new plans.")


def screen_medicine() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    nonce = st.session_state.form_nonce
    st.title("Homeopathic medicines")
    st.markdown(f"Patient: **{patient.name}**")
    if st.button("← Back to patient"):
        reset_form()
        go("patient", view=None, medicine_list=None)

    st.subheader("1. Get recommendations")
    st.caption("Based on Info.md and Extra_info.md.")
    extra = st.text_area(
        "Additional input for this request (optional)", key=f"medicine-extra-{nonce}", height=120,
        placeholder="For example: current symptoms, modalities, or remedies already tried.",
    )
    if st.button("Generate recommendations", type="primary"):
        with guarded("generating medicine recommendations") as outcome, st.status("Generating recommendations…", expanded=True) as status:
            st.session_state.medicine_list = generate_and_save_medicines(extra, None, st.write)
            status.update(label=f"Saved {MEDICINE.file_name(st.session_state.medicine_list[0])}", state="complete", expanded=False)

    current = st.session_state.medicine_list
    if current is None:
        return
    number, text = current
    st.success(f"{MEDICINE.file_name(number)} saved to Drive.")
    with st.container(border=True):
        st.markdown(text)
    st.download_button(f"Download {MEDICINE.file_name(number)}", text, file_name=MEDICINE.file_name(number),
                       mime="text/markdown", on_click="ignore")

    st.subheader("2. Your recommendations")
    st.caption(f"Describe the changes you want. A new list is created from {MEDICINE.file_name(number)} and your input; "
               "the list above is kept.")
    doctor_input = st.text_area(
        "Doctor's recommendations", key=f"medicine-doctor-{nonce}-{number}", height=160,
        placeholder="For example: use Kali bichromicum 30C instead of Pulsatilla; add Belladonna 200C for acute fever.",
    )
    if st.button("Create revised list", type="primary"):
        if not doctor_input.strip():
            st.warning("Enter your recommendations first.")
            return
        with guarded("revising the medicine list") as outcome, st.status("Creating revised list…", expanded=True) as status:
            revised = generate_and_save_medicines(doctor_input, current, st.write)
            status.update(label=f"Saved {MEDICINE.file_name(revised[0])}", state="complete", expanded=False)
        if outcome.ok:
            st.session_state.medicine_list = revised
            st.rerun()


# ================================================================ main

SCREENS = {
    "login": screen_login,
    "search": screen_search,
    "patient": screen_patient,
    "new": screen_new,
    "update": screen_update,
    "medicine": screen_medicine,
}


def main() -> None:
    init_state()
    if not password_gate():
        return
    sidebar()
    screen = st.session_state.screen
    if st.session_state.drive is None:
        screen = "login"
    SCREENS[screen]()


main()

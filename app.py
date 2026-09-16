"""NutritionPlan: Streamlit UI.

Screens: Login → Search (with the records chat) → Patient options | New case study → Update case study.
Patient data lives only in Google Drive (drive.py); AI calls are in ai.py; the chat's record search is in chat.py.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import html
import io
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import streamlit as st

import ai
import chat
import config
import drive
import pdf
from ai import AIError, Attachment
from drive import DriveError, DriveFile, PatientFolders

st.set_page_config(page_title="NutritionPlan", page_icon="🥗")

UPLOAD_TYPES = ["jpg", "jpeg", "png", "pdf"]
MIME_BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

STYLE_FILE = Path(__file__).with_name("style.css")
WELCOME_BARS = [(44, ""), (78, "gold"), (36, ""), (70, "gold"), (58, ""), (100, "gold"), (50, ""), (84, "gold"), (62, "")]  # (height %, class)


@dataclass(frozen=True)
class PlanKind:
    key: str  # ai.PLAN_PROMPTS key
    title: str  # document title, e.g. "Nutrition Plan"
    folder: str  # PatientFolders attribute holding the plan
    file: str  # the plan file, overwritten on each request
    legacy_prefix: str  # numbered <prefix>N.md plans written by earlier app versions


NUTRITION = PlanKind("nutrition", "Nutrition Plan", "plans", drive.NUTRITION_PLAN_FILE, drive.LEGACY_PLAN_PREFIX)
EXERCISE = PlanKind("exercise", "Exercise Plan", "exercise_plans", drive.EXERCISE_PLAN_FILE, drive.LEGACY_EXERCISE_PLAN_PREFIX)
PLAN_KINDS = {k.key: k for k in (NUTRITION, EXERCISE)}

MEDICINE_TITLE = "Homeopathic Medicine List"


def medicine_file(number: int) -> str:
    return f"{drive.MEDICINE_LIST_PREFIX}{number}.md"

DEFAULT_STATE = {
    "authenticated": False,  # passed the app password in this browser session
    "screen": "login",
    "drive": None,  # drive.Drive once signed in
    "root_id": None,  # id of NutritionPlan/ in Drive
    "patient": None,  # DriveFile of the selected patient folder
    "search_query": "",
    "matches": [],  # pick-list after an ambiguous search
    "view": None,  # panel on the patient screen: download | open | plan
    "last_plan": None,  # (plan kind key, markdown, generated) of the plan just requested; generated=False if reused
    "cache": {},  # patient id -> data already fetched from Drive this session
    "flash": None,  # one-off success message
    "form_nonce": 0,  # bumped to clear upload/text widgets
    "camera_shots": [],  # [(sha1, Attachment)] captured with the camera
    "medicine_list": None,  # (number, markdown, generated) of the medicine list shown on the medicine screen
    "chat_open": False,  # records chat panel on the search screen
    "chat_animate": False,  # play the slide-up animation on this run only (the panel was just opened)
    "chat_turns": [],  # [{"question", "answer", "sources", "chars"}]
    "chat_patients": [],  # patients the last question was about; a follow-up that names nobody stays on them
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
    records_index().mark_stale()


@st.cache_resource
def _records_index() -> chat.Index:
    return chat.Index()


def records_index() -> chat.Index:
    """Search index for the records chat, kept for the life of the app process."""
    index = _records_index()
    if not isinstance(index, chat.Index):  # chat.py was edited while the app was running
        _records_index.clear()
        index = _records_index()
    return index


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
            "nutrition": current_plan_file(NUTRITION, f.plans),
            "exercise": current_plan_file(EXERCISE, f.exercise_plans),
            "medicine": client().list_plans(f.medicine, drive.MEDICINE_LIST_PREFIX),
        }
    return cache["listing"]


def current_plan_file(kind: PlanKind, plans_id: str) -> DriveFile | None:
    """The saved plan, or the newest numbered plan from an earlier app version."""
    found = client().find_child(plans_id, kind.file, folder=False)
    if found:
        return found
    legacy = client().list_plans(plans_id, kind.legacy_prefix)
    return legacy[-1][1] if legacy else None


def case_changed_since(f: PatientFolders, output: DriveFile) -> bool:
    """True when Info.md, Extra_info.md or a photo was modified after `output` was saved. Uses Drive metadata only."""
    docs = [x for x in client().list_children(f.documents, folders=False) if x.name in (drive.INFO_FILE, drive.EXTRA_INFO_FILE)]
    return any(x.modified > output.modified for x in docs + client().list_children(f.photos, folders=False))


def download_attachment(file: DriveFile) -> Attachment:
    return Attachment(file.name, file.mime_type, client().download_bytes(file.id, file.name))


def pdf_name(md_name: str) -> str:
    return md_name.removesuffix(".md") + ".pdf"


@st.cache_data(max_entries=200, show_spinner=False)
def markdown_pdf(text: str, title: str) -> bytes:
    return pdf.markdown_to_pdf(text, title)


def as_download(name: str, data: bytes, mime_type: str) -> tuple[str, bytes, str]:
    """(file name, bytes, mime type) offered for download: Markdown files are converted to PDF."""
    if not name.endswith(".md"):
        return name, data, mime_type
    title = f"{st.session_state.patient.name}: {name.removesuffix('.md')}"
    return pdf_name(name), markdown_pdf(data.decode("utf-8"), title), "application/pdf"


def pdf_download_button(md_name: str, text: str, **kwargs) -> None:
    """Download button for a Markdown document, delivered as PDF."""
    name, data, mime = as_download(md_name, text.encode("utf-8"), "text/markdown")
    st.download_button(f"Download {name}", data, file_name=name, mime=mime, on_click="ignore", icon=":material/download:", **kwargs)


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


def compose_plan(kind: PlanKind, name: str, body: str) -> str:
    return (
        f"# {kind.title}: {name}\n\n"
        f"_Generated {timestamp()} by {ai.provider_label()}. AI draft for clinician review._\n\n{body}\n"
    )


def compose_medicine_list(number: int, name: str, body: str, doctor_input: str, revised_from: int | None) -> str:
    source = f", revised from {medicine_file(revised_from)} using the doctor's input" if revised_from else ""
    text = (
        f"# {MEDICINE_TITLE} {number}: {name}\n\n"
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


def save_next_medicine_list(f: PatientFolders, compose, log) -> tuple[int, str]:
    """Save compose(number) as the next MedicineListN.md; returns (number, text)."""
    lists = client().list_plans(f.medicine, drive.MEDICINE_LIST_PREFIX)  # listed just before saving in case one was added meanwhile
    number = lists[-1][0] + 1 if lists else 1
    text = compose(number)
    log(f"Saving {medicine_file(number)} to Drive…")
    client().save_new_plan(f.medicine, number, text, drive.MEDICINE_LIST_PREFIX)
    return number, text


def request_plan(kind: PlanKind, force: bool, log) -> tuple[str, str, bool]:
    """(kind key, plan text, generated). Overwrites the plan file.

    Unless `force` is set, the saved plan is returned without calling the AI when nothing was added
    to the case study since it was saved.
    """
    f, name = folders(), st.session_state.patient.name
    plans_id = getattr(f, kind.folder)
    try:
        log("Checking the case study for changes…")
        saved = current_plan_file(kind, plans_id)
        previous = client().download_bytes(saved.id, saved.name).decode("utf-8") if saved else None
        if saved and not force and not case_changed_since(f, saved):
            return kind.key, previous, False

        info_md, extra_md, photos = read_case(f, log)
        log(f"Writing the {kind.title.lower()} with {ai.provider_label()}{' (updating the saved plan)' if previous else ''}…")
        body = ai.generate_plan(name, info_md, extra_md, previous, photos, kind=kind.key)
        text = compose_plan(kind, name, body)
        log(f"Saving {kind.file} to Drive…")
        client().write_text(plans_id, kind.file, text)
        return kind.key, text, True
    finally:
        invalidate_patient_cache()


def request_medicines(extra_input: str, force: bool, log) -> tuple[int, str, bool]:
    """(number, text, generated). Fresh recommendations saved as the next MedicineListN.md.

    Unless `force` is set or extra input is given, the latest list is returned without calling the AI
    when nothing was added to the case study since it was saved.
    """
    f, name = folders(), st.session_state.patient.name
    try:
        log("Checking the case study for changes…")
        lists = client().list_plans(f.medicine, drive.MEDICINE_LIST_PREFIX)
        if lists and not force and not extra_input.strip():
            number, latest = lists[-1]
            if not case_changed_since(f, latest):
                return number, client().download_bytes(latest.id, latest.name).decode("utf-8"), False

        info_md, extra_md, photos = read_case(f, log)
        log(f"Writing homeopathic medicine recommendations with {ai.provider_label()}…")
        body = ai.recommend_medicines(name, info_md, extra_md, extra_input, None, photos)
        number, text = save_next_medicine_list(f, lambda n: compose_medicine_list(n, name, body, extra_input, None), log)
        return number, text, True
    finally:
        invalidate_patient_cache()


def revise_medicines(doctor_input: str, current: tuple[int, str], log) -> tuple[int, str, bool]:
    """The current list revised with the doctor's recommendations, saved as the next MedicineListN.md."""
    f, name = folders(), st.session_state.patient.name
    try:
        info_md, extra_md, photos = read_case(f, log)
        log(f"Revising {medicine_file(current[0])} with your recommendations using {ai.provider_label()}…")
        body = ai.recommend_medicines(name, info_md, extra_md, doctor_input, current, photos)
        number, text = save_next_medicine_list(
            f, lambda n: compose_medicine_list(n, name, body, doctor_input, current[0]), log
        )
        return number, text, True
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
    records_index().mark_stale()
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


def load_styles() -> None:
    st.html(f"<style>{STYLE_FILE.read_text(encoding='utf-8')}</style>")


def hero(title: str, subtitle: str = "", eyebrow: str = "") -> None:
    """Large page heading with an optional pill label above and a muted line below."""
    parts = [f'<span class="np-eyebrow">{html.escape(eyebrow)}</span>' if eyebrow else "",
             f"<h1>{html.escape(title)}</h1>",
             f"<p>{html.escape(subtitle)}</p>" if subtitle else ""]
    st.html(f'<div class="np-hero">{"".join(parts)}</div>')


def back_button(label: str) -> bool:
    """Small arrow link at the top of a screen."""
    with st.container(key="topbar"):
        return st.button(label, icon=":material/arrow_back:", type="tertiary", key="back")


def welcome_card(subtitle: str) -> None:
    """Card with the app's tagline and decorative bars, shown before the doctor is signed in."""
    bars = "".join(f'<span class="{cls}" style="height:{height}%"></span>' for height, cls in WELCOME_BARS)
    st.html(
        '<div class="np-welcome"><div><span class="np-eyebrow">NutritionPlan</span>'
        f"<h1>Personal care plans for every patient</h1><p>{html.escape(subtitle)}</p></div>"
        f'<div class="np-bars" aria-hidden="true">{bars}</div></div>'
    )


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
    welcome_card("Case notes, diet and exercise plans and medicines, kept in your own Google Drive.")
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
        submitted = st.form_submit_button("Unlock", type="primary", icon=":material/lock_open:")
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
        st.markdown("### :material/eco: NutritionPlan")
        st.caption(f"AI: {ai.provider_label()}")
        if st.button("Lock app", icon=":material/lock:"):
            st.session_state.authenticated = False
            st.rerun()
        if st.session_state.drive is not None:
            st.caption("Signed in to Google Drive")
            # With GOOGLE_TOKEN_JSON the login comes from secrets, so there is nothing to log out of.
            if not config.GOOGLE_TOKEN_JSON and st.button("Log out", icon=":material/logout:"):
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
        welcome_card("Sign in with your Google account to open the patient case studies stored in your Google Drive.")
        if not st.button("Login with Google", type="primary", icon=":material/login:"):
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
    hero("Find a patient", "Search by name. If nobody matches, you can start a new case study.", eyebrow="Patients")
    with st.form("search"):
        query = st.text_input("Patient name", value=st.session_state.search_query, placeholder="e.g. Anita Sharma")
        submitted = st.form_submit_button("Search", type="primary", icon=":material/search:")

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
        with st.container(border=True, key="card-matches"):
            st.subheader("Did you mean…")
            choice = st.radio("Matching patients", matches, format_func=lambda p: p.name, index=None)
            with st.container(horizontal=True):
                if st.button("Open selected patient", type="primary", disabled=choice is None, icon=":material/folder_open:"):
                    open_patient(choice)
                if st.button(f"Create new patient “{st.session_state.search_query}”", icon=":material/person_add:"):
                    go("new", matches=[])


def screen_patient() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    if back_button("Back to search"):
        go("search", patient=None, view=None, last_plan=None)
    show_flash()
    hero(patient.name, "Patient case study", eyebrow="Patient")

    requested = None
    with st.container(key="tiles"):
        top, bottom = st.columns(2), st.columns(2)
        if top[0].button("Diet Plan", icon=":material/restaurant:", key="tile-diet", width="stretch"):
            requested = NUTRITION
        if top[1].button("Exercise Plan", icon=":material/directions_run:", key="tile-exercise", width="stretch"):
            requested = EXERCISE
        if bottom[0].button("Medicines", icon=":material/medication:", key="tile-medicine", width="stretch"):
            reset_form()
            go("medicine", view=None, medicine_list=None)
        if bottom[1].button("Update Case", icon=":material/edit_note:", key="tile-update", width="stretch"):
            reset_form()
            go("update")

    with st.container(horizontal=True):
        view = st.session_state.view
        if st.button("Open Case Study", icon=":material/menu_book:", type="primary" if view == "open" else "secondary"):
            st.session_state.view = "open"
            st.rerun()
        if st.button("Download Case files", icon=":material/download:", type="primary" if view == "download" else "secondary"):
            st.session_state.view = "download"
            st.rerun()
    st.divider()

    if requested:
        st.session_state.view = None
        run_plan_request(requested, force=False)

    view = st.session_state.view
    if view == "download":
        render_downloads()
    elif view == "open":
        render_case_study()
    elif view == "plan" and st.session_state.last_plan:
        render_new_plan()


def run_plan_request(kind: PlanKind, force: bool) -> bool:
    label = kind.title.lower()
    with guarded(f"preparing the {label}") as outcome, st.status(f"Preparing {label}…", expanded=True) as status:
        result = request_plan(kind, force, st.write)
        done = f"Saved {kind.file}" if result[2] else f"No changes since {kind.file} was saved"
        status.update(label=done, state="complete", expanded=False)
    if outcome.ok:
        st.session_state.last_plan = result
        st.session_state.view = "plan"
    return outcome.ok


def render_downloads() -> None:
    st.subheader("Case files")
    name = st.session_state.patient.name
    with guarded("loading case files") as outcome, st.spinner("Loading files from Drive…"):
        cache = patient_cache()
        if "tree" not in cache:
            cache["tree"] = client().walk(st.session_state.patient.id)
        files = cache["tree"]
        # Drive path -> (download path, file name, bytes, mime type); Markdown files are offered as PDF
        payload = {}
        for path, f in files:
            if not f.mime_type.startswith(GOOGLE_NATIVE_PREFIX):
                file_name, data, mime = as_download(f.name, file_bytes(f), f.mime_type)
                payload[path] = (path.removesuffix(f.name) + file_name, file_name, data, mime)
        if "zip" not in cache:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for download_path, _, data, _ in payload.values():
                    zf.writestr(f"{name}/{download_path}", data)
            cache["zip"] = buf.getvalue()
    if not outcome.ok:
        return
    if not files:
        st.info("This patient has no files yet.")
        return

    st.download_button("Download all as ZIP", cache["zip"], file_name=f"{name}.zip", mime="application/zip", type="primary", on_click="ignore", icon=":material/folder_zip:")
    for path, f in files:
        if path in payload:
            download_path, file_name, data, mime = payload[path]
            st.download_button(download_path, data, file_name=file_name, mime=mime, key=f"dl-{f.id}", on_click="ignore", icon=":material/description:")
        else:
            st.caption(f"{path} (Google Docs file, open it in Drive)")


def render_case_study() -> None:
    with guarded("loading the case study") as outcome, st.spinner("Loading case study…"):
        listing = case_listing()

        st.subheader("Case notes (Info.md)")
        with st.container(border=True, key="card-info"):
            st.markdown(file_bytes(listing["info"]).decode("utf-8") if listing["info"] else "_Info.md has not been created yet._")

        st.subheader("Extra observations (Extra_info.md)")
        with st.container(border=True, key="card-extra"):
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
        render_saved_plan(NUTRITION, listing["nutrition"], "Diet Plan")

        st.subheader("Exercise plan")
        render_saved_plan(EXERCISE, listing["exercise"], "Exercise Plan")

        st.subheader("Homeopathic medicines")
        render_medicine_lists(listing["medicine"])
    if not outcome.ok:
        st.caption("Try again, or go back to search.")


def render_saved_plan(kind: PlanKind, plan_file: DriveFile | None, button: str) -> None:
    if plan_file is None:
        st.info(f"Nothing saved yet. Use **{button}** to create one.")
        return
    text = file_bytes(plan_file).decode("utf-8")
    with st.container(border=True, key=f"card-plan-{kind.key}"):
        st.markdown(text)
    pdf_download_button(kind.file, text, key=f"dl-{kind.key}")


def render_medicine_lists(lists: list[tuple[int, DriveFile]]) -> None:
    if not lists:
        st.info("Nothing saved yet. Use **Medicines** to create one.")
        return
    by_number = dict(lists)
    numbers = sorted(by_number, reverse=True)
    number = st.selectbox(
        "Version", numbers, key="version-medicine",
        format_func=lambda n: medicine_file(n) + (" (latest)" if n == numbers[0] else ""),
    )
    list_file = by_number[number]
    text = file_bytes(list_file).decode("utf-8")
    with st.container(border=True, key="card-medicine"):
        st.markdown(text)
    pdf_download_button(list_file.name, text, key="dl-medicine")


def render_new_plan() -> None:
    key, text, generated = st.session_state.last_plan
    kind = PLAN_KINDS[key]
    if generated:
        st.success(f"{kind.file} saved to Drive.")
    else:
        st.info("Nothing was added to the case study since this plan was saved, so the saved plan is shown and the AI was not called.")
        if st.button("Regenerate anyway", key=f"regenerate-{key}", icon=":material/refresh:") and run_plan_request(kind, force=True):
            st.rerun()
    with st.container(border=True, key="card-new-plan"):
        st.markdown(text)
    pdf_download_button(kind.file, text, type="primary")


def screen_new() -> None:
    name = st.session_state.search_query
    nonce = st.session_state.form_nonce
    if back_button("Back to search"):
        reset_form()
        go("search", patient=None)
    hero(name, "No patient with this name yet. Add their notes to create a case study.", eyebrow="New case study")

    photos = photo_inputs()
    notes = st.text_area("Extra observations (text from doctor)", key=f"notes-{nonce}", height=160)

    if st.button("Create patient", type="primary", icon=":material/person_add:"):
        if not photos and not notes.strip():
            st.warning("Add at least one photo or some observations.")
            return

        with guarded("creating the patient") as outcome, st.status(f"Creating case study for {name}…", expanded=True) as status:
            create_patient(name, photos, notes, st.write)
            status.update(label="Case study created", state="complete", expanded=False)

        if outcome.ok:
            reset_form()
            go("patient", view=None, last_plan=None,
               flash=f"Created case study for {name}. Request a diet plan, exercise plan or medicines when you need them.")

    if st.session_state.patient is not None:
        # The folder exists even though a later step failed; let the doctor continue from the patient page.
        # Rendered on every run (not only right after the failure) so the click is seen on the next rerun.
        if st.button("Open patient page"):
            reset_form()
            open_patient(st.session_state.patient)


def screen_update() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    nonce = st.session_state.form_nonce
    if back_button("Back to patient"):
        reset_form()
        go("patient", view=None)
    hero(patient.name, "Add new photos or observations.", eyebrow="Update case study")

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

    if not st.button("Save", type="primary", icon=":material/save:"):
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
        go("patient", view=None, last_plan=None, flash="Case study updated. Use Diet Plan or Exercise Plan to generate new plans.")


def screen_medicine() -> None:
    patient = st.session_state.patient
    if patient is None:
        go("search")
    nonce = st.session_state.form_nonce
    if back_button("Back to patient"):
        reset_form()
        go("patient", view=None, medicine_list=None)
    hero(patient.name, "Homeopathic medicine recommendations", eyebrow="Medicines")

    st.subheader("1. Get recommendations")
    st.caption("Based on Info.md and Extra_info.md. If nothing was added since the last list and you leave the box "
               "below empty, the last list is shown without calling the AI.")
    extra = st.text_area(
        "Additional input for this request (optional)", key=f"medicine-extra-{nonce}", height=120,
        placeholder="For example: current symptoms, modalities, or remedies already tried.",
    )
    if st.button("Generate recommendations", type="primary", icon=":material/auto_awesome:"):
        run_medicine_request(extra, force=False)

    current = st.session_state.medicine_list
    if current is None:
        return
    number, text, generated = current
    if generated:
        st.success(f"{medicine_file(number)} saved to Drive.")
    else:
        st.info(f"Nothing was added to the case study since {medicine_file(number)} was saved, "
                "so it is shown and the AI was not called.")
        if st.button("Generate new recommendations anyway", icon=":material/refresh:") and run_medicine_request(extra, force=True):
            st.rerun()
    with st.container(border=True, key="card-medicine-list"):
        st.markdown(text)
    pdf_download_button(medicine_file(number), text)

    st.subheader("2. Your recommendations")
    st.caption(f"Describe the changes you want. A new list is created from {medicine_file(number)} and your input; "
               "the list above is kept.")
    doctor_input = st.text_area(
        "Doctor's recommendations", key=f"medicine-doctor-{nonce}-{number}", height=160,
        placeholder="For example: use Kali bichromicum 30C instead of Pulsatilla; add Belladonna 200C for acute fever.",
    )
    if st.button("Create revised list", type="primary", icon=":material/edit_note:"):
        if not doctor_input.strip():
            st.warning("Enter your recommendations first.")
            return
        with guarded("revising the medicine list") as outcome, st.status("Creating revised list…", expanded=True) as status:
            revised = revise_medicines(doctor_input, (number, text), st.write)
            status.update(label=f"Saved {medicine_file(revised[0])}", state="complete", expanded=False)
        if outcome.ok:
            st.session_state.medicine_list = revised
            st.rerun()


def run_medicine_request(extra_input: str, force: bool) -> bool:
    with guarded("preparing medicine recommendations") as outcome, st.status("Preparing recommendations…", expanded=True) as status:
        result = request_medicines(extra_input, force, st.write)
        done = f"Saved {medicine_file(result[0])}" if result[2] else f"No changes since {medicine_file(result[0])} was saved"
        status.update(label=done, state="complete", expanded=False)
    if outcome.ok:
        st.session_state.medicine_list = result
    return outcome.ok


# ================================================================ records chat (slide-up panel on the search screen)

CHAT_CSS = """
<style>
.st-key-chat-launcher, .st-key-chat-panel { position: fixed; right: 24px; z-index: 999990; }
.st-key-chat-launcher { bottom: 24px; width: auto !important; }
.st-key-chat-launcher button { border-radius: 999px; padding: 0.75rem 1.4rem; box-shadow: 0 10px 28px rgba(35, 34, 32, 0.3); }
.st-key-chat-panel {
  bottom: 0; width: min(460px, calc(100vw - 32px)) !important; padding: 0.75rem 1rem 1rem;
  background: %(background)s; border: 1px solid %(border)s; border-bottom: none;
  border-radius: 28px 28px 0 0; box-shadow: 0 -12px 36px rgba(35, 34, 32, 0.22);
  %(animation)s
}
.st-key-chat-messages { height: min(440px, 55vh) !important; }
@keyframes chat-slide-up { from { transform: translateY(100%%); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
@media (max-width: 640px) { .st-key-chat-launcher, .st-key-chat-panel { right: 16px; } }
</style>
"""


def records_chat() -> None:
    """Floating "Ask about patients" button that slides up a chat over every patient's records."""
    dark = st.context.theme.type == "dark"
    st.html(CHAT_CSS % {
        "background": "#0e1117" if dark else "#F4EEE8",
        "border": "rgba(250, 250, 250, 0.2)" if dark else "#D6C7B5",
        "animation": "animation: chat-slide-up 0.28s ease-out;" if st.session_state.chat_animate else "",
    })
    st.session_state.chat_animate = False

    if not st.session_state.chat_open:
        with st.container(key="chat-launcher"):
            if st.button("Ask about patients", type="primary", icon=":material/forum:"):
                st.session_state.chat_open = True
                st.session_state.chat_animate = True
                st.rerun()
        return

    with st.container(key="chat-panel"):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown("**:material/forum: Patient records assistant**", width="stretch")
            if st.button("New chat", key="chat-clear", disabled=not st.session_state.chat_turns):
                st.session_state.chat_turns = []
                st.session_state.chat_patients = []
                st.rerun()
            if st.button("", icon=":material/close:", key="chat-close", help="Close"):
                st.session_state.chat_open = False
                st.rerun()

        messages = st.container(key="chat-messages", height=440, autoscroll=True)
        with messages:
            if not st.session_state.chat_turns:
                st.caption("Ask about any patient (use their name), or about all records, for example "
                           "“Which patients were given Pulsatilla?” Search picks the relevant records; "
                           "only those are sent to the AI.")
            for turn in st.session_state.chat_turns:
                render_chat_turn(turn)
        question = st.chat_input("Ask about a patient or all records…", key="chat-input")

    if question and question.strip():
        with messages:
            st.chat_message("user").markdown(question)
            with st.chat_message("assistant"):
                if ask_records(question.strip()):
                    st.rerun()


def render_chat_turn(turn: dict) -> None:
    st.chat_message("user").markdown(turn["question"])
    with st.chat_message("assistant"):
        st.markdown(turn["answer"])
        with st.expander(f"Records used: {len(turn['sources'])} · about {turn['chars'] // 4:,} tokens"):
            for source in turn["sources"] or ["Only the patient list (search found no matching records)"]:
                st.caption(source)


def ask_records(question: str) -> bool:
    """Search the records locally, then send only the matches with the question to the AI."""
    progress = st.empty()
    history = [(t["question"], t["answer"]) for t in st.session_state.chat_turns]
    with guarded("answering the question") as outcome, st.spinner("Searching records…"):
        index = records_index()
        index.refresh(client(), st.session_state.root_id, progress.caption)
        context = index.build_context(question, st.session_state.chat_patients)
        progress.caption(f"Found {len(context.sources)} record(s). Asking {ai.provider_label()}…")
        answer = ai.answer_question(question, context.text, history)
    progress.empty()
    if outcome.ok:
        st.session_state.chat_turns.append(
            {"question": question, "answer": answer, "sources": context.sources, "chars": context.chars}
        )
        st.session_state.chat_patients = context.patients
    return outcome.ok


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
    load_styles()
    if not password_gate():
        return
    sidebar()
    screen = st.session_state.screen
    if st.session_state.drive is not None and not isinstance(st.session_state.drive, drive.Drive):
        # drive.py was edited while the app was running: Streamlit reloaded the module, but this session still
        # holds a client built from the old class. The login screen reconnects from the saved token.
        st.session_state.drive = None
    if st.session_state.drive is None:
        screen = "login"
    SCREENS[screen]()
    if screen == "search":
        records_chat()


main()

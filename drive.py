"""Google Drive helpers: OAuth login, find/create folders, upload, list, download.

Drive layout (names must match exactly):

    NutritionPlan/<PatientName>/Documents/Photos/
    NutritionPlan/<PatientName>/Documents/Info.md
    NutritionPlan/<PatientName>/Documents/Extra_info.md
    NutritionPlan/<PatientName>/Nutrition_Plan/NutritionPlan.md   (overwritten on each request)
    NutritionPlan/<PatientName>/Exercise_Plan/ExercisePlan.md     (overwritten on each request)
    NutritionPlan/<PatientName>/Homeopathic_Medicine/MedicineList1.md, MedicineList2.md, ...
"""
from __future__ import annotations

import io
import json
import re
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

import config

# Full Drive scope so the app also sees folders/files you create or move by hand.
SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
MARKDOWN_MIME = "text/markdown"

ROOT_FOLDER = "NutritionPlan"
DOCUMENTS = "Documents"
PHOTOS = "Photos"
PLANS = "Nutrition_Plan"
EXERCISE_PLANS = "Exercise_Plan"
MEDICINE = "Homeopathic_Medicine"
INFO_FILE = "Info.md"
EXTRA_INFO_FILE = "Extra_info.md"
NUTRITION_PLAN_FILE = "NutritionPlan.md"
EXERCISE_PLAN_FILE = "ExercisePlan.md"
# Numbered plans written by earlier versions of the app (PlanN.md / ExercisePlanN.md); read only as a fallback.
LEGACY_PLAN_PREFIX = "Plan"
LEGACY_EXERCISE_PLAN_PREFIX = "ExercisePlan"
MEDICINE_LIST_PREFIX = "MedicineList"  # Homeopathic_Medicine/MedicineListN.md
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
APPOINTMENT_SHEET = "Appoinment_history"  # Google Sheet in NutritionPlan/ (name spelled as in Drive)
VISIT_TAB_INDEX = 0  # first tab: PatientCode | PatientName | Date | Time
PATIENT_TAB_INDEX = 1  # second tab: PatientCode | PatientName
CODE_LETTERS = 3
CODE_DIGITS = 2

RESUMABLE_THRESHOLD = 5 * 1024 * 1024
LOGIN_TIMEOUT_SECONDS = 180
PARENT_BATCH = 40  # folder ids per "'id' in parents or …" query in walk_tree()
DOWNLOAD_WORKERS = 8  # parallel downloads in download_many()


class DriveError(Exception):
    """A Drive operation failed. The message is meant to be shown in the UI."""


# ---------------------------------------------------------------- auth


def load_cached_credentials() -> Credentials | None:
    """Return valid credentials from the cached token, refreshing if needed; None if a login is required."""
    if config.GOOGLE_TOKEN_JSON:
        return _credentials_from_secret()
    path = config.GOOGLE_TOKEN_FILE
    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, OSError):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except (RefreshError, TransportError):
            return None
        _save_token(creds)
        return creds
    return None


def _credentials_from_secret() -> Credentials:
    """Credentials from the GOOGLE_TOKEN_JSON setting. Refreshed in memory only; nothing is written to disk."""
    try:
        creds = Credentials.from_authorized_user_info(json.loads(config.GOOGLE_TOKEN_JSON), SCOPES)
    except (ValueError, KeyError) as e:
        raise DriveError("The GOOGLE_TOKEN_JSON secret is not valid token.json content.") from e
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise DriveError(
                "The Google login saved in GOOGLE_TOKEN_JSON has expired or was revoked. "
                "Sign in on your computer, then paste the new token.json into the secret."
            ) from e
        except TransportError as e:
            raise DriveError(f"Could not reach Google to refresh the login: {e}") from e
    return creds


class _LinkBrowser(webbrowser.BaseBrowser):
    """A 'browser' that hands the sign-in URL to a callback instead of launching one.

    Launching a browser fails silently in WSL and other headless setups, so the app
    shows the link itself.
    """

    def __init__(self, on_url: Callable[[str], None]):
        super().__init__(_LINK_BROWSER)
        self._on_url = on_url

    def open(self, url, new=0, autoraise=True):
        self._on_url(url)
        return True


_LINK_BROWSER = "nutritionplan-link"


def login(show_link: Callable[[str], None]) -> Credentials:
    """Run the OAuth flow and cache the token.

    show_link receives the Google sign-in URL; this call then blocks until the
    browser is redirected back to the temporary local server (or it times out).
    """
    if not config.GOOGLE_CREDENTIALS_FILE.exists():
        raise DriveError(
            f"OAuth client file not found at {config.GOOGLE_CREDENTIALS_FILE}. "
            "Download it from Google Cloud Console (see README → Google Drive setup). "
            "On Streamlit Community Cloud, set the GOOGLE_TOKEN_JSON secret instead."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS_FILE), SCOPES)
    webbrowser.register(_LINK_BROWSER, None, _LinkBrowser(show_link))
    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            browser=_LINK_BROWSER,
            timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            prompt="consent",  # always return a refresh token so later runs skip the login screen
            authorization_prompt_message="",
            success_message="Signed in. You can close this tab and return to NutritionPlan.",
        )
    except WSGITimeoutError as e:
        raise DriveError("Google sign-in timed out. Click Login and try again.") from e
    if creds is None or not creds.token:
        raise DriveError("Google sign-in did not complete (timed out or was cancelled). Try again.")
    _save_token(creds)
    return creds


def logout() -> None:
    config.GOOGLE_TOKEN_FILE.unlink(missing_ok=True)


def _save_token(creds: Credentials) -> None:
    config.GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    try:
        config.GOOGLE_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- data types


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified: str = ""  # RFC 3339 UTC modifiedTime from Drive; sorts chronologically as a string

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


@dataclass(frozen=True)
class PatientFolders:
    patient: DriveFile
    documents: str
    photos: str
    plans: str
    exercise_plans: str
    medicine: str


@dataclass(frozen=True)
class Visit:
    """One row of the visits tab in Appoinment_history."""
    code: str
    name: str
    day: date | None  # None when the cell isn't a date
    time: str  # HH:MM


@dataclass(frozen=True)
class PatientVisits:
    """A patient's visit count and latest visit, from the visits tab."""
    name: str
    visits: int
    last_day: date | None
    last_time: str  # HH:MM


def _quote(value: str) -> str:
    """Escape a value for use inside a single-quoted Drive query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _from_api(item: dict) -> DriveFile:
    return DriveFile(item["id"], item["name"], item["mimeType"], item.get("modifiedTime", ""))


def _tab_ref(title: str) -> str:
    """A sheet tab name quoted for A1 notation."""
    return "'" + title.replace("'", "''") + "'"


SHEETS_EPOCH = date(1899, 12, 30)  # day 0 of Google Sheets date serial numbers
TEXT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


def _cell_date(value) -> date | None:
    """A visit-sheet date cell: a serial number, or text typed by hand in a common format."""
    if isinstance(value, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(value))
    for fmt in TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _cell_time(value) -> str:
    """A visit-sheet time cell as HH:MM (serial numbers are fractions of a day)."""
    if isinstance(value, (int, float)):
        minutes = round((value % 1) * 24 * 60) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    return str(value).strip()


def next_patient_code(name: str, existing: list[str]) -> str:
    """First 3 letters of the name in capitals plus the lowest unused 2-digit number, e.g. 'Yuvraj' -> YUV01.

    Names with fewer than 3 letters are padded with X. Codes already in `existing` are never reused.
    """
    letters = "".join(c for c in name.upper() if "A" <= c <= "Z")[:CODE_LETTERS].ljust(CODE_LETTERS, "X")
    code_re = re.compile(rf"^{letters}(\d{{{CODE_DIGITS}}})$")
    used = {int(m.group(1)) for code in existing if (m := code_re.match(code.strip().upper()))}
    number = next((n for n in range(1, 10 ** CODE_DIGITS) if n not in used), None)
    if number is None:
        raise DriveError(f"All patient codes starting with {letters} are used ({letters}01–{letters}99).")
    return f"{letters}{number:0{CODE_DIGITS}d}"


# ---------------------------------------------------------------- client


class Drive:
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        # httplib2 connections are not thread-safe, and Streamlit can run a new rerun of the script while an
        # older one is still inside a Drive call. Concurrent use of one connection can crash the process.
        self._lock = threading.Lock()

    def _execute(self, request, action: str, *, locked: bool = True):
        """Run a request; `locked=False` only for requests built on a connection owned by the calling thread."""
        try:
            if not locked:
                return request.execute(num_retries=2)
            with self._lock:
                return request.execute(num_retries=2)
        except HttpError as e:
            raise DriveError(f"Google Drive error while {action}: {e.resp.status} {e.reason}") from e
        except RefreshError as e:
            raise DriveError("Your Google session expired or was revoked. Log out and log in again.") from e
        except (TransportError, httplib2.HttpLib2Error, OSError) as e:
            raise DriveError(f"Could not reach Google Drive while {action}: {e}") from e

    # --- listing / lookup

    def list_children(self, parent_id: str, *, folders: bool | None = None) -> list[DriveFile]:
        """Non-trashed children of a folder, sorted by name. folders=True/False filters by type."""
        query = f"'{_quote(parent_id)}' in parents and trashed = false"
        if folders is True:
            query += f" and mimeType = '{FOLDER_MIME}'"
        elif folders is False:
            query += f" and mimeType != '{FOLDER_MIME}'"
        return [_from_api(f) for f in self._list_all(query, "id, name, mimeType, modifiedTime", order_by="name")]

    def _list_all(self, query: str, file_fields: str, order_by: str | None = None) -> list[dict]:
        """Raw file resources matching a query, following every result page."""
        items: list[dict] = []
        page_token = None
        while True:
            resp = self._execute(
                self._svc.files().list(
                    q=query,
                    spaces="drive",
                    fields=f"nextPageToken, files({file_fields})",
                    orderBy=order_by,
                    pageSize=1000,
                    pageToken=page_token,
                ),
                "listing files",
            )
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    def walk_tree(self, folder_id: str) -> list[tuple[str, DriveFile]]:
        """All files under a folder as (relative path, file) pairs, sorted by path.

        Same result as walk(), but lists one folder level at a time with batched queries, so the whole
        NutritionPlan/ tree takes a handful of requests instead of one per folder.
        """
        out: list[tuple[str, DriveFile]] = []
        level = {folder_id: ""}  # folder id -> path prefix
        while level:
            next_level: dict[str, str] = {}
            ids = list(level)
            for i in range(0, len(ids), PARENT_BATCH):
                parents = " or ".join(f"'{_quote(x)}' in parents" for x in ids[i:i + PARENT_BATCH])
                for item in self._list_all(f"({parents}) and trashed = false", "id, name, mimeType, modifiedTime, parents"):
                    parent = next((p for p in item.get("parents", []) if p in level), None)
                    if parent is None:
                        continue
                    f = _from_api(item)
                    if f.is_folder:
                        next_level[f.id] = level[parent] + f.name + "/"
                    else:
                        out.append((level[parent] + f.name, f))
            level = next_level
        return sorted(out, key=lambda x: x[0])

    def find_child(self, parent_id: str, name: str, *, folder: bool) -> DriveFile | None:
        """Child with exactly this name (case-sensitive), or None."""
        op = "=" if folder else "!="
        query = (
            f"'{_quote(parent_id)}' in parents and name = '{_quote(name)}' "
            f"and trashed = false and mimeType {op} '{FOLDER_MIME}'"
        )
        resp = self._execute(
            self._svc.files().list(q=query, spaces="drive", fields="files(id, name, mimeType, modifiedTime)", pageSize=10),
            f"looking up '{name}'",
        )
        return next((_from_api(f) for f in resp.get("files", []) if f["name"] == name), None)

    def walk(self, folder_id: str, prefix: str = "") -> list[tuple[str, DriveFile]]:
        """All files under a folder as (relative path, file) pairs."""
        out: list[tuple[str, DriveFile]] = []
        for child in self.list_children(folder_id):
            path = prefix + child.name
            if child.is_folder:
                out.extend(self.walk(child.id, path + "/"))
            else:
                out.append((path, child))
        return out

    # --- folders

    def create_folder(self, parent_id: str, name: str) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self._execute(self._svc.files().create(body=body, fields="id"), f"creating folder '{name}'")["id"]

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        found = self.find_child(parent_id, name, folder=True)
        return found.id if found else self.create_folder(parent_id, name)

    def ensure_root(self) -> str:
        """Id of NutritionPlan/ in My Drive, created on first run."""
        return self.get_or_create_folder("root", ROOT_FOLDER)

    # --- patients

    def search_patients(self, root_id: str, query: str) -> tuple[list[DriveFile], list[DriveFile]]:
        """Case-insensitive search: (exact name matches, other folders containing the query)."""
        needle = query.strip().casefold()
        patients = self.list_children(root_id, folders=True)
        exact = [p for p in patients if p.name.strip().casefold() == needle]
        partial = [p for p in patients if needle in p.name.casefold() and p not in exact]
        return exact, partial

    def create_patient(self, root_id: str, name: str) -> PatientFolders:
        patient_id = self.create_folder(root_id, name)
        return self.patient_folders(DriveFile(patient_id, name, FOLDER_MIME))

    def patient_folders(self, patient: DriveFile) -> PatientFolders:
        """Folder ids for a patient, creating any missing subfolder."""
        documents = self.get_or_create_folder(patient.id, DOCUMENTS)
        photos = self.get_or_create_folder(documents, PHOTOS)
        plans = self.get_or_create_folder(patient.id, PLANS)
        exercise_plans = self.get_or_create_folder(patient.id, EXERCISE_PLANS)
        medicine = self.get_or_create_folder(patient.id, MEDICINE)
        return PatientFolders(patient, documents, photos, plans, exercise_plans, medicine)

    # --- appointment sheet

    def _appointment_tabs(self, root_id: str) -> tuple[str, list[str]]:
        """(spreadsheet id, tab titles in order) of Appoinment_history."""
        sheet = self.find_child(root_id, APPOINTMENT_SHEET, folder=False)
        if sheet is None or sheet.mime_type != SPREADSHEET_MIME:
            raise DriveError(f"The Google Sheet '{APPOINTMENT_SHEET}' was not found in {ROOT_FOLDER}/.")
        props = self._execute(
            self._sheets.spreadsheets().get(spreadsheetId=sheet.id, fields="sheets.properties(title,index)"),
            f"reading '{APPOINTMENT_SHEET}'",
        )
        tabs = sorted(props.get("sheets", []), key=lambda s: s["properties"].get("index", 0))
        titles = [s["properties"]["title"] for s in tabs]
        if len(titles) <= max(VISIT_TAB_INDEX, PATIENT_TAB_INDEX):
            raise DriveError(f"'{APPOINTMENT_SHEET}' needs two sheets: visits first, patient codes second.")
        return sheet.id, titles

    def _read_rows(self, sheet_id: str, tab: str, cells: str, *, raw: bool = False) -> list[list]:
        """Cell values as shown in the sheet; raw=True gives dates and times as serial numbers instead."""
        options = {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"} if raw else {}
        resp = self._execute(
            self._sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"{_tab_ref(tab)}!{cells}", **options),
            f"reading '{APPOINTMENT_SHEET}'",
        )
        return resp.get("values", [])

    def _append_row(self, sheet_id: str, tab: str, row: list[str], *, parse: bool) -> None:
        """Append below the last row. parse=True lets Sheets read dates and times as real values."""
        self._execute(
            self._sheets.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"{_tab_ref(tab)}!A:{chr(ord('A') + len(row) - 1)}",
                valueInputOption="USER_ENTERED" if parse else "RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ),
            f"adding a row to '{APPOINTMENT_SHEET}'",
        )

    def register_patient_code(self, root_id: str, name: str) -> str:
        """Give a new patient a code and append (code, name) to the second tab of Appoinment_history."""
        sheet_id, tabs = self._appointment_tabs(root_id)
        return self._register_code(sheet_id, tabs[PATIENT_TAB_INDEX], name)

    def _register_code(self, sheet_id: str, tab: str, name: str) -> str:
        code = next_patient_code(name, [row[0] for row in self._read_rows(sheet_id, tab, "A2:A") if row])
        self._append_row(sheet_id, tab, [code, name], parse=False)
        return code

    def _find_code(self, sheet_id: str, tab: str, name: str) -> str | None:
        """The patient's code from the second tab, matched by name (case-insensitive)."""
        needle = name.strip().casefold()
        return next(
            (row[0].strip() for row in self._read_rows(sheet_id, tab, "A2:B")
             if len(row) >= 2 and row[0].strip() and row[1].strip().casefold() == needle),
            None,
        )

    def patient_visits(self, root_id: str, name: str) -> tuple[str | None, list[Visit]]:
        """(patient code, visits with that code in the first tab, newest first). No code means no visits."""
        sheet_id, tabs = self._appointment_tabs(root_id)
        code = self._find_code(sheet_id, tabs[PATIENT_TAB_INDEX], name)
        if code is None:
            return None, []
        visits = [
            Visit(str(row[0]).strip(), str(row[1]).strip() if len(row) > 1 else "",
                  _cell_date(row[2]) if len(row) > 2 else None, _cell_time(row[3]) if len(row) > 3 else "")
            for row in self._read_rows(sheet_id, tabs[VISIT_TAB_INDEX], "A2:D", raw=True)
            if row and str(row[0]).strip().upper() == code.upper()
        ]
        visits.sort(key=lambda v: (v.day or date.min, v.time), reverse=True)
        return code, visits

    def visit_counts(self, root_id: str) -> list[PatientVisits]:
        """Visits in the first tab grouped by patient name ignoring case; most visits first."""
        sheet_id, tabs = self._appointment_tabs(root_id)
        groups: dict[str, PatientVisits] = {}  # casefolded name -> summary (name as first written)
        for row in self._read_rows(sheet_id, tabs[VISIT_TAB_INDEX], "A2:D", raw=True):
            name = str(row[1]).strip() if len(row) > 1 else ""
            if not name:
                continue
            day = _cell_date(row[2]) if len(row) > 2 else None
            time = _cell_time(row[3]) if len(row) > 3 else ""
            current = groups.get(name.casefold()) or PatientVisits(name, 0, None, "")
            later = day is not None and (current.last_day is None or (day, time) > (current.last_day, current.last_time))
            groups[name.casefold()] = PatientVisits(
                current.name, current.visits + 1,
                day if later else current.last_day, time if later else current.last_time,
            )
        return sorted(groups.values(), key=lambda g: (-g.visits, g.name.casefold()))

    def visit_times(self, root_id: str, name: str, day: date) -> list[str]:
        """Times (HH:MM) of the visits already recorded for this patient on this day, in sheet order."""
        sheet_id, tabs = self._appointment_tabs(root_id)
        needle = name.strip().casefold()
        return [
            _cell_time(row[3]) if len(row) > 3 else ""
            for row in self._read_rows(sheet_id, tabs[VISIT_TAB_INDEX], "A2:D", raw=True)
            if len(row) > 2 and str(row[1]).strip().casefold() == needle and _cell_date(row[2]) == day
        ]

    def record_visit(self, root_id: str, name: str, when: datetime) -> str:
        """Append (code, name, date, time) to the first tab; returns the patient code.

        Patients created before codes existed get one here, so every visit row has a code.
        """
        sheet_id, tabs = self._appointment_tabs(root_id)
        code = self._find_code(sheet_id, tabs[PATIENT_TAB_INDEX], name) or self._register_code(sheet_id, tabs[PATIENT_TAB_INDEX], name)
        self._append_row(
            sheet_id, tabs[VISIT_TAB_INDEX],
            [code, name, when.strftime("%Y-%m-%d"), when.strftime("%H:%M")], parse=True,
        )
        return code

    # --- files

    def upload_bytes(self, parent_id: str, name: str, data: bytes, mime_type: str) -> str:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=len(data) > RESUMABLE_THRESHOLD)
        body = {"name": name, "parents": [parent_id]}
        return self._execute(self._svc.files().create(body=body, media_body=media, fields="id"), f"uploading '{name}'")["id"]

    def download_bytes(self, file_id: str, name: str = "file") -> bytes:
        return self._execute(self._svc.files().get_media(fileId=file_id), f"downloading '{name}'")

    def download_many(self, files: list[DriveFile]) -> dict[str, bytes]:
        """Contents by file id, downloaded in parallel. Each worker thread gets its own connection."""
        local = threading.local()

        def fetch(file: DriveFile) -> tuple[str, bytes]:
            if not hasattr(local, "svc"):
                local.svc = build("drive", "v3", credentials=self._creds, cache_discovery=False)
            request = local.svc.files().get_media(fileId=file.id)
            return file.id, self._execute(request, f"downloading '{file.name}'", locked=False)

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            return dict(pool.map(fetch, files))

    def read_text(self, parent_id: str, name: str) -> str | None:
        found = self.find_child(parent_id, name, folder=False)
        return self.download_bytes(found.id, name).decode("utf-8") if found else None

    def write_text(self, parent_id: str, name: str, text: str) -> None:
        """Create a Markdown file, or replace the contents of the existing one."""
        data = text.encode("utf-8")
        existing = self.find_child(parent_id, name, folder=False)
        if existing is None:
            self.upload_bytes(parent_id, name, data, MARKDOWN_MIME)
            return
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=MARKDOWN_MIME, resumable=False)
        self._execute(self._svc.files().update(fileId=existing.id, media_body=media), f"updating '{name}'")

    def trash(self, file: DriveFile) -> None:
        """Move to Drive trash (recoverable for 30 days)."""
        self._execute(self._svc.files().update(fileId=file.id, body={"trashed": True}), f"deleting '{file.name}'")

    # --- plans

    def list_plans(self, plans_id: str, prefix: str) -> list[tuple[int, DriveFile]]:
        """<prefix>N.md files (e.g. PlanN.md) as (N, file), oldest first."""
        name_re = re.compile(rf"^{re.escape(prefix)}(\d+)\.md$")
        plans = []
        for f in self.list_children(plans_id, folders=False):
            match = name_re.match(f.name)
            if match:
                plans.append((int(match.group(1)), f))
        return sorted(plans, key=lambda p: p[0])

    def save_new_plan(self, plans_id: str, number: int, text: str, prefix: str) -> str:
        """Save <prefix>N.md. Refuses to overwrite: existing plans are never modified."""
        name = f"{prefix}{number}.md"
        if self.find_child(plans_id, name, folder=False):
            raise DriveError(f"{name} already exists; not overwriting it. Request the plan again.")
        self.upload_bytes(plans_id, name, text.encode("utf-8"), MARKDOWN_MIME)
        return name

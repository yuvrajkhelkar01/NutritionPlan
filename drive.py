"""Google Drive helpers: OAuth login, find/create folders, upload, list, download.

Drive layout (names must match exactly):

    NutritionPlan/<PatientName>/Documents/Photos/
    NutritionPlan/<PatientName>/Documents/Info.md
    NutritionPlan/<PatientName>/Documents/Extra_info.md
    NutritionPlan/<PatientName>/Nutrition_Plan/Plan1.md, Plan2.md, ...
    NutritionPlan/<PatientName>/Exercise_Plan/ExercisePlan1.md, ExercisePlan2.md, ...
    NutritionPlan/<PatientName>/Homeopathic_Medicine/MedicineList1.md, MedicineList2.md, ...
"""
from __future__ import annotations

import io
import json
import re
import webbrowser
from dataclasses import dataclass
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
PLAN_PREFIX = "Plan"  # Nutrition_Plan/PlanN.md
EXERCISE_PLAN_PREFIX = "ExercisePlan"  # Exercise_Plan/ExercisePlanN.md
MEDICINE_LIST_PREFIX = "MedicineList"  # Homeopathic_Medicine/MedicineListN.md

RESUMABLE_THRESHOLD = 5 * 1024 * 1024
LOGIN_TIMEOUT_SECONDS = 180


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


def _quote(value: str) -> str:
    """Escape a value for use inside a single-quoted Drive query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _from_api(item: dict) -> DriveFile:
    return DriveFile(item["id"], item["name"], item["mimeType"])


# ---------------------------------------------------------------- client


class Drive:
    def __init__(self, creds: Credentials):
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _execute(self, request, action: str):
        try:
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
        items: list[DriveFile] = []
        page_token = None
        while True:
            resp = self._execute(
                self._svc.files().list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType)",
                    orderBy="name",
                    pageSize=1000,
                    pageToken=page_token,
                ),
                "listing files",
            )
            items.extend(_from_api(f) for f in resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    def find_child(self, parent_id: str, name: str, *, folder: bool) -> DriveFile | None:
        """Child with exactly this name (case-sensitive), or None."""
        op = "=" if folder else "!="
        query = (
            f"'{_quote(parent_id)}' in parents and name = '{_quote(name)}' "
            f"and trashed = false and mimeType {op} '{FOLDER_MIME}'"
        )
        resp = self._execute(
            self._svc.files().list(q=query, spaces="drive", fields="files(id, name, mimeType)", pageSize=10),
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

    # --- files

    def upload_bytes(self, parent_id: str, name: str, data: bytes, mime_type: str) -> str:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=len(data) > RESUMABLE_THRESHOLD)
        body = {"name": name, "parents": [parent_id]}
        return self._execute(self._svc.files().create(body=body, media_body=media, fields="id"), f"uploading '{name}'")["id"]

    def download_bytes(self, file_id: str, name: str = "file") -> bytes:
        return self._execute(self._svc.files().get_media(fileId=file_id), f"downloading '{name}'")

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

    def list_plans(self, plans_id: str, prefix: str = PLAN_PREFIX) -> list[tuple[int, DriveFile]]:
        """<prefix>N.md files (e.g. PlanN.md) as (N, file), oldest first."""
        name_re = re.compile(rf"^{re.escape(prefix)}(\d+)\.md$")
        plans = []
        for f in self.list_children(plans_id, folders=False):
            match = name_re.match(f.name)
            if match:
                plans.append((int(match.group(1)), f))
        return sorted(plans, key=lambda p: p[0])

    def save_new_plan(self, plans_id: str, number: int, text: str, prefix: str = PLAN_PREFIX) -> str:
        """Save <prefix>N.md. Refuses to overwrite: existing plans are never modified."""
        name = f"{prefix}{number}.md"
        if self.find_child(plans_id, name, folder=False):
            raise DriveError(f"{name} already exists; not overwriting it. Request the plan again.")
        self.upload_bytes(plans_id, name, text.encode("utf-8"), MARKDOWN_MIME)
        return name

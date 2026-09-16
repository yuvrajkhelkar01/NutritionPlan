"""Patient-records search for the chat assistant.

Finding the relevant records is done here with plain search, not by the AI: every Markdown/text file under
NutritionPlan/ is indexed in memory (split into sections, ranked with BM25), and patients named in a question
are matched against the patient folder names. Only the selected passages are sent to the AI with the question.

The index lists Drive metadata with a few batched queries and downloads a file only when it is new or its
modifiedTime changed, so refreshing it is cheap after the first build. Photos are not indexed: their content is
already transcribed in Info.md.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable

import drive
from drive import DriveFile

TEXT_EXTENSIONS = (".md", ".txt")
REFRESH_SECONDS = 60  # Drive is listed again at most this often, unless the index is marked stale

CONTEXT_CHAR_BUDGET = 48_000  # ≈ 12k tokens of records per question
MAX_FULL_PATIENTS = 3  # up to this many named patients get their documents in full; more get excerpts
MAX_EXCERPTS = 20
CHUNK_CHARS = 1_500
MAX_ROSTER = 400
FOCUS_BOOST = 2.0  # score multiplier for excerpts from patients named in the question
SUPERSEDED_WEIGHT = 0.5  # score multiplier for older versions of a plan or medicine list
# Older versions are only searched when the question asks about the past.
HISTORY_WORDS = frozenset("previous previously earlier before old older past history historical change changed changes "
                          "version versions progress initial original".split())
NAME_FUZZ = 0.85  # SequenceMatcher ratio for a misspelt name word

BM25_K1 = 1.5
BM25_B = 0.75

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but
by can could did do does doing down during each few for from further had has have having he her here hers him
his how i if in into is it its just me more most my no nor not now of off on once only or other our out over own
please same she should so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your
tell show give list find patient patients doctor md
""".split())

# (file name pattern, label shown to the doctor, extra search words, order when a patient's documents are sent in full,
#  family: files of one family are versions of the same document; only the newest is current)
DOC_KINDS = [
    (re.compile(r"^Info\.md$"), "Case notes", "case notes history symptoms diagnosis transcription", 0, None),
    (re.compile(r"^Extra_info\.md$"), "Extra observations", "observations notes doctor", 1, None),
    (re.compile(r"^NutritionPlan\.md$"), "Nutrition plan", "nutrition diet food meal plan", 2, "nutrition"),
    (re.compile(r"^Plan(\d+)\.md$"), "Nutrition plan", "nutrition diet food meal plan", 2, "nutrition"),
    (re.compile(r"^ExercisePlan\.md$"), "Exercise plan", "exercise workout activity plan", 3, "exercise"),
    (re.compile(r"^ExercisePlan(\d+)\.md$"), "Exercise plan", "exercise workout activity plan", 3, "exercise"),
    (re.compile(r"^MedicineList(\d+)\.md$"), "Homeopathic medicines", "homeopathic medicine remedy potency", 4, "medicine"),
]
OTHER_ORDER = 5

_WORD_RE = re.compile(r"[^\W_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in _WORD_RE.findall(text.casefold()) if w not in STOPWORDS]


_SUFFIXES = ("ations", "ation", "ically", "ical", "ities", "ity", "ions", "ion", "ives", "ive", "ness", "ment",
             "ics", "ic", "ies", "ing", "ed", "es", "ly", "al", "s")
MIN_STEM = 4


def _stem(word: str) -> str:
    """Light suffix stripping, so 'diabetic'/'diabetes' and 'medicine'/'medicines' meet."""
    if word.isdigit():
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= (3 if suffix == "s" else MIN_STEM) and not word.endswith("ss"):
            word = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    return word[:-1] if len(word) > MIN_STEM and word.endswith("e") else word


# ---------------------------------------------------------------- data


@dataclass
class Chunk:
    doc: "Doc"
    heading: str
    text: str
    terms: Counter = field(default_factory=Counter)
    length: int = 0


@dataclass
class Doc:
    path: str  # relative to NutritionPlan/, e.g. "Asha Rao/Documents/Info.md"
    file: DriveFile
    text: str
    patient: str  # first path segment; "" for files directly in NutritionPlan/
    label: str
    order: int
    version: int  # N in MedicineListN.md / PlanN.md / ExercisePlanN.md, else 0
    family: str | None
    chunks: list[Chunk] = field(default_factory=list)
    superseded: bool = False  # a newer version of the same document exists

    @property
    def title(self) -> str:
        if not self.patient:
            return self.path
        version = f" version {self.version}" if self.version else ""
        older = ", older version" if self.superseded else ""
        return f"{self.patient} › {self.label}{version}{older} — {self.path}"


@dataclass
class Context:
    """What is sent to the AI for one question."""
    text: str
    sources: list[str]  # "title (full document | excerpts)" lines shown under the answer
    patients: list[str]  # patients named in (or carried over to) this question
    chars: int


def _describe(path: str, file: DriveFile, text: str) -> Doc:
    segments = path.split("/")
    patient = segments[0] if len(segments) > 1 else ""
    label, extra, order, version, family = file.name, "", OTHER_ORDER, 0, None
    for pattern, kind_label, kind_words, kind_order, kind_family in DOC_KINDS:
        match = pattern.match(file.name)
        if match:
            label, extra, order, family = kind_label, kind_words, kind_order, kind_family
            version = int(match.group(1)) if match.groups() else 0
            break
    doc = Doc(path, file, text, patient, label, order, version, family)
    # Folder and file names (CamelCase split) are searchable too. The patient name is left out: it would match
    # every file of that patient, and patients named in a question are found by find_patients() instead.
    path_words = " ".join(_CAMEL_RE.sub(" ", s) for s in segments[1 if patient else 0:]) + " " + extra
    doc.chunks = [
        Chunk(doc, heading, body, Counter(tokenize(f"{path_words} {heading} {body}")))
        for heading, body in _split(text)
    ]
    for chunk in doc.chunks:
        chunk.length = sum(chunk.terms.values())
    return doc


def _split(text: str) -> list[tuple[str, str]]:
    """(heading trail, text) sections of at most CHUNK_CHARS, split at Markdown headings, then paragraphs."""
    sections: list[tuple[str, list[str]]] = []
    trail: list[str] = []
    lines: list[str] = []

    def flush():
        if "".join(lines).strip():
            sections.append((" › ".join(trail), lines.copy()))
        lines.clear()

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            trail[:] = trail[: level - 1] + [heading.group(2).strip()]
        lines.append(line)
    flush()

    out: list[tuple[str, str]] = []
    for heading, section_lines in sections:
        piece = ""
        for paragraph in "\n".join(section_lines).split("\n\n"):
            while len(paragraph) > CHUNK_CHARS:  # a single huge paragraph (e.g. a long table)
                cut = paragraph.rfind("\n", 0, CHUNK_CHARS)
                cut = cut if cut > 0 else CHUNK_CHARS
                if piece.strip():
                    out.append((heading, piece.strip()))
                    piece = ""
                out.append((heading, paragraph[:cut].strip()))
                paragraph = paragraph[cut:]
            if piece and len(piece) + len(paragraph) > CHUNK_CHARS:
                out.append((heading, piece.strip()))
                piece = ""
            piece += paragraph + "\n\n"
        if piece.strip():
            out.append((heading, piece.strip()))
    return out


# ---------------------------------------------------------------- index


class Index:
    """In-memory search index over all text files in NutritionPlan/. Shared by all sessions of the app."""

    def __init__(self):
        self._lock = threading.Lock()
        self.docs: dict[str, Doc] = {}  # file id -> Doc
        self.patient_names: list[str] = []
        self.patient_updated: dict[str, str] = {}  # patient -> latest modifiedTime of any of their files
        self._chunks: list[Chunk] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}  # term -> [(chunk position, term count)]
        self._avg_len = 0.0
        self._refreshed_at = 0.0

    def mark_stale(self) -> None:
        """Call after the app writes to Drive so the next question re-lists it."""
        self._refreshed_at = 0.0

    def refresh(self, client: drive.Drive, root_id: str, log: Callable[[str], None], force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._refreshed_at < REFRESH_SECONDS:
                return
            log("Checking Drive for new or changed files…")
            tree = client.walk_tree(root_id)
            patients = sorted({p.split("/")[0] for p, f in tree if "/" in p}, key=str.casefold)
            patients += [f.name for f in client.list_children(root_id, folders=True) if f.name not in patients]

            wanted = {f.id: (path, f) for path, f in tree if f.name.casefold().endswith(TEXT_EXTENSIONS)}
            changed = [
                (path, f) for fid, (path, f) in wanted.items()
                if fid not in self.docs or self.docs[fid].file.modified != f.modified or self.docs[fid].path != path
            ]
            if changed:
                log(f"Reading {len(changed)} new or changed file(s)…")
                contents = client.download_many([f for _, f in changed])
                for path, f in changed:
                    self.docs[f.id] = _describe(path, f, contents[f.id].decode("utf-8", errors="replace"))
            for fid in set(self.docs) - set(wanted):
                del self.docs[fid]

            self.patient_names = sorted(set(patients), key=str.casefold)
            self.patient_updated = {}
            for path, f in tree:
                if "/" in path:
                    name = path.split("/")[0]
                    self.patient_updated[name] = max(self.patient_updated.get(name, ""), f.modified)
            self._rebuild()
            self._refreshed_at = time.time()

    def _rebuild(self) -> None:
        newest: dict[tuple[str, str], Doc] = {}
        for doc in self.docs.values():
            if doc.family:
                key = (doc.patient, doc.family)
                # The single overwritten file (version 0) is newer than any numbered file from earlier app versions.
                if key not in newest or (doc.version == 0, doc.version) > (newest[key].version == 0, newest[key].version):
                    newest[key] = doc
        for doc in self.docs.values():
            doc.superseded = bool(doc.family) and newest[(doc.patient, doc.family)] is not doc

        self._chunks = [c for d in sorted(self.docs.values(), key=lambda d: d.path) for c in d.chunks]
        self._postings = {}
        for i, chunk in enumerate(self._chunks):
            for term, tf in chunk.terms.items():
                self._postings.setdefault(term, []).append((i, tf))
        self._avg_len = (sum(c.length for c in self._chunks) / len(self._chunks)) if self._chunks else 0.0

    # --- search

    def search(self, query_terms: list[str]) -> dict[int, float]:
        """BM25 score per chunk position (only chunks that match at least one term)."""
        n = len(self._chunks)
        scores: dict[int, float] = {}
        for term in set(query_terms):
            postings = self._postings.get(term, [])
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, tf in postings:
                norm = BM25_K1 * (1 - BM25_B + BM25_B * self._chunks[i].length / (self._avg_len or 1))
                scores[i] = scores.get(i, 0.0) + idf * tf * (BM25_K1 + 1) / (tf + norm)
        return scores

    def find_patients(self, question: str) -> list[str]:
        """Patients named in the question: full name, or a name word that belongs to only one patient."""
        words = _WORD_RE.findall(question.casefold())
        folded = " ".join(words)
        found: list[str] = []
        word_owners: dict[str, set[str]] = {}
        for name in self.patient_names:
            name_words = _WORD_RE.findall(name.casefold())
            if name_words and f" {' '.join(name_words)} " in f" {folded} ":
                found.append(name)
            for w in name_words:
                if len(w) >= 3 and w not in STOPWORDS:
                    word_owners.setdefault(w, set()).add(name)
        for w in words:
            if len(w) < 3 or w in STOPWORDS:
                continue
            owners = word_owners.get(w)
            if owners is None and len(w) >= 4:  # tolerate a typo in a longer name word
                close = [k for k in word_owners if len(k) >= 4 and SequenceMatcher(None, w, k).ratio() >= NAME_FUZZ]
                owners = set().union(*(word_owners[k] for k in close)) if close else None
            if owners and len(owners) == 1:
                found.extend(owners)
        return list(dict.fromkeys(found))

    def build_context(self, question: str, previous_patients: list[str]) -> Context:
        """Pick the records to send with a question, within CONTEXT_CHAR_BUDGET."""
        patients = self.find_patients(question) or [p for p in previous_patients if p in self.patient_names]
        scores = self.search(tokenize(question))
        include_old = any(w in HISTORY_WORDS for w in _WORD_RE.findall(question.casefold()))

        blocks: list[str] = []
        sources: list[str] = []
        used: set[int] = set()  # chunk positions already sent, or left out
        budget = CONTEXT_CHAR_BUDGET

        roster = self._roster()
        blocks.append(roster)
        budget -= len(roster)

        if patients and len(patients) <= MAX_FULL_PATIENTS:
            share = budget // len(patients)
            position = {id(c): i for i, c in enumerate(self._chunks)}
            for patient in patients:
                room = share
                for doc in self._patient_docs(patient, scores, position, include_old):
                    block = f"## {doc.title}\n\n{doc.text.strip()}"
                    if len(block) > room:
                        continue  # too big to send whole; its best sections can still come in as excerpts
                    blocks.append(block)
                    sources.append(f"{doc.title} (full document)")
                    used.update(position[id(c)] for c in doc.chunks)
                    room -= len(block)
                    budget -= len(block)

        focus = set(patients)
        if not include_old:
            used.update(i for i in scores if self._chunks[i].doc.superseded)

        def weight(chunk: Chunk) -> float:
            return (FOCUS_BOOST if chunk.doc.patient in focus else 1.0) * (SUPERSEDED_WEIGHT if chunk.doc.superseded else 1.0)

        ranked = sorted(((s * weight(self._chunks[i]), i) for i, s in scores.items() if i not in used), reverse=True)
        excerpts: dict[str, list[str]] = {}
        excerpt_docs: dict[str, Doc] = {}
        count = 0
        for _, i in ranked:
            if count >= MAX_EXCERPTS:
                break
            chunk = self._chunks[i]
            text = f"### Excerpt{f' ({chunk.heading})' if chunk.heading else ''}\n\n{chunk.text}"
            if len(text) > budget:
                continue
            excerpts.setdefault(chunk.doc.file.id, []).append(text)
            excerpt_docs[chunk.doc.file.id] = chunk.doc
            budget -= len(text)
            count += 1
        for fid, texts in excerpts.items():
            doc = excerpt_docs[fid]
            blocks.append(f"## {doc.title}\n\n" + "\n\n".join(texts))
            sources.append(f"{doc.title} ({len(texts)} excerpt{'s' if len(texts) != 1 else ''})")

        text = "\n\n".join(blocks)
        return Context(text, sources, patients, len(text))

    def _patient_docs(self, patient: str, scores: dict[int, float], position: dict[int, int], include_old: bool) -> list[Doc]:
        """The patient's current documents, most relevant to the question first, then (if asked for) older
        versions, newest first."""
        docs = [d for d in self.docs.values() if d.patient == patient and (include_old or not d.superseded)]

        def relevance(doc: Doc) -> float:
            return sum(scores.get(position[id(c)], 0.0) for c in doc.chunks)

        return sorted(docs, key=lambda d: (d.superseded, -relevance(d) if not d.superseded else -d.version, d.order, d.path))

    def _roster(self) -> str:
        names = self.patient_names
        lines = [f"- {n} (last updated {self.patient_updated.get(n, '')[:10] or 'unknown'})" for n in names[:MAX_ROSTER]]
        if len(names) > MAX_ROSTER:
            lines.append(f"- … and {len(names) - MAX_ROSTER} more")
        return f"## All patients in storage ({len(names)})\n\n" + ("\n".join(lines) if lines else "_None yet._")

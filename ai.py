"""AI calls: transcribe handwritten case notes (Info.md), generate nutrition and exercise plans,
and recommend homeopathic medicines.

The provider is picked with AI_PROVIDER in .env: claude (default), gemini or openai.
Prompts live in prompts/ so they can be edited without touching code.
"""
from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from datetime import date

from PIL import Image, ImageOps

import config

PDF_MIME = "application/pdf"
SUPPORTED_MIMES = {"image/jpeg", "image/png", PDF_MIME}

MAX_IMAGE_EDGE = 2400  # px; keeps handwriting legible while staying well under provider size limits
MAX_OUTPUT_TOKENS = 32000

# Claude models that accept server-side refusal fallbacks (`fallbacks: "default"`).
CLAUDE_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

# Gemini "model overloaded" style server errors are usually over within seconds: wait this long, then retry.
GEMINI_RETRY_DELAYS = (3, 8)

# Plan kind -> (system prompt, template) in prompts/.
PLAN_PROMPTS = {
    "nutrition": ("plan_system.md", "plan_template.md"),
    "exercise": ("exercise_system.md", "exercise_template.md"),
}


@dataclass(frozen=True)
class Attachment:
    name: str
    mime_type: str
    data: bytes


class AIError(Exception):
    """An AI call failed. The message is meant to be shown in the UI."""


def provider_label() -> str:
    return f"{config.AI_PROVIDER} ({config.ai_model()})"


# ---------------------------------------------------------------- public API


def transcribe_notes(patient_name: str, photos: list[Attachment]) -> str:
    """Markdown with '## Transcription' and '## Structured Summary' sections."""
    if not photos:
        raise AIError("There are no photos to transcribe.")
    parts: list[str | Attachment] = []
    for i, photo in enumerate(photos, 1):
        parts.append(f"Photo {i} of {len(photos)} (file: {photo.name})")
        parts.append(_prepare(photo))
    parts.append(f"Patient name: {patient_name}\n\nTranscribe these case notes and write the structured summary.")
    return _complete(_prompt("info_system.md"), parts)


def generate_plan(
    patient_name: str,
    info_md: str | None,
    extra_info_md: str,
    previous_plan: tuple[int, str] | None,
    photos: list[Attachment] | None = None,
    kind: str = "nutrition",
) -> str:
    """Plan body in Markdown (no title). `kind` is "nutrition" or "exercise".

    Uses Info.md when present, otherwise the photos. `previous_plan` is (version number, text).
    """
    system = _system(*PLAN_PROMPTS[kind])
    parts = _case_parts(patient_name, info_md, extra_info_md, photos)

    if previous_plan:
        number, text = previous_plan
        parts.append(f"# Previous {kind} plan (version {number})\n\n{text.strip()}")
        parts.append(
            "Review this previous plan and produce an updated plan that accounts for the new information. "
            'Note explicitly what changed and why in the "Changes from Previous Plan" section.'
        )
    else:
        parts.append('This is the first plan for this patient. Leave out the "Changes from Previous Plan" section.')

    return _complete(system, parts)


def recommend_medicines(
    patient_name: str,
    info_md: str | None,
    extra_info_md: str,
    doctor_input: str,
    current_list: tuple[int, str] | None = None,
    photos: list[Attachment] | None = None,
) -> str:
    """Homeopathic medicine recommendations as a Markdown table (Recommendation | Potency | Rate).

    Without `current_list` this is a fresh recommendation, and `doctor_input` is optional extra context.
    With `current_list` (version number, text) the list is revised according to `doctor_input`.
    """
    system = _system("medicine_system.md", "medicine_template.md")
    parts = _case_parts(patient_name, info_md, extra_info_md, photos)

    if current_list:
        number, text = current_list
        parts.append(f"# Current recommendation list (version {number})\n\n{text.strip()}")
        parts.append(f"# Doctor's recommendations\n\n{doctor_input.strip()}")
        parts.append(
            "Create a revised recommendation list based on the doctor's recommendations above. "
            "The doctor's input takes priority: apply every change they ask for, and keep the parts they did not "
            "comment on unless their input makes them inconsistent. Output only the revised table."
        )
    else:
        if doctor_input.strip():
            parts.append(f"# Doctor's additional input for this request\n\n{doctor_input.strip()}")
        parts.append("Write the recommendations as the table only.")

    return _complete(system, parts)


# ---------------------------------------------------------------- helpers


def _system(system_file: str, template_file: str) -> str:
    return _prompt(system_file).replace("{{PLAN_TEMPLATE}}", _prompt(template_file).strip())


def _case_parts(
    patient_name: str, info_md: str | None, extra_info_md: str, photos: list[Attachment] | None
) -> list[str | Attachment]:
    """Patient name, date, case notes (Info.md, or the photos when it is missing) and Extra_info.md."""
    parts: list[str | Attachment] = [f"Patient name: {patient_name}\nToday's date: {date.today():%Y-%m-%d}"]

    if info_md and info_md.strip():
        parts.append(f"# Case notes (Info.md)\n\n{info_md.strip()}")
    elif photos:
        parts.append("# Case-note photos")
        for photo in photos:
            parts.append(f"Photo (file: {photo.name})")
            parts.append(_prepare(photo))
    else:
        parts.append("# Case notes\n\nNo case notes or photos are available.")

    parts.append(f"# Doctor's extra observations (Extra_info.md)\n\n{extra_info_md.strip() or 'None recorded.'}")
    return parts


def _prompt(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8")


def _prepare(att: Attachment) -> Attachment:
    """PDFs go as-is. Photos are rotated per EXIF, downscaled and re-encoded as JPEG."""
    if att.mime_type == PDF_MIME:
        return att
    if att.mime_type not in SUPPORTED_MIMES:
        raise AIError(f"Unsupported file type for '{att.name}': {att.mime_type}")
    try:
        with Image.open(io.BytesIO(att.data)) as original:
            img = ImageOps.exif_transpose(original).convert("RGB")
        img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
    except Exception as e:  # Pillow raises many exception types for bad images
        raise AIError(f"Could not read image '{att.name}': {e}") from e
    return Attachment(att.name, "image/jpeg", buf.getvalue())


def _complete(system: str, parts: list[str | Attachment]) -> str:
    providers = {"claude": _claude, "gemini": _gemini, "openai": _openai}
    call = providers.get(config.AI_PROVIDER)
    if call is None:
        raise AIError(f"Unknown AI_PROVIDER '{config.AI_PROVIDER}' in .env. Use claude, gemini or openai.")
    text = call(system, parts).strip()
    if not text:
        raise AIError("The AI returned an empty response. Try again.")
    return text


# ---------------------------------------------------------------- providers


def _claude(system: str, parts: list[str | Attachment]) -> str:
    import anthropic

    content = []
    for part in parts:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
        else:
            block_type = "document" if part.mime_type == PDF_MIME else "image"
            data = base64.standard_b64encode(part.data).decode("utf-8")
            content.append({"type": block_type, "source": {"type": "base64", "media_type": part.mime_type, "data": data}})

    request = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if config.CLAUDE_MODEL in CLAUDE_FALLBACK_MODELS:
        # If a safety classifier declines, the API re-runs the request on Anthropic's recommended fallback model.
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise AIError("Claude API key is missing or invalid. Check ANTHROPIC_API_KEY in .env.") from e
    except anthropic.PermissionDeniedError as e:
        raise AIError(f"Claude API key lacks permission: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise AIError(f"Claude model '{config.CLAUDE_MODEL}' was not found. Check CLAUDE_MODEL in .env.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Claude API rate limit reached. Wait a minute and try again.") from e
    except anthropic.BadRequestError as e:
        raise AIError(f"Claude rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Could not reach the Claude API. Check your internet connection.") from e
    except (anthropic.AnthropicError, TypeError) as e:  # e.g. no credentials configured at all
        raise AIError(f"Claude API is not configured correctly: {e}") from e

    if message.stop_reason == "refusal":
        raise AIError("Claude declined to answer this request.")
    if message.stop_reason == "max_tokens":
        raise AIError("The AI response was cut off at the output limit, so nothing was saved. Try again.")
    return "".join(block.text for block in message.content if block.type == "text")


def _gemini(system: str, parts: list[str | Attachment]) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise AIError("Gemini SDK is not installed. Run: pip install google-genai") from e
    if not config.GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set in .env.")

    from google.genai import errors

    contents = [p if isinstance(p, str) else types.Part.from_bytes(data=p.data, mime_type=p.mime_type) for p in parts]
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    for delay in (*GEMINI_RETRY_DELAYS, None):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # no tools used
                ),
            )
            return response.text or ""
        except errors.ServerError as e:  # 5xx, e.g. 503 "model is currently experiencing high demand"
            if delay is None:
                raise AIError(f"Gemini is temporarily unavailable ({e.code}) after several tries. Wait a minute and try again.") from e
            time.sleep(delay)
        except Exception as e:
            raise AIError(f"Gemini API error: {e}") from e


def _openai(system: str, parts: list[str | Attachment]) -> str:
    try:
        import openai
    except ImportError as e:
        raise AIError("OpenAI SDK is not installed. Run: pip install openai") from e
    if not config.OPENAI_API_KEY:
        raise AIError("OPENAI_API_KEY is not set in .env.")

    content = []
    for part in parts:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
            continue
        data_url = f"data:{part.mime_type};base64,{base64.b64encode(part.data).decode('utf-8')}"
        if part.mime_type == PDF_MIME:
            content.append({"type": "file", "file": {"filename": part.name, "file_data": data_url}})
        else:
            content.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})
    try:
        client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            max_completion_tokens=MAX_OUTPUT_TOKENS,
        )
    except openai.OpenAIError as e:
        raise AIError(f"OpenAI API error: {e}") from e
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise AIError("The AI response was cut off at the output limit, so nothing was saved. Try again.")
    return choice.message.content or ""

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import google.auth
import google.auth.transport.requests
import httpx
from bs4 import BeautifulSoup
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("avatar-studio")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PRESETS_DIR = STATIC_DIR / "presets"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "studio.db"

GCP_PROJECT = os.environ.get("GCP_PROJECT", "cloud-llm-preview1")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
# Model ID from the "Gemini 3.8 Live API with Avatar - Private Preview User Guide".
DEFAULT_LIVE_MODEL = os.environ.get("LIVE_MODEL", "gemini-3.8-live-preview")

# "Phonetic Standard" script from the Private Preview guide (best overall quality, ~15s read).
VOICE_RECORDING_SCRIPT = (
    "When the sunlight strikes raindrops in the air, they act like a prism and form a rainbow. "
    "The rainbow is a division of white light into many beautiful colors. "
    "These take the shape of a long round arch."
)

PREBUILT_VOICES = [
    {"id": "Aoede", "name": "Aoede", "style": "Warm, expressive & articulate", "gender": "Female"},
    {"id": "Puck", "name": "Puck", "style": "Upbeat, conversational & friendly", "gender": "Male"},
    {"id": "Kore", "name": "Kore", "style": "Calm, composed & professional", "gender": "Female"},
    {"id": "Charon", "name": "Charon", "style": "Deep, authoritative & clear", "gender": "Male"},
    {"id": "Fenrir", "name": "Fenrir", "style": "Energetic, dynamic & bold", "gender": "Male"},
    {"id": "Zephyr", "name": "Zephyr", "style": "Breezy, natural & empathetic", "gender": "Female"},
    {"id": "Orbit", "name": "Orbit", "style": "Crisp, analytical & modern", "gender": "Neutral"},
    {"id": "Achernar", "name": "Achernar", "style": "Smooth, poised & executive", "gender": "Female"},
]

PRESET_AVATARS = [
    {
        "id": "preset-aria",
        "name": "Aria (Custom Photo Portrait)",
        "type": "custom_photo",
        "preview_url": "/static/presets/aria.jpg",
        "voice": "Aoede",
        "badge": "9:16 Studio Portrait",
    },
    {
        "id": "preset-marcus",
        "name": "Marcus (Custom Photo Portrait)",
        "type": "custom_photo",
        "preview_url": "/static/presets/marcus.jpg",
        "voice": "Charon",
        "badge": "9:16 Studio Portrait",
    },
    {
        "id": "preset-elena",
        "name": "Elena (Custom Photo Portrait)",
        "type": "custom_photo",
        "preview_url": "/static/presets/elena.jpg",
        "voice": "Kore",
        "badge": "9:16 Studio Portrait",
    },
    {
        "id": "builtin-Kira",
        "name": "Kira (Built-in Gemini Avatar)",
        "type": "builtin",
        "avatar_name": "Kira",
        "preview_url": "/static/presets/aria.jpg",
        "voice": "Aoede",
        "badge": "Built-in 3D/Live",
    },
    {
        "id": "builtin-Kai",
        "name": "Kai (Built-in Gemini Avatar)",
        "type": "builtin",
        "avatar_name": "Kai",
        "preview_url": "/static/presets/marcus.jpg",
        "voice": "Puck",
        "badge": "Built-in 3D/Live",
    },
    {
        "id": "builtin-Vera",
        "name": "Vera (Built-in Gemini Avatar)",
        "type": "builtin",
        "avatar_name": "Vera",
        "preview_url": "/static/presets/elena.jpg",
        "voice": "Zephyr",
        "badge": "Built-in 3D/Live",
    },
]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_KNOWLEDGE_EXTS = {".txt", ".md", ".pdf", ".csv", ".json", ".html"}


# ---------------------------------------------------------------------------
# Media Normalization Helpers (FFmpeg)
# ---------------------------------------------------------------------------
def normalize_to_portrait_jpeg(raw_bytes: bytes) -> bytes:
    """
    Converts any input image (JPEG, PNG, WebP, any aspect ratio) into an exact
    704x1280 (9:16 portrait) RGB JPEG required by Gemini Live customizedAvatar.
    """
    with tempfile.NamedTemporaryFile(suffix=".img", delete=True) as src_tmp, \
         tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as dst_tmp:
        src_tmp.write(raw_bytes)
        src_tmp.flush()
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src_tmp.name,
            "-vf",
            "scale=704:1280:force_original_aspect_ratio=increase,crop=704:1280,format=yuvj420p",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-q:v",
            "2",
            dst_tmp.name,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if res.returncode != 0:
            logger.error("FFmpeg image normalization failed: %s", res.stderr.decode(errors="ignore"))
            raise HTTPException(status_code=400, detail="Uploaded file could not be processed as a valid image.")
        data = Path(dst_tmp.name).read_bytes()
        if not data:
            raise HTTPException(status_code=400, detail="Image conversion produced empty output.")
        return data


def normalize_to_wav_24k(raw_bytes: bytes) -> bytes:
    """
    Converts any input audio (WAV, WebM, MP3, OGG, M4A, MP4, AAC, FLAC) into 24kHz 16-bit mono
    PCM WAV required by Gemini Live replicatedVoiceConfig.
    """
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=True) as src_tmp, \
         tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as dst_tmp:
        src_tmp.write(raw_bytes)
        src_tmp.flush()
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src_tmp.name,
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            "-t",
            "30",
            "-f",
            "wav",
            dst_tmp.name,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=20)
        if res.returncode != 0:
            logger.error("FFmpeg audio normalization failed: %s", res.stderr.decode(errors="ignore"))
            raise HTTPException(status_code=400, detail="Uploaded audio could not be converted to 24kHz PCM WAV.")
        data = Path(dst_tmp.name).read_bytes()
        if len(data) < 1000:
            raise HTTPException(status_code=400, detail="Voice recording is too short. Please provide at least 3 seconds of speech.")
        return data


# Private Preview guide: custom voice sample should be 10-20 s of 16-bit PCM with as little
# background noise as possible, because the replicated voice copies the room noise it hears.
# Gentle cleanup: 70 Hz high-pass (fan / AC rumble), light FFT denoise, trim leading silence,
# cap at 20 s with a short fade-out. NOTE: gemini-3.8-live-preview only accepts a WAV container
# (raw PCM is rejected with "1007 Failed to parse WAV audio"), so the cleaned PCM is re-wrapped
# in an exact-size WAV header and sent as audio/wav.
LIVE_VOICE_SAMPLE_RATE = 24000
LIVE_VOICE_SAMPLE_MAX_S = 20
LIVE_VOICE_SAMPLE_MIME = "audio/wav"
_LIVE_VOICE_FILTER_TAIL = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
    f"atrim=0:{LIVE_VOICE_SAMPLE_MAX_S},"
    f"afade=t=out:st={LIVE_VOICE_SAMPLE_MAX_S - 0.15}:d=0.15"
)
LIVE_VOICE_CLEANUP_FILTERS = {
    # Live A/B on gemini-3.8-live-preview: output pause noise about -51 dBFS vs -46 for the raw sample.
    "gentle": "highpass=f=70,afftdn=nr=12:nf=-50:tn=1," + _LIVE_VOICE_FILTER_TAIL,
    # Adds a noise gate so the sample's pauses are truly silent; the clone reproduces that silence
    # (best live run about -67 dBFS pause noise = prebuilt-voice level) with the voice spectrum unchanged.
    "gated": (
        "highpass=f=90,afftdn=nr=24:nf=-45:tn=1,"
        "agate=threshold=0.02:ratio=4:range=0.03:attack=5:release=150," + _LIVE_VOICE_FILTER_TAIL
    ),
}
LIVE_VOICE_CLEANUP = os.environ.get("LIVE_VOICE_CLEANUP", "gated").strip().lower()
if LIVE_VOICE_CLEANUP not in LIVE_VOICE_CLEANUP_FILTERS:
    LIVE_VOICE_CLEANUP = "gated"
LIVE_VOICE_CLEANUP_FILTER = LIVE_VOICE_CLEANUP_FILTERS[LIVE_VOICE_CLEANUP]
_LIVE_VOICE_SAMPLE_CACHE: Dict[str, str] = {}


def prepare_live_voice_sample(wav_b64: str) -> Optional[str]:
    """Returns base64 WAV (24 kHz mono 16-bit, cleaned, <=20 s) for `replicatedVoiceConfig`,
    or None if processing fails (caller then sends the stored WAV unchanged). The stored sample
    itself is never modified; results are cached per sample."""
    if not wav_b64:
        return None
    key = hashlib.sha256((LIVE_VOICE_CLEANUP_FILTER + "|" + wav_b64).encode("ascii", errors="ignore")).hexdigest()
    cached = _LIVE_VOICE_SAMPLE_CACHE.get(key)
    if cached:
        return cached
    try:
        raw = base64.b64decode(wav_b64)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-af",
            LIVE_VOICE_CLEANUP_FILTER,
            "-ac",
            "1",
            "-ar",
            str(LIVE_VOICE_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ]
        res = subprocess.run(cmd, input=raw, capture_output=True, timeout=20)
        pcm = res.stdout
        if res.returncode != 0 or len(pcm) < LIVE_VOICE_SAMPLE_RATE * 2 * 3:  # need >= 3 s
            logger.warning(
                "Voice sample cleanup failed (rc=%s, %d bytes): %s",
                res.returncode,
                len(pcm),
                res.stderr.decode(errors="ignore")[:300],
            )
            return None
        # FFmpeg cannot finalize WAV sizes when writing to a pipe, so build the header here.
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(LIVE_VOICE_SAMPLE_RATE)
            wav_out.writeframes(pcm)
        clean_b64 = base64.b64encode(wav_buf.getvalue()).decode("ascii")
        _LIVE_VOICE_SAMPLE_CACHE[key] = clean_b64
        logger.info(
            "Prepared live voice sample: %.1fs cleaned WAV (%s, %s cleanup) from %d-byte stored WAV",
            len(pcm) / (LIVE_VOICE_SAMPLE_RATE * 2),
            LIVE_VOICE_SAMPLE_MIME,
            LIVE_VOICE_CLEANUP,
            len(raw),
        )
        return clean_b64
    except Exception as exc:
        logger.warning("Voice sample cleanup error: %s", exc)
        return None


# "clean" (default): send the cleaned sample, automatically falling back to the stored WAV if
# Vertex rejects it. "raw": always send the stored WAV exactly as recorded.
LIVE_VOICE_SAMPLE_FORMAT = os.environ.get("LIVE_VOICE_SAMPLE_FORMAT", "clean").strip().lower()


def schedule_voice_sample_warmup(custom_voice_b64: str) -> None:
    """Pre-computes the cleaned sample in a worker thread right after a voice is saved, so the next
    session start is a cache hit instead of waiting on FFmpeg."""
    if LIVE_VOICE_SAMPLE_FORMAT != "clean" or not custom_voice_b64:
        return
    try:
        asyncio.get_running_loop().run_in_executor(None, prepare_live_voice_sample, custom_voice_b64)
    except RuntimeError:
        prepare_live_voice_sample(custom_voice_b64)


def warm_live_voice_samples() -> None:
    """Startup warm-up: prepares the cleaned sample for every saved custom-voice avatar."""
    if LIVE_VOICE_SAMPLE_FORMAT != "clean":
        return
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT custom_voice_b64 FROM avatars WHERE voice_mode = 'custom_voice' AND custom_voice_b64 != '';"
        ).fetchall()
        conn.close()
        for row in rows:
            prepare_live_voice_sample(row["custom_voice_b64"])
    except Exception as exc:
        logger.warning("Voice sample warm-up skipped: %s", exc)


# ---------------------------------------------------------------------------
# Google Cloud Authentication Helper (Cloudtop + Cloud Run compatible)
# ---------------------------------------------------------------------------
GLOBAL_TOKEN_STATE: Dict[str, Any] = {
    "token": os.environ.get("GCP_ACCESS_TOKEN", "").strip(),
    "updated_at": time.time(),
}


CLOUDTOP_TOKEN_REFRESH_SCRIPT = Path.home() / "bin" / "refresh-google-gcp-token"
CLOUDTOP_TOKEN_FILE = Path.home() / ".config" / "gcloud" / "google_access_token"
# Tokens live 60 min. The background loop refreshes every 10 min, so the foreground path (session
# start) only has to run the SSO script itself if the file is somehow older than 40 min.
CLOUDTOP_TOKEN_FOREGROUND_MAX_AGE_S = 2400
CLOUDTOP_TOKEN_BACKGROUND_EVERY_S = 600


async def cloudtop_token_refresher() -> None:
    """Keeps the local token file fresh in the background so starting a live session never
    waits on the token refresh script."""
    if not CLOUDTOP_TOKEN_REFRESH_SCRIPT.exists():
        return
    while True:
        await asyncio.sleep(CLOUDTOP_TOKEN_BACKGROUND_EVERY_S)
        try:
            await asyncio.to_thread(
                subprocess.run, [str(CLOUDTOP_TOKEN_REFRESH_SCRIPT)], check=True, timeout=30, capture_output=True
            )
        except Exception as exc:
            logger.warning("Background token refresh failed (will retry): %s", exc)


def get_gcp_access_token(force_default_creds: bool = False) -> str:
    """
    Obtains a valid Google Cloud OAuth2 access token for Vertex AI Live.
    1. If a local token refresh script is present, reads the token file kept fresh by
       `cloudtop_token_refresher`, and only invokes the refresh script itself when the file is
       missing or older than 40 minutes.
    2. Otherwise uses `GLOBAL_TOKEN_STATE["token"]` if freshly updated (<45m old) or
       `google.auth.default()`.
    """
    refresh_script = CLOUDTOP_TOKEN_REFRESH_SCRIPT
    token_file = CLOUDTOP_TOKEN_FILE
    if refresh_script.exists():
        try:
            max_age = 0 if force_default_creds else CLOUDTOP_TOKEN_FOREGROUND_MAX_AGE_S
            if not token_file.exists() or (time.time() - token_file.stat().st_mtime > max_age):
                subprocess.run([str(refresh_script)], check=True, timeout=10)
            if token_file.exists():
                tok = token_file.read_text().strip()
                if tok:
                    GLOBAL_TOKEN_STATE["token"] = tok
                    GLOBAL_TOKEN_STATE["updated_at"] = time.time()
                    return tok
        except Exception as exc:
            logger.warning("Cloudtop token refresh script fallback: %s", exc)

    if not force_default_creds:
        tmp_token_file = Path("/tmp/google_access_token")
        if tmp_token_file.exists():
            try:
                if time.time() - tmp_token_file.stat().st_mtime < 2700:
                    tok = tmp_token_file.read_text().strip()
                    if tok:
                        return tok
            except Exception:
                pass

        if GLOBAL_TOKEN_STATE.get("token") and (time.time() - GLOBAL_TOKEN_STATE.get("updated_at", 0) < 2700):
            return GLOBAL_TOKEN_STATE["token"]

    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        if creds.token:
            return creds.token
    except Exception as exc:
        logger.warning("google.auth.default() failed: %s", exc)

    env_tok = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
    if env_tok:
        return env_tok

    # Final fallback: gcloud auth print-access-token
    try:
        out = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", "--configuration=google"],
            timeout=8,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            return out
    except Exception:
        pass

    raise RuntimeError("Unable to obtain Google Cloud OAuth access token.")


# ---------------------------------------------------------------------------
# Database Layer (SQLite with Parameterized Queries)
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avatars (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role_tagline TEXT NOT NULL DEFAULT '',
                avatar_mode TEXT NOT NULL DEFAULT 'custom_photo',
                builtin_avatar_name TEXT NOT NULL DEFAULT 'Kira',
                photo_b64 TEXT NOT NULL DEFAULT '',
                photo_mime TEXT NOT NULL DEFAULT 'image/jpeg',
                voice_mode TEXT NOT NULL DEFAULT 'prebuilt',
                prebuilt_voice TEXT NOT NULL DEFAULT 'Aoede',
                custom_voice_b64 TEXT NOT NULL DEFAULT '',
                custom_voice_mime TEXT NOT NULL DEFAULT 'audio/wav',
                system_instruction TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id TEXT PRIMARY KEY,
                avatar_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (avatar_id) REFERENCES avatars(id) ON DELETE CASCADE
            );
            """
        )

    # Seed initial showcase avatars if table is empty
    cur = conn.execute("SELECT COUNT(*) AS cnt FROM avatars;")
    if cur.fetchone()["cnt"] == 0:
        seed_default_avatars(conn)
    conn.close()


def seed_default_avatars(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    aria_path = PRESETS_DIR / "aria.jpg"
    marcus_path = PRESETS_DIR / "marcus.jpg"
    elena_path = PRESETS_DIR / "elena.jpg"

    aria_b64 = base64.b64encode(normalize_to_portrait_jpeg(aria_path.read_bytes())).decode() if aria_path.exists() else ""
    marcus_b64 = base64.b64encode(normalize_to_portrait_jpeg(marcus_path.read_bytes())).decode() if marcus_path.exists() else ""
    elena_b64 = base64.b64encode(normalize_to_portrait_jpeg(elena_path.read_bytes())).decode() if elena_path.exists() else ""

    seeds = [
        {
            "id": "avatar-aria-architect",
            "name": "Aria Chen",
            "role_tagline": "Principal Gemini 3.8 Live Cloud Architect",
            "avatar_mode": "custom_photo",
            "builtin_avatar_name": "Kira",
            "photo_b64": aria_b64,
            "voice_mode": "prebuilt",
            "prebuilt_voice": "Aoede",
            "system_instruction": (
                "You are Aria Chen, Principal Cloud AI Architect at Google Cloud. "
                "You speak warmly, concisely, and enthusiastically. Whenever the user asks "
                "about architecture, Gemini 3.8 Live capabilities, pricing, SLAs, or attached documents, "
                "use your attached Knowledge Base or call the `search_avatar_knowledge` tool to give "
                "accurate, grounded answers."
            ),
            "knowledge": [
                {
                    "title": "Gemini 3.8 Live Architecture & Custom Avatar Spec",
                    "source_type": "note",
                    "source_ref": "internal://specs/gemini-3.8-live",
                    "content": (
                        "Gemini 3.8 Live (`gemini-3.8-live`) supports real-time bidirectional audio and 9:16 portrait "
                        "video avatar generation (`responseModalities: ['VIDEO']`). Custom photo avatars use "
                        "`avatarConfig.customizedAvatar` with a 704x1280 RGB JPEG image. Custom voice cloning uses "
                        "`speechConfig.voiceConfig.replicatedVoiceConfig` with a 24kHz 16-bit mono PCM WAV audio sample. "
                        "Project `cloud-llm-preview1` (project number 801452371447) is enabled for both Custom Avatar "
                        "and Custom Voice in `us-central1`."
                    ),
                },
                {
                    "title": "Enterprise Cloud Run Deployment & SLA Guide",
                    "source_type": "file",
                    "source_ref": "cloud-run-enterprise-sla.md",
                    "content": (
                        "Our Cloud Run deployment runs in `us-central1` with WebSocket session affinity and streaming "
                        "FFmpeg MP4-to-fMP4 remuxing (`frag_keyframe+empty_moov+default_base_moof+separate_moof`). "
                        "Target first-frame video latency is 1.2 seconds, with 99.95% enterprise uptime SLA and "
                        "automatic horizontal scaling up to 100 concurrent interactive avatar sessions."
                    ),
                },
            ],
        },
        {
            "id": "avatar-marcus-advisor",
            "name": "Dr. Marcus Vance",
            "role_tagline": "AI Product & Executive Strategy Coach",
            "avatar_mode": "custom_photo",
            "builtin_avatar_name": "Kai",
            "photo_b64": marcus_b64,
            "voice_mode": "prebuilt",
            "prebuilt_voice": "Charon",
            "system_instruction": (
                "You are Dr. Marcus Vance, an executive advisor and AI product strategist. "
                "Keep responses structured, insightful, and conversational (2 to 4 sentences per turn). "
                "Always reference facts from your attached knowledge base when asked about product roadmaps or metrics."
            ),
            "knowledge": [
                {
                    "title": "Q4 2026 AI Avatar Product Roadmap & KPIs",
                    "source_type": "file",
                    "source_ref": "q4-2026-roadmap.pdf",
                    "content": (
                        "Q4 2026 Key Milestones: 1) Launch self-service Custom Photo + Custom Voice Avatar Studio "
                        "by October 15, 2026. 2) Achieve 94% Customer Satisfaction (CSAT) on live knowledge-grounded "
                        "support sessions. 3) Reduce average ticket resolution time from 18 minutes to 2.4 minutes "
                        "using real-time RAG grounding with the Backend Knowledge Agent."
                    ),
                }
            ],
        },
        {
            "id": "avatar-elena-specialist",
            "name": "Elena Rostova",
            "role_tagline": "Customer Experience & Onboarding Specialist",
            "avatar_mode": "custom_photo",
            "builtin_avatar_name": "Vera",
            "photo_b64": elena_b64,
            "voice_mode": "prebuilt",
            "prebuilt_voice": "Kore",
            "system_instruction": (
                "You are Elena Rostova, Lead Customer Onboarding Specialist. You are friendly, patient, and clear. "
                "Use your attached knowledge base whenever answering customer onboarding or policy questions."
            ),
            "knowledge": [
                {
                    "title": "Voice Profile Recording Best Practices",
                    "source_type": "note",
                    "source_ref": "voice-cloning-guide.md",
                    "content": (
                        "To create a high-fidelity custom voice profile with `replicatedVoiceConfig`, users should read "
                        "the provided calibration script in a quiet room at a natural, relaxed pace (approx. 10-18 seconds): "
                        f'"{VOICE_RECORDING_SCRIPT}"'
                    ),
                }
            ],
        },
    ]

    with conn:
        for s in seeds:
            conn.execute(
                """
                INSERT INTO avatars (
                    id, name, role_tagline, avatar_mode, builtin_avatar_name,
                    photo_b64, photo_mime, voice_mode, prebuilt_voice,
                    custom_voice_b64, custom_voice_mime, system_instruction,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'image/jpeg', ?, ?, '', 'audio/wav', ?, ?, ?);
                """,
                (
                    s["id"],
                    s["name"],
                    s["role_tagline"],
                    s["avatar_mode"],
                    s["builtin_avatar_name"],
                    s["photo_b64"],
                    s["voice_mode"],
                    s["prebuilt_voice"],
                    s["system_instruction"],
                    now,
                    now,
                ),
            )
            for k in s["knowledge"]:
                conn.execute(
                    """
                    INSERT INTO knowledge_items (
                        id, avatar_id, title, source_type, source_ref, content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        f"kn-{uuid.uuid4().hex[:10]}",
                        s["id"],
                        k["title"],
                        k["source_type"],
                        k["source_ref"],
                        k["content"],
                        now,
                    ),
                )


# ---------------------------------------------------------------------------
# Security & SSRF Validation for URL Knowledge Ingestion
# ---------------------------------------------------------------------------
def validate_external_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are permitted.")
    if not parsed.hostname:
        raise ValueError("URL must contain a valid hostname.")
    hostname = parsed.hostname.lower()
    if hostname in ("localhost", "metadata.google.internal", "169.254.169.254"):
        raise ValueError("Access to internal or metadata hosts is forbidden.")
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
                raise ValueError("URL resolves to a private or restricted IP address.")
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname: {hostname}") from exc
    return url.strip()


# ---------------------------------------------------------------------------
# Backend Knowledge Agent (RAG Retrieval + Gemini Grounded Synthesis)
# ---------------------------------------------------------------------------
def get_avatar_knowledge_items(avatar_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, source_type, source_ref, content, created_at FROM knowledge_items WHERE avatar_id = ? ORDER BY created_at DESC;",
        (avatar_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def retrieve_relevant_chunks(avatar_id: str, query: str, top_k: int = 4) -> List[Dict[str, Any]]:
    items = get_avatar_knowledge_items(avatar_id)
    if not items:
        return []

    tokens = [
        t for t in re.findall(r"[a-zA-Z0-9_.-]{2,}", query.lower())
        if t not in {"the", "and", "for", "with", "what", "how", "can", "you", "tell", "about", "this", "from", "are", "is"}
    ]

    scored = []
    for item in items:
        haystack = f"{item['title']} {item['source_ref']} {item['content']}".lower()
        score = 1.0  # Base score so even broad questions return attached knowledge
        for tok in tokens:
            count = haystack.count(tok)
            if count > 0:
                score += 3.0 * (1 + math.log1p(count))
            if tok in item["title"].lower():
                score += 5.0
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, item in scored[:top_k]:
        results.append(
            {
                "id": item["id"],
                "title": item["title"],
                "source_type": item["source_type"],
                "source_ref": item["source_ref"],
                "excerpt": item["content"][:1200],
                "score": round(score, 2),
            }
        )
    return results


async def run_backend_knowledge_agent(avatar_id: str, query: str) -> Dict[str, Any]:
    """
    Executes the Backend Knowledge Agent:
    1. Retrieves the top matching chunks from the avatar's SQLite knowledge base.
    2. Calls Vertex AI (`gemini-2.5-flash`) with strict grounding instructions to synthesize
       an authoritative answer from the retrieved sources, falling back to direct chunk synthesis
       if offline.
    """
    chunks = retrieve_relevant_chunks(avatar_id, query, top_k=5)
    if not chunks:
        return {
            "query": query,
            "found": False,
            "agent_answer": "No external documents or links have been attached to this avatar's knowledge base yet.",
            "citations": [],
        }

    context_blocks = []
    for idx, c in enumerate(chunks, 1):
        context_blocks.append(
            f"[Source {idx}: {c['title']} ({c['source_type']}: {c['source_ref']})]\n{c['excerpt']}"
        )
    joined_context = "\n\n".join(context_blocks)

    synthesized = ""
    try:
        token = await asyncio.to_thread(get_gcp_access_token)
        endpoint = (
            f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1beta1/"
            f"projects/{GCP_PROJECT}/locations/{GCP_LOCATION}/publishers/google/models/gemini-2.5-flash:generateContent"
        )
        prompt = (
            "You are the Backend Knowledge Retrieval Agent for a real-time Gemini 3.8 Live Avatar.\n"
            "Answer the user's question accurately using ONLY the attached knowledge sources below. "
            "Cite source titles naturally and keep your synthesis concise (2-4 sentences) so the live avatar "
            "can speak it smoothly.\n\n"
            f"KNOWLEDGE SOURCES:\n{joined_context}\n\n"
            f"QUESTION: {query}"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 350},
                },
            )
            if resp.status_code == 200:
                body = resp.json()
                cands = body.get("candidates", [])
                if cands:
                    parts = cands[0].get("content", {}).get("parts", [])
                    synthesized = " ".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:
        logger.warning("Vertex AI Gemini 2.5 Flash synthesis fallback: %s", exc)

    if not synthesized:
        synthesized = "Based on attached knowledge (" + ", ".join(c["title"] for c in chunks[:2]) + "): " + chunks[0]["excerpt"][:500]

    return {
        "query": query,
        "found": True,
        "agent_answer": synthesized,
        "citations": chunks,
    }


# ---------------------------------------------------------------------------
# FastAPI Lifespan & App Initialization
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.get_running_loop().run_in_executor(None, warm_live_voice_samples)
    token_refresher_task = asyncio.create_task(cloudtop_token_refresher())
    yield
    token_refresher_task.cancel()


app = FastAPI(
    title="Gemini 3.8 Live Custom Avatar & Voice Studio",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self';"
    )
    if (
        request.url.path == "/"
        or request.url.path.startswith("/api/")
        or request.url.path.endswith(".js")
        or request.url.path.endswith(".css")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def get_studio_config():
    return {
        "project": GCP_PROJECT,
        "location": GCP_LOCATION,
        "default_model": DEFAULT_LIVE_MODEL,
        "available_models": [
            {"id": "gemini-3.8-live-preview", "label": "Gemini 3.8 Live Preview (default — Custom Avatar + Voice)"},
            {"id": "gemini-3.8-live", "label": "Gemini 3.8 Live"},
            {"id": "gemini-3.8-live-extended-thinking-preview", "label": "Gemini 3.8 Live Extended Thinking"},
        ],
        "voice_recording_script": VOICE_RECORDING_SCRIPT,
        "prebuilt_voices": PREBUILT_VOICES,
        "preset_avatars": PRESET_AVATARS,
    }


def serialize_avatar_row(row: sqlite3.Row, include_knowledge: bool = True) -> Dict[str, Any]:
    d = dict(row)
    avatar_id = d["id"]
    items = get_avatar_knowledge_items(avatar_id) if include_knowledge else []
    has_custom_photo = bool(d.get("photo_b64"))
    has_custom_voice = bool(d.get("custom_voice_b64"))
    return {
        "id": avatar_id,
        "name": d["name"],
        "role_tagline": d["role_tagline"],
        "avatar_mode": d["avatar_mode"],
        "builtin_avatar_name": d["builtin_avatar_name"],
        "has_custom_photo": has_custom_photo,
        "photo_data_url": f"data:{d['photo_mime']};base64,{d['photo_b64']}" if has_custom_photo else f"/static/presets/aria.jpg",
        "voice_mode": d["voice_mode"],
        "prebuilt_voice": d["prebuilt_voice"],
        "has_custom_voice": has_custom_voice,
        "custom_voice_data_url": f"data:{d['custom_voice_mime']};base64,{d['custom_voice_b64']}" if has_custom_voice else "",
        "system_instruction": d["system_instruction"],
        "knowledge_count": len(items),
        "knowledge_items": items,
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    }


@app.get("/api/avatars")
async def list_avatars():
    conn = get_db()
    rows = conn.execute("SELECT * FROM avatars ORDER BY updated_at DESC;").fetchall()
    conn.close()
    return {"avatars": [serialize_avatar_row(r) for r in rows]}


@app.get("/api/avatars/{avatar_id}")
async def get_avatar(avatar_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Avatar not found")
    return serialize_avatar_row(row)


@app.post("/api/avatars")
async def create_or_save_avatar(request: Request):
    """
    Creates and persists a reusable Avatar with:
    - Uploaded custom photo (or webcam capture / preset portrait), automatically normalized
      to 704x1280 (9:16 portrait) JPEG via FFmpeg so Gemini 3.8 Live `customizedAvatar` succeeds.
    - Recorded custom voice sample (or uploaded WAV/MP3) normalized to 24kHz mono PCM WAV
      for `replicatedVoiceConfig`, OR preselected voice.
    - Custom system instructions.
    Uses `request.form(max_part_size=50 * 1024 * 1024)` so large high-res photo/audio
    parts never trigger Starlette's default 1024KB form part limit.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        form_data = await request.json()
    else:
        form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=20, max_fields=50)

    def get_str(key: str, default: str = "") -> str:
        val = form_data.get(key, default)
        return val if isinstance(val, str) else default

    name = get_str("name", "My Custom Avatar")
    role_tagline = get_str("role_tagline", "Custom AI Persona")
    avatar_mode = get_str("avatar_mode", "custom_photo")
    builtin_avatar_name = get_str("builtin_avatar_name", "Kira")
    preset_photo_id = get_str("preset_photo_id", "")
    voice_mode = get_str("voice_mode", "prebuilt")
    prebuilt_voice = get_str("prebuilt_voice", "Aoede")
    system_instruction = get_str("system_instruction", "")
    photo_data_url = get_str("photo_data_url", "")
    voice_data_url = get_str("voice_data_url", "")

    photo_file = form_data.get("photo_file")
    voice_file = form_data.get("voice_file")

    clean_name = name.strip()[:80]
    if not clean_name:
        raise HTTPException(status_code=400, detail="Avatar name is required.")

    photo_b64 = ""
    if photo_file is not None and hasattr(photo_file, "read"):
        raw_img = await photo_file.read()
        if raw_img:
            norm_img = normalize_to_portrait_jpeg(raw_img)
            photo_b64 = base64.b64encode(norm_img).decode()
    if not photo_b64 and photo_data_url and "," in photo_data_url:
        raw_img = base64.b64decode(photo_data_url.split(",", 1)[1])
        norm_img = normalize_to_portrait_jpeg(raw_img)
        photo_b64 = base64.b64encode(norm_img).decode()
    if not photo_b64 and preset_photo_id:
        preset_map = {
            "preset-aria": PRESETS_DIR / "aria.jpg",
            "preset-marcus": PRESETS_DIR / "marcus.jpg",
            "preset-elena": PRESETS_DIR / "elena.jpg",
        }
        p_path = preset_map.get(preset_photo_id, PRESETS_DIR / "aria.jpg")
        if p_path.exists():
            photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(p_path.read_bytes())).decode()
    if not photo_b64:
        default_path = PRESETS_DIR / "aria.jpg"
        if default_path.exists():
            photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(default_path.read_bytes())).decode()

    custom_voice_b64 = ""
    if voice_file is not None and hasattr(voice_file, "read"):
        raw_aud = await voice_file.read()
        if raw_aud:
            norm_wav = normalize_to_wav_24k(raw_aud)
            custom_voice_b64 = base64.b64encode(norm_wav).decode()
            voice_mode = "custom_voice"
    if not custom_voice_b64 and voice_data_url and "," in voice_data_url:
        raw_aud = base64.b64decode(voice_data_url.split(",", 1)[1])
        norm_wav = normalize_to_wav_24k(raw_aud)
        custom_voice_b64 = base64.b64encode(norm_wav).decode()
        voice_mode = "custom_voice"

    avatar_id = f"avatar-{uuid.uuid4().hex[:10]}"
    now = int(time.time())

    if not system_instruction.strip():
        system_instruction = (
            f"You are {clean_name} ({role_tagline.strip()}). "
            "Speak naturally, warmly, and concisely. When the user asks questions about your attached "
            "documents, links, or domain knowledge, use your Knowledge Base or call `search_avatar_knowledge` "
            "to provide accurate, grounded answers."
        )

    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO avatars (
                id, name, role_tagline, avatar_mode, builtin_avatar_name,
                photo_b64, photo_mime, voice_mode, prebuilt_voice,
                custom_voice_b64, custom_voice_mime, system_instruction,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'image/jpeg', ?, ?, ?, 'audio/wav', ?, ?, ?);
            """,
            (
                avatar_id,
                clean_name,
                role_tagline.strip()[:140],
                avatar_mode if avatar_mode in ("custom_photo", "builtin") else "custom_photo",
                builtin_avatar_name.strip()[:40] or "Kira",
                photo_b64,
                voice_mode if voice_mode in ("custom_voice", "prebuilt") else "prebuilt",
                prebuilt_voice.strip()[:40] or "Aoede",
                custom_voice_b64,
                system_instruction.strip()[:6000],
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    if voice_mode == "custom_voice":
        schedule_voice_sample_warmup(custom_voice_b64)
    return serialize_avatar_row(row)


class UpdateAvatarRequest(BaseModel):
    name: Optional[str] = None
    role_tagline: Optional[str] = None
    system_instruction: Optional[str] = None
    prebuilt_voice: Optional[str] = None
    voice_mode: Optional[str] = None
    avatar_mode: Optional[str] = None
    builtin_avatar_name: Optional[str] = None


@app.put("/api/avatars/{avatar_id}")
async def update_avatar(avatar_id: str, req: UpdateAvatarRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Avatar not found")

    now = int(time.time())
    name = (req.name if req.name is not None else row["name"]).strip()[:80]
    role_tagline = (req.role_tagline if req.role_tagline is not None else row["role_tagline"]).strip()[:140]
    system_instruction = (req.system_instruction if req.system_instruction is not None else row["system_instruction"]).strip()[:6000]
    prebuilt_voice = (req.prebuilt_voice if req.prebuilt_voice is not None else row["prebuilt_voice"]).strip()[:40]
    voice_mode = req.voice_mode if req.voice_mode in ("custom_voice", "prebuilt") else row["voice_mode"]
    avatar_mode = req.avatar_mode if req.avatar_mode in ("custom_photo", "builtin") else row["avatar_mode"]
    builtin_avatar_name = (req.builtin_avatar_name if req.builtin_avatar_name is not None else row["builtin_avatar_name"]).strip()[:40]

    with conn:
        conn.execute(
            """
            UPDATE avatars
            SET name = ?, role_tagline = ?, system_instruction = ?,
                prebuilt_voice = ?, voice_mode = ?, avatar_mode = ?,
                builtin_avatar_name = ?, updated_at = ?
            WHERE id = ?;
            """,
            (name, role_tagline, system_instruction, prebuilt_voice, voice_mode, avatar_mode, builtin_avatar_name, now, avatar_id),
        )
        updated = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    return serialize_avatar_row(updated)


@app.post("/api/avatars/{avatar_id}/update")
async def update_avatar_multipart(avatar_id: str, request: Request):
    """
    Full multipart update for an existing saved avatar: supports re-uploading the portrait photo,
    recording/uploading a new custom voice WAV, switching to any prebuilt voice, and updating
    the avatar's name, role tagline, and system instructions.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Avatar not found")

    form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=10, max_fields=25)

    def _get_opt_str(key: str) -> Optional[str]:
        val = form_data.get(key)
        return str(val).strip() if isinstance(val, str) else None

    name_in = _get_opt_str("name")
    role_in = _get_opt_str("role_tagline") or _get_opt_str("role_title")
    sys_in = _get_opt_str("system_instruction")
    voice_mode_in = _get_opt_str("voice_mode")
    prebuilt_voice_in = _get_opt_str("prebuilt_voice") or _get_opt_str("prebuilt_voice_name")
    avatar_mode_in = _get_opt_str("avatar_mode")
    builtin_avatar_in = _get_opt_str("builtin_avatar_name")
    preset_photo_id = _get_opt_str("preset_photo_id") or ""
    photo_data_url = _get_opt_str("photo_data_url") or ""
    voice_data_url = _get_opt_str("voice_data_url") or ""

    photo_file = form_data.get("photo_file")
    voice_file = form_data.get("voice_file")

    # 1. Resolve updated photo (or keep existing photo_b64)
    photo_b64 = row["photo_b64"]
    avatar_mode = avatar_mode_in if avatar_mode_in in ("custom_photo", "builtin") else row["avatar_mode"]
    if photo_file is not None and hasattr(photo_file, "read"):
        raw_img = await photo_file.read()
        if raw_img:
            photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(raw_img)).decode()
            avatar_mode = "custom_photo"
    elif photo_data_url and "," in photo_data_url:
        raw_img = base64.b64decode(photo_data_url.split(",", 1)[1])
        photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(raw_img)).decode()
        avatar_mode = "custom_photo"
    elif preset_photo_id:
        preset_map = {
            "preset-aria": PRESETS_DIR / "aria.jpg",
            "preset-marcus": PRESETS_DIR / "marcus.jpg",
            "preset-elena": PRESETS_DIR / "elena.jpg",
        }
        p_path = preset_map.get(preset_photo_id)
        if p_path and p_path.exists():
            photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(p_path.read_bytes())).decode()
            avatar_mode = "custom_photo"

    # 2. Resolve updated voice (new custom WAV, or switch to prebuilt voice, or keep existing)
    custom_voice_b64 = row["custom_voice_b64"]
    voice_mode = voice_mode_in if voice_mode_in in ("custom_voice", "prebuilt") else row["voice_mode"]
    if voice_file is not None and hasattr(voice_file, "read"):
        raw_aud = await voice_file.read()
        if raw_aud:
            custom_voice_b64 = base64.b64encode(normalize_to_wav_24k(raw_aud)).decode()
            voice_mode = "custom_voice"
    elif voice_data_url and "," in voice_data_url:
        raw_aud = base64.b64decode(voice_data_url.split(",", 1)[1])
        custom_voice_b64 = base64.b64encode(normalize_to_wav_24k(raw_aud)).decode()
        voice_mode = "custom_voice"

    name = (name_in if name_in else row["name"])[:80]
    role_tagline = (role_in if role_in is not None else row["role_tagline"])[:140]
    system_instruction = (sys_in if sys_in is not None else row["system_instruction"])[:6000]
    prebuilt_voice = (prebuilt_voice_in if prebuilt_voice_in else row["prebuilt_voice"])[:40]
    builtin_avatar_name = (builtin_avatar_in if builtin_avatar_in else row["builtin_avatar_name"])[:40]
    now = int(time.time())

    with conn:
        conn.execute(
            """
            UPDATE avatars
            SET name = ?, role_tagline = ?, avatar_mode = ?, builtin_avatar_name = ?,
                photo_b64 = ?, photo_mime = 'image/jpeg',
                voice_mode = ?, prebuilt_voice = ?, custom_voice_b64 = ?, custom_voice_mime = 'audio/wav',
                system_instruction = ?, updated_at = ?
            WHERE id = ?;
            """,
            (
                name,
                role_tagline,
                avatar_mode,
                builtin_avatar_name,
                photo_b64,
                voice_mode,
                prebuilt_voice,
                custom_voice_b64,
                system_instruction,
                now,
                avatar_id,
            ),
        )
        updated = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    if voice_mode == "custom_voice":
        schedule_voice_sample_warmup(custom_voice_b64)
    return serialize_avatar_row(updated)



@app.delete("/api/avatars/{avatar_id}")
async def delete_avatar(avatar_id: str):
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM avatars WHERE id = ?;", (avatar_id,))
    conn.close()
    return {"deleted": True, "id": avatar_id}


class ClientLogRequest(BaseModel):
    event: str = ""
    kind: Optional[str] = None
    code: Optional[int] = None
    elapsed_ms: Optional[int] = None
    opened: Optional[bool] = None
    url: Optional[str] = None
    page: Optional[str] = None
    ua: Optional[str] = None


@app.post("/api/client-log")
async def client_log(req: ClientLogRequest, request: Request):
    """Logs browser-side diagnostics (e.g. a live WebSocket that never connected) to the server log."""
    logger.warning(
        "Client report from %s: event=%s kind=%s code=%s elapsed_ms=%s opened=%s ws_url=%s page=%s ua=%s",
        request.client.host if request.client else "?",
        (req.event or "")[:60],
        (req.kind or "")[:40],
        req.code,
        req.elapsed_ms,
        req.opened,
        (req.url or "")[:200],
        (req.page or "")[:200],
        (req.ua or "")[:200],
    )
    return {"logged": True}


@app.get("/api/ws-prime")
async def uberproxy_websocket_prime(request: Request):
    """Chrome + ÜberProxy WebSocket workaround (crbug.com/903513, go/uberproxy-and-http2-connection-reuse).

    Through the Cloudtop ÜberProxy hostname, Chrome often serves this page over an HTTP/2
    connection it originally opened for a *different* *.proxy.googlers.com host. Chrome's SSL
    client-certificate cache is then never populated for this hostname, and WebSocket
    connections (which can't ride on that shared HTTP/2 connection) are aborted by the browser
    before they ever reach this server. Answering 421 Misdirected Request makes Chrome retry
    once on a dedicated connection for this exact hostname; that handshake populates the cache,
    so the /ws/live connection opened right afterwards goes through.
    """
    logger.info(
        "WS prime (421) from %s host=%s",
        request.client.host if request.client else "?",
        request.headers.get("host", ""),
    )
    return Response(status_code=421, headers={"Cache-Control": "no-store"})


class TokenSyncRequest(BaseModel):
    token: Optional[str] = None
    access_token: Optional[str] = None


@app.post("/api/internal/update-token")
async def update_internal_gcp_token(req: TokenSyncRequest):
    """Allows the Cloudtop token daemon to push fresh OAuth2 tokens for cloud-llm-preview1 with zero downtime."""
    tok = (req.token or req.access_token or "").strip()
    if not tok:
        raise HTTPException(status_code=400, detail="Token is required")
    GLOBAL_TOKEN_STATE["token"] = tok
    GLOBAL_TOKEN_STATE["updated_at"] = time.time()
    try:
        Path("/tmp/google_access_token").write_text(tok)
    except Exception:
        pass
    return {"updated": True, "token_length": len(tok), "updated_at": GLOBAL_TOKEN_STATE["updated_at"]}


@app.post("/api/photo-preview")
async def preview_portrait_photo(request: Request):
    """
    Normalizes any uploaded photo (JPG, PNG, WebP, HEIC, etc. up to 50MB) into an exact
    704x1280 (9:16 portrait) JPEG data URL so the UI can immediately display the exact
    formatted portrait before or during avatar creation.
    """
    form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=5, max_fields=10)
    photo_file = form_data.get("photo_file")
    if photo_file is None or not hasattr(photo_file, "read"):
        raise HTTPException(status_code=400, detail="photo_file is required")
    raw_bytes = await photo_file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")
    norm_img = normalize_to_portrait_jpeg(raw_bytes)
    b64 = base64.b64encode(norm_img).decode()
    return {
        "photo_data_url": f"data:image/jpeg;base64,{b64}",
        "width": 704,
        "height": 1280,
    }


@app.post("/api/avatars/{avatar_id}/photo")
async def update_avatar_photo(avatar_id: str, request: Request):
    """
    Directly updates an existing avatar's portrait photo (from the main stage 1-click upload
    or the wizard), normalizing it to 704x1280 (9:16) JPEG and setting `avatar_mode='custom_photo'`.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Avatar not found")

    form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=5, max_fields=10)
    photo_file = form_data.get("photo_file")
    photo_data_url = form_data.get("photo_data_url", "")

    photo_b64 = ""
    if photo_file is not None and hasattr(photo_file, "read"):
        raw_img = await photo_file.read()
        if raw_img:
            photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(raw_img)).decode()
    elif isinstance(photo_data_url, str) and "," in photo_data_url:
        raw_img = base64.b64decode(photo_data_url.split(",", 1)[1])
        photo_b64 = base64.b64encode(normalize_to_portrait_jpeg(raw_img)).decode()

    if not photo_b64:
        conn.close()
        raise HTTPException(status_code=400, detail="Valid photo file is required.")

    now = int(time.time())
    with conn:
        conn.execute(
            """
            UPDATE avatars
            SET photo_b64 = ?, photo_mime = 'image/jpeg', avatar_mode = 'custom_photo', updated_at = ?
            WHERE id = ?;
            """,
            (photo_b64, now, avatar_id),
        )
        updated = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    return serialize_avatar_row(updated)


@app.post("/api/voice-preview")
async def preview_voice_audio(request: Request):
    """
    Normalizes any uploaded local audio file (WAV, MP3, M4A, OGG, AAC, FLAC, WebM, MP4)
    into a 24kHz 16-bit mono PCM WAV data URL so the browser player can preview it
    and verify its duration immediately upon file selection.
    """
    form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=5, max_fields=10)
    voice_file = form_data.get("voice_file")
    if voice_file is None or not hasattr(voice_file, "read"):
        raise HTTPException(status_code=400, detail="voice_file is required")
    raw_bytes = await voice_file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")
    norm_wav = await asyncio.to_thread(normalize_to_wav_24k, raw_bytes)
    duration_s = round(max(0, len(norm_wav) - 44) / (24000 * 2), 1)
    b64 = base64.b64encode(norm_wav).decode("ascii")
    schedule_voice_sample_warmup(b64)
    return {
        "voice_data_url": f"data:audio/wav;base64,{b64}",
        "duration_seconds": duration_s,
        "sample_rate": 24000,
    }


@app.post("/api/avatars/{avatar_id}/voice")
async def update_avatar_voice(avatar_id: str, request: Request):
    """
    Directly updates an existing avatar's custom voice from a local audio file or recording,
    normalizing it to 24kHz 16-bit mono PCM WAV, setting `voice_mode='custom_voice'`, and
    warming the cleaned live voice sample cache.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Avatar not found")

    form_data = await request.form(max_part_size=50 * 1024 * 1024, max_files=5, max_fields=10)
    voice_file = form_data.get("voice_file")
    voice_data_url = form_data.get("voice_data_url", "")

    custom_voice_b64 = ""
    if voice_file is not None and hasattr(voice_file, "read"):
        raw_aud = await voice_file.read()
        if raw_aud:
            norm_wav = await asyncio.to_thread(normalize_to_wav_24k, raw_aud)
            custom_voice_b64 = base64.b64encode(norm_wav).decode("ascii")
    elif isinstance(voice_data_url, str) and "," in voice_data_url:
        raw_aud = base64.b64decode(voice_data_url.split(",", 1)[1])
        norm_wav = await asyncio.to_thread(normalize_to_wav_24k, raw_aud)
        custom_voice_b64 = base64.b64encode(norm_wav).decode("ascii")

    if not custom_voice_b64:
        conn.close()
        raise HTTPException(status_code=400, detail="Valid audio file is required.")

    now = int(time.time())
    with conn:
        conn.execute(
            """
            UPDATE avatars
            SET custom_voice_b64 = ?, custom_voice_mime = 'audio/wav', voice_mode = 'custom_voice', updated_at = ?
            WHERE id = ?;
            """,
            (custom_voice_b64, now, avatar_id),
        )
        updated = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()
    schedule_voice_sample_warmup(custom_voice_b64)
    logger.info(
        "Updated custom voice for '%s' (%s): %d-byte stored WAV",
        updated["name"],
        avatar_id,
        len(base64.b64decode(custom_voice_b64)),
    )
    return serialize_avatar_row(updated)


# ---------------------------------------------------------------------------
# Knowledge Base Ingestion Endpoints (URLs, Files, Notes + Backend Agent)
# ---------------------------------------------------------------------------
class AddUrlKnowledgeRequest(BaseModel):
    url: str
    title: Optional[str] = None


@app.post("/api/avatars/{avatar_id}/knowledge/url")
async def add_knowledge_url(avatar_id: str, req: AddUrlKnowledgeRequest):
    try:
        safe_url = validate_external_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(
                safe_url,
                headers={"User-Agent": "GeminiLiveAvatarStudio-KnowledgeAgent/1.0"},
            )
            resp.raise_for_status()
            html_text = resp.text
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL content: {exc}") from exc

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "footer", "nav"]):
        tag.decompose()
    extracted_title = (req.title or (soup.title.string.strip() if soup.title and soup.title.string else safe_url))[:140]
    clean_text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()[:15000]
    if not clean_text:
        raise HTTPException(status_code=400, detail="No readable text could be extracted from the URL.")

    item_id = f"kn-{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (id, avatar_id, title, source_type, source_ref, content, created_at)
            VALUES (?, ?, ?, 'url', ?, ?, ?);
            """,
            (item_id, avatar_id, extracted_title, safe_url, clean_text, now),
        )
        conn.execute("UPDATE avatars SET updated_at = ? WHERE id = ?;", (now, avatar_id))
    conn.close()
    return {"added": True, "item": {"id": item_id, "title": extracted_title, "source_type": "url", "source_ref": safe_url, "content": clean_text[:300]}}


@app.post("/api/avatars/{avatar_id}/knowledge/file")
async def add_knowledge_file(avatar_id: str, file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    safe_filename = os.path.basename(file.filename)
    ext = Path(safe_filename).suffix.lower()
    if ext not in ALLOWED_KNOWLEDGE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_KNOWLEDGE_EXTS))}",
        )

    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 10MB limit.")

    extracted_text = ""
    if ext == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(raw_bytes))
            pages = [page.extract_text() or "" for page in reader.pages[:40]]
            extracted_text = "\n".join(pages)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse PDF: {exc}") from exc
    elif ext == ".html":
        soup = BeautifulSoup(raw_bytes.decode("utf-8", errors="ignore"), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        extracted_text = soup.get_text(separator=" ")
    else:
        extracted_text = raw_bytes.decode("utf-8", errors="ignore")

    clean_text = re.sub(r"\s+", " ", extracted_text).strip()[:20000]
    if not clean_text:
        raise HTTPException(status_code=400, detail="Uploaded file did not contain readable text.")

    item_id = f"kn-{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (id, avatar_id, title, source_type, source_ref, content, created_at)
            VALUES (?, ?, ?, 'file', ?, ?, ?);
            """,
            (item_id, avatar_id, safe_filename[:140], safe_filename[:140], clean_text, now),
        )
        conn.execute("UPDATE avatars SET updated_at = ? WHERE id = ?;", (now, avatar_id))
    conn.close()
    return {"added": True, "item": {"id": item_id, "title": safe_filename, "source_type": "file", "source_ref": safe_filename, "content": clean_text[:300]}}


class AddNoteKnowledgeRequest(BaseModel):
    title: str
    content: str


@app.post("/api/avatars/{avatar_id}/knowledge/note")
async def add_knowledge_note(avatar_id: str, req: AddNoteKnowledgeRequest):
    title = req.title.strip()[:140] or "Custom Knowledge Note"
    content = req.content.strip()[:20000]
    if not content:
        raise HTTPException(status_code=400, detail="Knowledge note content cannot be empty.")

    item_id = f"kn-{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (id, avatar_id, title, source_type, source_ref, content, created_at)
            VALUES (?, ?, ?, 'note', 'custom-note', ?, ?);
            """,
            (item_id, avatar_id, title, content, now),
        )
        conn.execute("UPDATE avatars SET updated_at = ? WHERE id = ?;", (now, avatar_id))
    conn.close()
    return {"added": True, "item": {"id": item_id, "title": title, "source_type": "note", "source_ref": "custom-note", "content": content[:300]}}


@app.delete("/api/avatars/{avatar_id}/knowledge/{item_id}")
async def delete_knowledge_item(avatar_id: str, item_id: str):
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM knowledge_items WHERE id = ? AND avatar_id = ?;", (item_id, avatar_id))
    conn.close()
    return {"deleted": True, "id": item_id}


class AskAgentRequest(BaseModel):
    query: str


@app.post("/api/avatars/{avatar_id}/ask-agent")
async def ask_knowledge_agent_endpoint(avatar_id: str, req: AskAgentRequest):
    res = await run_backend_knowledge_agent(avatar_id, req.query)
    return res


# ---------------------------------------------------------------------------
# FFmpeg MP4 -> Fragmented MP4 (fMP4) Live Stream Remuxer
# ---------------------------------------------------------------------------
class FFmpegRemuxer:
    """
    Spawns a persistent `ffmpeg` subprocess per WebSocket session to remux plain
    MP4 chunks emitted by `gemini-3.8-live` into fragmented MP4 (`fMP4`) chunks
    ready for browser `MediaSource` (`video/mp4; codecs="avc1.42E01E, mp4a.40.2"`).
    """

    def __init__(self, client_ws: WebSocket):
        self.client_ws = client_ws
        self.proc: Optional[ asyncio.subprocess.Process] = None
        self.reader_task: Optional[asyncio.Task] = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "mp4",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof+separate_moof",
            "-f",
            "mp4",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(self._pump_stdout())

    async def _pump_stdout(self):
        try:
            while self.proc and self.proc.stdout:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                await self.client_ws.send_json(
                    {
                        "type": "video_fmp4",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                )
        except Exception as exc:
            logger.debug("FFmpeg stdout pump ended: %s", exc)

    async def write_mp4_chunk(self, raw_mp4: bytes):
        if not self.proc or self.proc.returncode is not None:
            await self.start()
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(raw_mp4)
                await self.proc.stdin.drain()
            except Exception as exc:
                logger.warning("FFmpeg write_mp4_chunk error, restarting remuxer: %s", exc)
                await self.stop()
                await self.start()
                if self.proc and self.proc.stdin:
                    self.proc.stdin.write(raw_mp4)
                    await self.proc.stdin.drain()

    async def stop(self):
        if self.reader_task:
            self.reader_task.cancel()
            self.reader_task = None
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


# ---------------------------------------------------------------------------
# Build Setup Payload for Gemini 3.8 Live (`BidiGenerateContent`)
# ---------------------------------------------------------------------------
def build_gemini_live_setup(
    avatar_row: sqlite3.Row,
    knowledge_items: List[Dict[str, Any]],
    model_id: str = DEFAULT_LIVE_MODEL,
    force_prebuilt_voice: bool = False,
    force_builtin_avatar: bool = False,
    voice_sample_clean_b64: Optional[str] = None,
) -> Dict[str, Any]:
    full_model = (
        model_id
        if model_id.startswith("projects/")
        else f"projects/{GCP_PROJECT}/locations/{GCP_LOCATION}/publishers/google/models/{model_id}"
    )

    # 1. Construct System Instruction + Attached Knowledge Summary (Direct zero-latency grounding)
    base_sys = (
        (avatar_row["system_instruction"] or f"You are {avatar_row['name']}.")
        + "\n\nSPEAKING STYLE INSTRUCTION: Speak naturally, smoothly, and continuously in a warm, human conversational flow without abrupt pauses."
    )
    if knowledge_items:
        kb_summary_lines = [
            "\n\n=== ATTACHED KNOWLEDGE BASE (DIRECT GROUNDING CONTEXT) ===",
            "All attached knowledge is pre-loaded below so you can answer immediately on the first turn without any tool call delay:",
        ]
        for idx, item in enumerate(knowledge_items[:12], 1):
            kb_summary_lines.append(
                f"\n--- Knowledge Source #{idx}: {item['title']} ({item['source_type']}: {item['source_ref']}) ---\n"
                f"{item['content'][:2200]}"
            )
        full_sys_text = base_sys + "\n".join(kb_summary_lines)
    else:
        full_sys_text = base_sys

    # 2. Configure Voice (`replicatedVoiceConfig` for custom voice OR `prebuiltVoiceConfig`)
    if not force_prebuilt_voice and avatar_row["voice_mode"] == "custom_voice" and avatar_row["custom_voice_b64"]:
        if voice_sample_clean_b64:
            # Cleaned (70 Hz high-pass + light denoise), trimmed to <= 20 s, re-wrapped as WAV.
            voice_config = {
                "replicatedVoiceConfig": {
                    "mimeType": LIVE_VOICE_SAMPLE_MIME,
                    "voiceSampleAudio": voice_sample_clean_b64,
                }
            }
        else:
            voice_config = {
                "replicatedVoiceConfig": {
                    "mimeType": "audio/wav",
                    "voiceSampleAudio": avatar_row["custom_voice_b64"],
                }
            }
    else:
        voice_config = {
            "prebuiltVoiceConfig": {
                "voiceName": avatar_row["prebuilt_voice"] or "Aoede"
            }
        }

    # 3. Configure Avatar (`customizedAvatar` with 704x1280 JPEG OR `avatarName`)
    if not force_builtin_avatar and avatar_row["avatar_mode"] == "custom_photo" and avatar_row["photo_b64"]:
        avatar_config = {
            "customizedAvatar": {
                "imageMimeType": "image/jpeg",
                "imageData": avatar_row["photo_b64"],
            }
        }
    else:
        avatar_config = {
            "avatarName": avatar_row["builtin_avatar_name"] or "Kira"
        }

    return {
        "setup": {
            "model": full_model,
            "generationConfig": {
                "responseModalities": ["VIDEO"],
                "thinkingConfig": {
                    "thinkingBudget": 0,
                },
                "speechConfig": {
                    "voiceConfig": voice_config,
                },
            },
            "avatarConfig": avatar_config,
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                    "silenceDurationMs": 350,
                }
            },
            "systemInstruction": {
                "parts": [{"text": full_sys_text}]
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
    }


# ---------------------------------------------------------------------------
# WebSocket Bridge: Browser <-> FastAPI <-> Vertex AI `gemini-3.8-live`
# ---------------------------------------------------------------------------
@app.websocket("/ws/live/{avatar_id}")
async def live_avatar_websocket(websocket: WebSocket, avatar_id: str):
    await websocket.accept()
    model_id = websocket.query_params.get("model", DEFAULT_LIVE_MODEL)

    conn = get_db()
    avatar_row = conn.execute("SELECT * FROM avatars WHERE id = ?;", (avatar_id,)).fetchone()
    conn.close()

    if not avatar_row:
        await websocket.send_json({"type": "error", "message": f"Avatar '{avatar_id}' not found."})
        await websocket.close()
        return

    knowledge_items = get_avatar_knowledge_items(avatar_id)

    try:
        token = await asyncio.to_thread(get_gcp_access_token)
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"GCP Auth Error: {exc}"})
        await websocket.close()
        return

    ws_url = f"wss://{GCP_LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"

    try:
        await websocket.send_json(
            {
                "type": "status",
                "state": "connecting",
                "message": f"Connecting to Vertex AI {model_id} (project {GCP_PROJECT}, {GCP_LOCATION}) (Custom Avatar: {avatar_row['avatar_mode']}, Voice: {avatar_row['voice_mode']})...",
            }
        )

        custom_voice_requested = avatar_row["voice_mode"] == "custom_voice" and bool(avatar_row["custom_voice_b64"])
        voice_clean_b64 = None
        if custom_voice_requested and LIVE_VOICE_SAMPLE_FORMAT == "clean":
            # Cache hit after warm-up; runs in a worker thread so the event loop never blocks on FFmpeg.
            voice_clean_b64 = await asyncio.to_thread(prepare_live_voice_sample, avatar_row["custom_voice_b64"])

        vertex_ws = None
        first_msg = None
        used_builtin_fallback = False
        applied_force_voice = False
        applied_voice_format = None
        # (force_prebuilt_voice, force_builtin_avatar, send_cleaned_voice_sample)
        attempt_configs = []
        if voice_clean_b64:
            attempt_configs.append((False, False, True))
        attempt_configs += [
            (False, False, False),
            (True, False, False),
            (True, True, False),
        ]
        last_err = None
        for force_voice, force_builtin, use_clean in attempt_configs:
            # On an auth (1008) failure, retry the SAME config once with a refreshed token so an
            # expired token never causes the custom voice / custom photo to be silently dropped.
            for auth_retry in range(2):
                candidate_ws = None
                try:
                    candidate_ws = await websockets.connect(
                        ws_url,
                        additional_headers={"Authorization": f"Bearer {token}"},
                        max_size=50 * 1024 * 1024,
                        ping_interval=20,
                        ping_timeout=20,
                    )
                    setup_payload = build_gemini_live_setup(
                        avatar_row,
                        knowledge_items,
                        model_id=model_id,
                        force_prebuilt_voice=force_voice,
                        force_builtin_avatar=force_builtin,
                        voice_sample_clean_b64=voice_clean_b64 if use_clean else None,
                    )
                    await candidate_ws.send(json.dumps(setup_payload))
                    first_raw = await asyncio.wait_for(candidate_ws.recv(), timeout=20.0)
                    first_msg = json.loads(first_raw)
                    vertex_ws = candidate_ws
                    used_builtin_fallback = force_builtin and (avatar_row["avatar_mode"] == "custom_photo")
                    applied_force_voice = force_voice
                    if custom_voice_requested and not force_voice:
                        applied_voice_format = f"cleaned ({LIVE_VOICE_CLEANUP}) audio/wav" if use_clean else "stored audio/wav"
                    break
                except Exception as attempt_exc:
                    last_err = attempt_exc
                    logger.warning(
                        "Handshake attempt (force_voice=%s, force_builtin=%s, clean_sample=%s, auth_retry=%s) failed: %s",
                        force_voice,
                        force_builtin,
                        use_clean,
                        auth_retry,
                        attempt_exc,
                    )
                    if candidate_ws is not None:
                        try:
                            await candidate_ws.close()
                        except Exception:
                            pass
                    if "1008" in str(attempt_exc) and auth_retry == 0:
                        try:
                            token = await asyncio.to_thread(get_gcp_access_token, True)
                        except Exception:
                            pass
                        continue
                    break
            if vertex_ws is not None:
                break

        if vertex_ws is None or first_msg is None:
            raise RuntimeError(f"Could not establish Vertex AI Live session: {last_err}")

        voice_fallback = custom_voice_requested and applied_force_voice
        if custom_voice_requested and not applied_force_voice:
            voice_applied = "custom_voice"
        else:
            voice_applied = f"prebuilt:{avatar_row['prebuilt_voice'] or 'Aoede'}"
        logger.info(
            "Live session established for '%s' (%s): model=%s project=%s location=%s voice=%s%s, avatar=%s",
            avatar_row["name"],
            avatar_id,
            model_id,
            GCP_PROJECT,
            GCP_LOCATION,
            f"replicatedVoiceConfig (custom voice sample, {applied_voice_format})"
            if voice_applied == "custom_voice"
            else voice_applied,
            " [FALLBACK: custom voice was rejected]" if voice_fallback else "",
            "builtin fallback" if used_builtin_fallback else avatar_row["avatar_mode"],
        )

        async with vertex_ws:
            if "setupComplete" in first_msg:
                await websocket.send_json(
                    {
                        "type": "setup_complete",
                        "session_id": first_msg["setupComplete"].get("sessionId", "live-session"),
                        "avatar_name": avatar_row["name"],
                        "avatar_mode": avatar_row["avatar_mode"],
                        "voice_mode": avatar_row["voice_mode"],
                        "voice_applied": voice_applied,
                        "voice_fallback": voice_fallback,
                        "used_builtin_fallback": used_builtin_fallback,
                        "knowledge_count": len(knowledge_items),
                        "model": model_id,
                        "project": GCP_PROJECT,
                        "location": GCP_LOCATION,
                        "voice_sample_format": applied_voice_format,
                    }
                )
                # Immediately send a 20ms silent 16kHz PCM frame so Vertex AI warms up the custom avatar
                # video stream, FFmpeg emits the fMP4 init segment, and browser MSE starts playing at T=0!
                try:
                    silent_20ms_b64 = base64.b64encode(b"\x00" * 640).decode("ascii")
                    await vertex_ws.send(
                        json.dumps(
                            {
                                "realtimeInput": {
                                    "mediaChunks": [
                                        {
                                            "mimeType": "audio/pcm;rate=16000",
                                            "data": silent_20ms_b64,
                                        }
                                    ]
                                }
                            }
                        )
                    )
                except Exception:
                    pass
            else:
                await websocket.send_json({"type": "status", "state": "info", "message": f"Handshake response: {str(first_msg)[:200]}"})

            async def _emit_background_rag_event(query_text: str):
                """Non-blocking background helper to surface RAG citations in the UI without delaying speech."""
                if not knowledge_items or not query_text.strip():
                    return
                try:
                    agent_res = await run_backend_knowledge_agent(avatar_id, query_text)
                    if agent_res.get("found"):
                        await websocket.send_json(
                            {
                                "type": "knowledge_agent_event",
                                "source": "proactive_rag",
                                "query": query_text,
                                "agent_answer": agent_res["agent_answer"],
                                "citations": agent_res["citations"],
                            }
                        )
                except Exception:
                    pass

            async def client_to_vertex():
                try:
                    while True:
                        data = await websocket.receive_json()
                        mtype = data.get("type")

                        if mtype == "audio_pcm":
                            await vertex_ws.send(
                                json.dumps(
                                    {
                                        "realtimeInput": {
                                            "mediaChunks": [
                                                {
                                                    "mimeType": "audio/pcm;rate=16000",
                                                    "data": data["data"],
                                                }
                                            ]
                                        }
                                    }
                                )
                            )
                        elif mtype == "user_text":
                            user_text = data.get("text", "").strip()
                            if not user_text:
                                continue

                            # 1. Send IMMEDIATELY to Vertex AI with ZERO blocking!
                            await vertex_ws.send(
                                json.dumps(
                                    {
                                        "clientContent": {
                                            "turns": [
                                                {
                                                    "role": "user",
                                                    "parts": [{"text": user_text}],
                                                }
                                            ],
                                            "turnComplete": True,
                                        }
                                    }
                                )
                            )
                            # 2. Fire-and-forget background RAG citation lookup for UI display only
                            if knowledge_items:
                                asyncio.create_task(_emit_background_rag_event(user_text))
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.debug("client_to_vertex loop ended: %s", exc)

            async def vertex_to_client():
                try:
                    async for raw_msg in vertex_ws:
                        msg = json.loads(raw_msg)

                        # 1. Handle Gemini Live Function Calling (`toolCall` -> Backend Knowledge Agent)
                        tool_call = msg.get("toolCall")
                        if tool_call:
                            func_calls = tool_call.get("functionCalls", [])
                            func_responses = []
                            for fc in func_calls:
                                fname = fc.get("name", "")
                                fargs = fc.get("args", {}) or {}
                                fid = fc.get("id", "")
                                query_str = fargs.get("query", "")
                                logger.info("Gemini Live invoked tool %s(query=%r)", fname, query_str)

                                agent_result = await run_backend_knowledge_agent(avatar_id, query_str)
                                await websocket.send_json(
                                    {
                                        "type": "knowledge_agent_event",
                                        "source": "live_tool_call",
                                        "tool_name": fname,
                                        "query": query_str,
                                        "agent_answer": agent_result["agent_answer"],
                                        "citations": agent_result["citations"],
                                    }
                                )
                                func_responses.append(
                                    {
                                        "id": fid,
                                        "name": fname,
                                        "response": {
                                            "result": agent_result["agent_answer"],
                                            "sources": [c["title"] for c in agent_result["citations"]],
                                        },
                                    }
                                )
                            if func_responses:
                                await vertex_ws.send(
                                    json.dumps({"toolResponse": {"functionResponses": func_responses}})
                                )
                            continue

                        # 2. Handle Server Content (Video MP4 chunks, Audio, Transcripts)
                        sc = msg.get("serverContent")
                        if not sc:
                            continue

                        if sc.get("interrupted"):
                            await websocket.send_json({"type": "interrupted"})

                        in_tr = sc.get("inputTranscription")
                        if in_tr and in_tr.get("text"):
                            await websocket.send_json(
                                {
                                    "type": "transcript",
                                    "role": "user",
                                    "text": in_tr["text"],
                                    "finished": bool(in_tr.get("finished")),
                                }
                            )

                        out_tr = sc.get("outputTranscription")
                        if out_tr and out_tr.get("text"):
                            await websocket.send_json(
                                {
                                    "type": "transcript",
                                    "role": "avatar",
                                    "text": out_tr["text"],
                                    "finished": bool(out_tr.get("finished")),
                                }
                            )

                        model_turn = sc.get("modelTurn")
                        if model_turn:
                            for part in model_turn.get("parts", []):
                                if "text" in part and part["text"]:
                                    await websocket.send_json(
                                        {
                                            "type": "transcript",
                                            "role": "avatar_text",
                                            "text": part["text"],
                                            "finished": False,
                                        }
                                    )
                                inline = part.get("inlineData")
                                if inline:
                                    mime = inline.get("mimeType", "")
                                    b64_data = inline.get("data", "")
                                    if mime.startswith("video/mp4") and b64_data:
                                        await websocket.send_json(
                                            {
                                                "type": "video_fmp4",
                                                "data": b64_data,
                                            }
                                        )
                                    elif mime.startswith("audio/") and b64_data:
                                        await websocket.send_json(
                                            {
                                                "type": "audio_pcm",
                                                "mimeType": mime,
                                                "data": b64_data,
                                            }
                                        )

                        if sc.get("turnComplete"):
                            await websocket.send_json({"type": "turn_complete"})

                except websockets.exceptions.ConnectionClosed as closed_exc:
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"Vertex AI Live stream closed ({closed_exc.code}): {closed_exc.reason}",
                            }
                        )
                    except Exception:
                        pass
                except (WebSocketDisconnect, RuntimeError) as exc:
                    # The browser side went away mid-stream (tab closed / session stopped): normal end.
                    logger.info("Browser disconnected during live stream for '%s': %s", avatar_id, exc)
                except Exception as exc:
                    logger.error("vertex_to_client error: %s", exc)
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    except Exception:
                        pass

            t1 = asyncio.create_task(client_to_vertex())
            t2 = asyncio.create_task(vertex_to_client())
            done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()

    except Exception as exc:
        logger.error("Live WebSocket setup failed: %s", exc)
        try:
            await websocket.send_json({"type": "error", "message": f"Live session error: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

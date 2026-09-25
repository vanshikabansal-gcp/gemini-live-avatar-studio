#!/usr/bin/env python3
"""
Automated End-to-End Verification Suite for Gemini 3.8 Live Avatar Studio.
Tests:
1. OAuth2 Token Freshness & Zero 1008 Errors:
   - POST /api/internal/update-token
   - get_gcp_access_token() returning valid ya29... token for cloud-llm-preview1
2. Custom Photo Upload + Custom Voice Creation (POST /api/avatars):
   - Create avatar named "Vanshika" with static/presets/elena.jpg normalized via photo_file
   - Synthesized 24kHz WAV voice file via voice_file
   - Attached knowledge via POST /api/avatars/{id}/knowledge/note ("Project Code: GEMINI-38-VERIFIED")
   - GET /api/avatars/{id} returns has_custom_photo: True, avatar_mode: "custom_photo", and valid data:image/jpeg;base64,... photo_data_url
3. Live WebSocket Session (/ws/live/{id}?model=gemini-3.8-live) End-to-End:
   - setup_complete arrives with ZERO 1008 authentication errors
   - Send user_text question ("What is the Project Code in your attached knowledge?")
   - Verify knowledge_agent_event arrives with "GEMINI-38-VERIFIED", transcript chunks arrive from the avatar, AND video_fmp4 MP4 video chunks arrive from gemini-3.8-live.
"""
import asyncio
import base64
import io
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Dict, Any, List

import httpx
import websockets

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app import get_gcp_access_token  # noqa: E402


def generate_synthesized_24khz_wav(duration_sec: float = 4.0, sample_rate: int = 24000) -> bytes:
    """Generates a valid 24kHz 16-bit mono PCM WAV speech-like harmonic signal."""
    buf = io.BytesIO()
    num_samples = int(duration_sec * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            t = i / sample_rate
            # Fundamental + harmonics with gentle envelope
            env = 0.5 + 0.4 * math.sin(2.0 * math.pi * 3.0 * t)
            val = env * (
                0.45 * math.sin(2.0 * math.pi * 210.0 * t)
                + 0.30 * math.sin(2.0 * math.pi * 420.0 * t)
                + 0.15 * math.sin(2.0 * math.pi * 630.0 * t)
            )
            sample = max(-32767, min(32767, int(val * 14000)))
            frames.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


async def run_verification_for_target(http_base: str, ws_base: str, label: str) -> Dict[str, Any]:
    print(f"\n==================================================================")
    print(f"RUNNING VERIFICATION SUITE AGAINST: {label} ({http_base})")
    print(f"==================================================================")

    results: Dict[str, Any] = {
        "target": label,
        "http_base": http_base,
        "ws_base": ws_base,
        "assertions": [],
        "metrics": {},
    }

    def record_assertion(name: str, passed: bool, detail: str):
        status_str = "PASS" if passed else "FAIL"
        print(f"  [{status_str}] {name}: {detail}")
        results["assertions"].append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            raise AssertionError(f"Assertion failed ({name}): {detail}")

    # -------------------------------------------------------------------------
    # STEP 1: OAuth2 Token Freshness & Zero 1008 Errors
    # -------------------------------------------------------------------------
    token = get_gcp_access_token()
    record_assertion(
        "get_gcp_access_token() returns valid ya29... token",
        bool(token and token.startswith("ya29.") and len(token) > 50),
        f"Token prefix={token[:10]}..., length={len(token)}",
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        tok_resp = await client.post(
            f"{http_base}/api/internal/update-token",
            json={"token": token},
        )
        if tok_resp.status_code == 200:
            tok_data = tok_resp.json()
            record_assertion(
                "POST /api/internal/update-token succeeds",
                tok_resp.status_code == 200 and tok_data.get("updated") is True,
                f"HTTP {tok_resp.status_code}, response={tok_data}",
            )
        elif "127.0.0.1" in http_base or "localhost" in http_base:
            record_assertion(
                "POST /api/internal/update-token succeeds",
                False,
                f"HTTP {tok_resp.status_code}, response={tok_resp.text}",
            )
        else:
            print(f"  [INFO] {http_base}/api/internal/update-token returned HTTP {tok_resp.status_code} (earlier Cloud Run revision)")

        # ---------------------------------------------------------------------
        # STEP 2: Custom Photo Upload + Custom Voice Creation (POST /api/avatars)
        # ---------------------------------------------------------------------
        elena_path = BASE_DIR / "static" / "presets" / "elena.jpg"
        raw_photo_bytes = elena_path.read_bytes()
        raw_wav_bytes = generate_synthesized_24khz_wav(duration_sec=4.0)

        files = {
            "photo_file": ("elena.jpg", raw_photo_bytes, "image/jpeg"),
            "voice_file": ("vanshika_24k.wav", raw_wav_bytes, "audio/wav"),
        }
        form_fields = {
            "name": "Vanshika",
            "role_tagline": "Gemini 3.8 Live Principal Verification Avatar",
            "avatar_mode": "custom_photo",
            "voice_mode": "custom_voice",
            "prebuilt_voice": "Aoede",
            "system_instruction": (
                "You are Vanshika. Whenever asked about the Project Code or attached knowledge, "
                "state the exact Project Code from your attached knowledge clearly and concisely."
            ),
        }

        create_resp = await client.post(
            f"{http_base}/api/avatars",
            data=form_fields,
            files=files,
        )
        create_data = create_resp.json()
        avatar_id = create_data.get("id", "")
        record_assertion(
            "POST /api/avatars creates avatar 'Vanshika' with custom photo & voice",
            create_resp.status_code == 200 and bool(avatar_id) and create_data.get("name") == "Vanshika",
            f"HTTP {create_resp.status_code}, id={avatar_id}, name={create_data.get('name')}, voice_mode={create_data.get('voice_mode')}, has_custom_voice={create_data.get('has_custom_voice')}",
        )

        # Attach knowledge note ("Project Code: GEMINI-38-VERIFIED")
        kn_resp = await client.post(
            f"{http_base}/api/avatars/{avatar_id}/knowledge/note",
            json={
                "title": "Official Verification Spec",
                "content": "Project Code: GEMINI-38-VERIFIED. The system is fully verified on Gemini 3.8 Live.",
            },
        )
        kn_data = kn_resp.json()
        record_assertion(
            "POST /api/avatars/{id}/knowledge/note attaches fact 'Project Code: GEMINI-38-VERIFIED'",
            kn_resp.status_code == 200 and kn_data.get("added") is True and "GEMINI-38-VERIFIED" in kn_data.get("item", {}).get("content", ""),
            f"HTTP {kn_resp.status_code}, item_id={kn_data.get('item', {}).get('id')}",
        )

        # Verify GET /api/avatars/{id}
        get_resp = await client.get(f"{http_base}/api/avatars/{avatar_id}")
        get_data = get_resp.json()
        photo_data_url = get_data.get("photo_data_url", "")
        record_assertion(
            "GET /api/avatars/{id} returns has_custom_photo=True, avatar_mode='custom_photo', and valid data:image/jpeg;base64,...",
            (
                get_resp.status_code == 200
                and get_data.get("has_custom_photo") is True
                and get_data.get("avatar_mode") == "custom_photo"
                and photo_data_url.startswith("data:image/jpeg;base64,")
                and len(photo_data_url) > 10000
            ),
            f"has_custom_photo={get_data.get('has_custom_photo')}, avatar_mode={get_data.get('avatar_mode')}, voice_mode={get_data.get('voice_mode')}, photo_data_url_len={len(photo_data_url)}",
        )

    # -------------------------------------------------------------------------
    # STEP 3: Live WebSocket Session (/ws/live/{id}?model=gemini-3.8-live)
    # -------------------------------------------------------------------------
    ws_url = f"{ws_base}/ws/live/{avatar_id}?model=gemini-3.8-live"
    print(f"  -> Connecting to WebSocket: {ws_url}")

    setup_complete_msg = None
    knowledge_events: List[Dict[str, Any]] = []
    transcript_chunks: List[Dict[str, Any]] = []
    video_fmp4_chunks: List[int] = []
    audio_pcm_chunks: List[int] = []
    auth_1008_errors: List[str] = []
    other_errors: List[str] = []
    turn_complete_received = False

    async with websockets.connect(ws_url, max_size=50 * 1024 * 1024, open_timeout=25) as ws:
        # Wait for setup_complete
        start_t = time.time()
        while time.time() - start_t < 30.0:
            raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "error":
                err_txt = str(msg.get("message", ""))
                if "1008" in err_txt:
                    auth_1008_errors.append(err_txt)
                else:
                    other_errors.append(err_txt)
                break
            elif mtype == "setup_complete":
                setup_complete_msg = msg
                break
            elif mtype == "video_fmp4":
                b64 = msg.get("data", "")
                if b64:
                    video_fmp4_chunks.append(len(base64.b64decode(b64)))

        record_assertion(
            "WebSocket setup_complete arrives with ZERO 1008 authentication errors",
            setup_complete_msg is not None and len(auth_1008_errors) == 0,
            f"setup_complete={setup_complete_msg}, 1008_errors={len(auth_1008_errors)}, other_errors={other_errors}",
        )

        # Send user_text question
        question = "What is the Project Code in your attached knowledge?"
        await ws.send(json.dumps({"type": "user_text", "text": question}))
        print(f"  -> Sent user_text: {question!r}")

        # Collect events until we have knowledge_agent_event, transcript chunks, video_fmp4 chunks, and turn_complete (or timeout)
        turn_start = time.time()
        while time.time() - turn_start < 35.0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "error":
                err_txt = str(msg.get("message", ""))
                if "1008" in err_txt:
                    auth_1008_errors.append(err_txt)
                else:
                    other_errors.append(err_txt)
            elif mtype == "knowledge_agent_event":
                knowledge_events.append(msg)
            elif mtype == "transcript":
                if msg.get("role") in ("avatar", "avatar_text"):
                    transcript_chunks.append(msg)
            elif mtype == "video_fmp4":
                b64 = msg.get("data", "")
                if b64:
                    video_fmp4_chunks.append(len(base64.b64decode(b64)))
            elif mtype == "audio_pcm":
                b64 = msg.get("data", "")
                if b64:
                    audio_pcm_chunks.append(len(base64.b64decode(b64)))
            elif mtype == "turn_complete":
                turn_complete_received = True
                # Wait briefly for any trailing remuxed fMP4 packets from ffmpeg stdout
                await asyncio.sleep(1.0)
                for _ in range(5):
                    try:
                        extra_raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                        extra_msg = json.loads(extra_raw)
                        if extra_msg.get("type") == "video_fmp4":
                            video_fmp4_chunks.append(len(base64.b64decode(extra_msg.get("data", ""))))
                        elif extra_msg.get("type") == "transcript" and extra_msg.get("role") in ("avatar", "avatar_text"):
                            transcript_chunks.append(extra_msg)
                    except Exception:
                        break
                break

    full_transcript = "".join(c.get("text", "") for c in transcript_chunks).strip()
    total_video_bytes = sum(video_fmp4_chunks)

    has_verified_in_ke = any(
        "GEMINI-38-VERIFIED" in json.dumps(ke) for ke in knowledge_events
    )
    record_assertion(
        "knowledge_agent_event arrives with 'GEMINI-38-VERIFIED'",
        len(knowledge_events) > 0 and has_verified_in_ke,
        f"events_count={len(knowledge_events)}, first_event_answer={knowledge_events[0].get('agent_answer')[:120] if knowledge_events else 'NONE'}",
    )

    record_assertion(
        "Avatar transcript chunks arrive from gemini-3.8-live",
        len(transcript_chunks) > 0 and len(full_transcript) > 0,
        f"transcript_chunk_count={len(transcript_chunks)}, full_transcript={full_transcript!r}",
    )

    record_assertion(
        "video_fmp4 MP4 video chunks arrive from gemini-3.8-live",
        len(video_fmp4_chunks) > 0 and total_video_bytes > 10000,
        f"video_fmp4_chunk_count={len(video_fmp4_chunks)}, total_video_bytes={total_video_bytes:,} bytes",
    )

    record_assertion(
        "Zero 1008 authentication errors throughout entire session",
        len(auth_1008_errors) == 0,
        f"auth_1008_errors={len(auth_1008_errors)}",
    )

    results["metrics"] = {
        "avatar_id": avatar_id,
        "session_id": setup_complete_msg.get("session_id") if setup_complete_msg else None,
        "used_builtin_fallback": setup_complete_msg.get("used_builtin_fallback") if setup_complete_msg else None,
        "knowledge_agent_event_count": len(knowledge_events),
        "transcript_chunk_count": len(transcript_chunks),
        "full_transcript": full_transcript,
        "video_fmp4_chunk_count": len(video_fmp4_chunks),
        "total_video_fmp4_bytes": total_video_bytes,
        "audio_pcm_chunk_count": len(audio_pcm_chunks),
        "turn_complete_received": turn_complete_received,
        "auth_1008_error_count": len(auth_1008_errors),
    }
    return results


async def main():
    all_reports = []
    # 1. Run against local server http://127.0.0.1:8080
    local_rep = await run_verification_for_target(
        "http://127.0.0.1:8080",
        "ws://127.0.0.1:8080",
        "Local Studio (http://127.0.0.1:8080)",
    )
    all_reports.append(local_rep)

    print("\n==================================================================")
    print("FINAL VERIFICATION SUMMARY JSON")
    print("==================================================================")
    print(json.dumps(all_reports, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

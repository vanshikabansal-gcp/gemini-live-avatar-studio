# Gemini 3.8 Live Avatar Studio

Real-time Custom Photo & Cloned Voice Avatar Studio powered by **Vertex AI Gemini 3.8 Live** (`gemini-3.8-live-preview`).

## Features
- **Custom Photo Avatars (`customizedAvatar`)**: Upload any portrait photo or capture from webcam — automatically center-cropped and normalized to `704x1280` (9:16) RGB JPEG via FFmpeg.
- **Zero-Shot Voice Cloning (`replicatedVoiceConfig`)**: Record a 10–20s voice sample in the browser (with automatic high-pass rumble removal, FFT denoise, and pause gating) or upload a local audio file (`.wav`, `.mp3`, `.m4a`, `.ogg`, `.flac`). Also supports built-in Gemini voices (`Aoede`, `Puck`, `Kore`, `Charon`, `Fenrir`, `Zephyr`, `Orbit`, `Achernar`).
- **Low-Latency Native fMP4 Streaming**: Direct fragmented MP4 (`video/mp4`) MediaSource Extensions (MSE) streaming with one-shot speech-onset playhead synchronization.
- **Full-Screen Call Mode**: Toggleable live transcript overlay, fit/fill portrait view, and saved-avatar editor.
- **Knowledge Base Grounding**: Attach web URLs, PDFs, Markdown/text files, or custom notes for zero-latency grounding.

## Quick Start (Local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Authenticate with Google Cloud
gcloud auth application-default login

# Start the Studio server on http://localhost:8080
GCP_PROJECT=cloud-llm-preview1 GCP_LOCATION=us-central1 uvicorn app:app --host 0.0.0.0 --port 8080
```

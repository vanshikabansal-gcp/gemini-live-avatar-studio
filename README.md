# Gemini 3.8 Live — Custom Avatar & Voice Studio

Created by **[@vanshikabansal](https://github.com/vanshikabansal-gcp)**

A full-stack, real-time **Custom Photo Avatar & Zero-Shot Voice Cloning Studio** powered by **Vertex AI Gemini 3.8 Live** (`gemini-3.8-live` / `gemini-3.8-live-preview`).

---

## Key Features

- **Custom Photo Avatars (`customizedAvatar`) & Prebuilt Avatars**:
  - Upload your own portrait photo (or take a webcam snapshot) — automatically normalized to `704x1280` (9:16 portrait) RGB JPEG via FFmpeg.
  - Or choose from ready-to-use Prebuilt Studio Avatars (`Aria`, `Marcus`, `Elena`, `Kira`, `Kai`, `Vera`).
- **Zero-Shot Voice Cloning (`replicatedVoiceConfig`) & Prebuilt HD Voices**:
  - Record a 10–15 second voice sample directly in the browser or upload a `.wav`/`.mp3`/`.m4a` file for real-time voice cloning (`24kHz` 16-bit mono PCM WAV).
  - Or select any Prebuilt HD Voice (`Aoede`, `Puck`, `Kore`, `Charon`, `Fenrir`, `Zephyr`, `Orbit`, `Achernar`).
- **Per-Avatar System Instructions & Knowledge Base Grounding**:
  - Configure custom **System Instructions** (persona, tone, or stage script) per avatar directly from the UI.
  - Attach custom **Knowledge Base** sources (Website URLs, PDF/TXT/MD/CSV files, or text notes) per avatar for zero-latency grounded Q&A.
- **Ultra-Low-Latency Streaming & Hardware Lip-Sync**:
  - Direct multiplexed fragmented MP4 (`video/mp4; codecs="avc1.42c01f, mp4a.40.2"`) streaming via browser `MediaSource` Extensions (MSE) with `thinkingBudget: 0` and `100ms` VAD silence detection.
- **Optional Password + Domain Access Gate**:
  - Set `STUDIO_ACCESS_PASSWORD` and `STUDIO_ALLOWED_DOMAINS` to protect your deployment behind a login gate.

---

## 1-Command Deploy to Google Cloud Run

Make sure your Google Cloud project has Vertex AI API enabled (`aiplatform.googleapis.com`):

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"

gcloud run deploy gemini-live-avatar-studio \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory=2Gi \
  --cpu=2 \
  --timeout=3600 \
  --set-env-vars="GCP_PROJECT=${PROJECT_ID},GCP_LOCATION=${REGION}"
```

### Optional: Enable Persistent Cloud Storage & Password Protection

To persist created avatars across Cloud Run container restarts and protect your studio with a password gate:

```bash
export GCS_BUCKET="${PROJECT_ID}-avatar-studio-data"
gcloud storage buckets create "gs://${GCS_BUCKET}" --project="${PROJECT_ID}" --location="${REGION}"

gcloud run services update gemini-live-avatar-studio \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --update-env-vars="GCS_PERSIST_BUCKET=${GCS_BUCKET},STUDIO_ACCESS_PASSWORD=YourStrongPassword,STUDIO_ALLOWED_DOMAINS=google.com"
```

---

## Local Development

### Prerequisites
- Python 3.11+
- `ffmpeg` installed (`sudo apt-get install ffmpeg` or `brew install ffmpeg`)
- Google Cloud SDK (`gcloud auth application-default login`)

### Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Authenticate with your Google Cloud project
gcloud auth application-default login

# Start the Studio server on http://127.0.0.1:8080
GCP_PROJECT="your-gcp-project-id" GCP_LOCATION="us-central1" uvicorn app:app --host 127.0.0.1 --port 8080
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GCP_PROJECT` | *(required)* | Your Google Cloud Project ID with Vertex AI enabled |
| `GCP_LOCATION` | `us-central1` | Vertex AI region |
| `LIVE_MODEL` | `gemini-3.8-live-preview` | Default Gemini Live model (`gemini-3.8-live-preview` or `gemini-3.8-live`) |
| `GCS_PERSIST_BUCKET` | *(optional)* | GCS bucket name to persist `studio.db` across Cloud Run restarts |
| `STUDIO_ACCESS_PASSWORD` | *(optional)* | When set, enables the login gate requiring email + password |
| `STUDIO_ALLOWED_DOMAINS` | `google.com` | Comma-separated list of allowed email domains when password gate is active |

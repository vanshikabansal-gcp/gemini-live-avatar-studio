/* Gemini 3.8 Live — Custom Avatar & Voice Studio Frontend (Uncluttered Studio Edition) */
(function () {
  "use strict";

  const state = {
    config: null,
    avatars: [],
    activeAvatar: null,
    ws: null,
    isLive: false,
    isMicStreaming: false,
    micAudioCtx: null,
    micStream: null,
    micProcessor: null,

    // MediaSource fMP4 video player state
    mediaSource: null,
    sourceBuffer: null,
    videoQueue: [],

    // Builder Modal state
    editingAvatarId: null,
    fullscreenTranscriptEnabled: true,
    builderAvatarMode: "custom_photo",
    builderBuiltinName: "Kira",
    builderPresetId: "preset-aria",
    builderPhotoDataUrl: "",
    uploadedPhotoFile: null,
    usedBuiltinFallback: false,
    builderVoiceMode: "custom_voice",
    builderPrebuiltVoice: "Aoede",
    builderVoiceDataUrl: "",

    // Voice Recorder state
    recAudioCtx: null,
    recStream: null,
    recProcessor: null,
    recSamples: [],
    recStartTime: 0,
    recTimerInterval: null,

    // Webcam state
    webcamStream: null,
  };

  function showToast(message) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    container.appendChild(item);
    setTimeout(() => {
      if (item.parentNode) item.parentNode.removeChild(item);
    }, 4000);
  }

  function switchWorkspaceView(viewId) {
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-view") === viewId);
    });
    document.querySelectorAll(".workspace-view").forEach((pane) => {
      pane.classList.toggle("active", pane.id === viewId);
    });
  }

  function switchWizardStep(stepNumber) {
    const stepStr = String(stepNumber);
    document.querySelectorAll(".wizard-step-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-step") === stepStr);
    });
    document.querySelectorAll(".wizard-pane").forEach((pane) => {
      pane.classList.toggle("active", pane.id === "wizard-step-" + stepStr);
    });
  }

  // -------------------------------------------------------------------------
  // Canvas 9:16 (704x1280) Center-Crop Helper for Uploaded Photos & Webcam
  // -------------------------------------------------------------------------
  function drawImageToPortraitCanvas(imgSource, labelText, isUserUpload) {
    const canvas = document.getElementById("builder-photo-canvas");
    const previewImg = document.getElementById("builder-photo-preview-img");
    if (!canvas) return "";
    const ctx = canvas.getContext("2d");
    const targetW = 704;
    const targetH = 1280;
    canvas.width = targetW;
    canvas.height = targetH;

    const srcW = imgSource.videoWidth || imgSource.naturalWidth || imgSource.width || targetW;
    const srcH = imgSource.videoHeight || imgSource.naturalHeight || imgSource.height || targetH;

    const targetAspect = targetW / targetH;
    const srcAspect = srcW / srcH;

    let sx = 0;
    let sy = 0;
    let sw = srcW;
    let sh = srcH;

    if (srcAspect > targetAspect) {
      sw = srcH * targetAspect;
      sx = (srcW - sw) / 2;
    } else {
      sh = srcW / targetAspect;
      sy = (srcH - sh) / 2;
    }

    ctx.fillStyle = "#0f172a";
    ctx.fillRect(0, 0, targetW, targetH);
    ctx.drawImage(imgSource, sx, sy, sw, sh, 0, 0, targetW, targetH);

    const dataUrl = canvas.toDataURL("image/jpeg", 0.9);
    if (isUserUpload !== false) {
      state.builderPhotoDataUrl = dataUrl;
      state.builderAvatarMode = "custom_photo";
      if (previewImg) {
        previewImg.src = dataUrl;
        previewImg.style.display = "block";
      }
      const stagePoster = document.getElementById("stage-poster-img");
      if (stagePoster) {
        stagePoster.src = dataUrl;
      }
    }

    const statusEl = document.getElementById("builder-photo-status");
    if (statusEl) {
      statusEl.textContent = labelText || "Cropped to 704x1280 (9:16)";
    }
    return dataUrl;
  }

  function loadUrlIntoPortraitCanvas(url, labelText, isUserUpload) {
    const previewImg = document.getElementById("builder-photo-preview-img");
    if (isUserUpload !== false && previewImg) {
      previewImg.src = url;
      previewImg.style.display = "block";
    }
    const img = new Image();
    if (!url.startsWith("data:") && !url.startsWith("blob:")) {
      img.crossOrigin = "anonymous";
    }
    img.onload = function () {
      drawImageToPortraitCanvas(img, labelText, isUserUpload);
    };
    img.src = url;
  }

  function dataUrlToBlob(dataUrl) {
    const parts = dataUrl.split(",");
    const mimeMatch = parts[0].match(/:(.*?);/);
    const mime = mimeMatch ? mimeMatch[1] : "application/octet-stream";
    const bstr = atob(parts[1]);
    let n = bstr.length;
    const u8arr = new Uint8Array(n);
    while (n--) {
      u8arr[n] = bstr.charCodeAt(n);
    }
    return new Blob([u8arr], { type: mime });
  }

  // -------------------------------------------------------------------------
  // Encode Float32 PCM samples into a 24kHz 16-bit Mono RIFF WAV Data URL
  // -------------------------------------------------------------------------
  function encodeWavFromFloat32(samples, sampleRate) {
    const numSamples = samples.length;
    const buffer = new ArrayBuffer(44 + numSamples * 2);
    const view = new DataView(buffer);

    function writeAscii(offset, str) {
      for (let i = 0; i < str.length; i++) {
        view.setUint8(offset + i, str.charCodeAt(i));
      }
    }

    writeAscii(0, "RIFF");
    view.setUint32(4, 36 + numSamples * 2, true);
    writeAscii(8, "WAVE");
    writeAscii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeAscii(36, "data");
    view.setUint32(40, numSamples * 2, true);

    let offset = 44;
    for (let i = 0; i < numSamples; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      offset += 2;
    }

    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    return "data:audio/wav;base64," + btoa(binary);
  }

  // -------------------------------------------------------------------------
  // Render Saved Avatars Ribbon & Active Stage
  // -------------------------------------------------------------------------
  function renderAvatarsList() {
    const listEl = document.getElementById("avatar-list");
    if (!listEl) return;
    listEl.replaceChildren();

    state.avatars.forEach((av) => {
      const card = document.createElement("div");
      card.className = "avatar-card" + (state.activeAvatar && state.activeAvatar.id === av.id ? " active" : "");

      const thumb = document.createElement("img");
      thumb.className = "avatar-card-thumb";
      thumb.src = av.photo_data_url || "/static/presets/aria.jpg";
      thumb.alt = av.name;

      const nameEl = document.createElement("span");
      nameEl.className = "avatar-card-name";
      nameEl.textContent = av.name;

      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "avatar-card-edit-btn";
      editBtn.title = "Edit " + av.name + " (Change Voice or Re-upload Photo)";
      editBtn.textContent = "\u270E";
      editBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        await selectAvatar(av.id);
        if (typeof window.__openEditAvatarModal === "function") {
          window.__openEditAvatarModal(state.activeAvatar || av);
        }
      });

      card.appendChild(thumb);
      card.appendChild(nameEl);
      card.appendChild(editBtn);

      card.addEventListener("click", () => {
        selectAvatar(av.id);
      });

      listEl.appendChild(card);
    });
  }

  async function selectAvatar(avatarId) {
    try {
      const prewarmChanged = !state.activeAvatar || state.activeAvatar.id !== avatarId;
      const resp = await fetch("/api/avatars/" + encodeURIComponent(avatarId));
      if (!resp.ok) return;
      const av = await resp.json();
      state.activeAvatar = av;

      const idx = state.avatars.findIndex((x) => x.id === av.id);
      if (idx >= 0) state.avatars[idx] = av;

      renderAvatarsList();
      renderActiveAvatarStage();
      renderKnowledgeList();

      // Automatically pre-connect & warm up the Gemini 3.8 Live session in the background
      // so there is 0ms handshake delay and 0ms initial GOP lag when the user speaks or types!
      if ((prewarmChanged || !state.isLive) && !state.userManuallyStopped) {
        startLiveSession();
      }
    } catch (err) {
      showToast("Failed to load avatar details.");
    }
  }

  function renderActiveAvatarStage() {
    const av = state.activeAvatar;
    if (!av) return;

    const posterEl = document.getElementById("stage-poster-img");
    const videoEl = document.getElementById("live-avatar-video");
    const nameEl = document.getElementById("stage-avatar-name");
    const badgePhoto = document.getElementById("stage-badge-photo");
    const badgeVoice = document.getElementById("stage-badge-voice");
    const badgeKb = document.getElementById("stage-badge-kb");
    const navKbCount = document.getElementById("nav-kb-count");
    const sysInput = document.getElementById("active-system-instruction");

    const kbLen = (av.knowledge_items || []).length;
    const photoUrl = av.photo_data_url || "/static/presets/aria.jpg";

    if (posterEl) {
      posterEl.src = photoUrl;
    }
    if (videoEl) {
      videoEl.poster = photoUrl;
      if (!state.isLive) {
        videoEl.classList.remove("is-streaming");
      }
    }
    if (nameEl) nameEl.textContent = av.name;
    if (badgePhoto) {
      badgePhoto.textContent = av.avatar_mode === "custom_photo" ? "Custom Photo (9:16)" : "Built-in: " + av.builtin_avatar_name;
    }
    if (badgeVoice) {
      badgeVoice.textContent = av.voice_mode === "custom_voice" ? "Custom Cloned Voice" : "Voice: " + av.prebuilt_voice;
    }
    if (badgeKb) {
      badgeKb.textContent = String(kbLen) + " Knowledge Sources";
    }
    if (navKbCount) {
      navKbCount.textContent = String(kbLen);
    }
    if (sysInput) {
      sysInput.value = av.system_instruction || "";
    }
  }

  function renderKnowledgeList() {
    const listEl = document.getElementById("knowledge-list");
    const countLabel = document.getElementById("kb-count-label");
    if (!listEl) return;
    listEl.replaceChildren();

    const items = (state.activeAvatar && state.activeAvatar.knowledge_items) || [];
    if (countLabel) countLabel.textContent = String(items.length);

    if (items.length === 0) {
      const empty = document.createElement("div");
      empty.className = "subtle-hint";
      empty.textContent = "No links or files attached yet. Add a URL or upload a document on the left.";
      listEl.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "kb-item-card";

      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "kb-item-title";
      title.textContent = item.title;

      const meta = document.createElement("div");
      meta.className = "kb-item-meta";
      meta.textContent = item.source_type.toUpperCase() + " • " + item.source_ref + " — " + (item.content || "").slice(0, 110) + "...";

      left.appendChild(title);
      left.appendChild(meta);

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-xs btn-outline";
      delBtn.textContent = "Remove";
      delBtn.addEventListener("click", async () => {
        if (!state.activeAvatar) return;
        await fetch(
          "/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/knowledge/" + encodeURIComponent(item.id),
          { method: "DELETE" }
        );
        await selectAvatar(state.activeAvatar.id);
        showToast("Knowledge source removed.");
      });

      card.appendChild(left);
      card.appendChild(delBtn);
      listEl.appendChild(card);
    });
  }

  function appendAgentEvent(eventData) {
    // 1. Update Live RAG Banner in Conversation view
    const ragBanner = document.getElementById("live-rag-banner");
    const ragText = document.getElementById("live-rag-text");
    const citeNames = (eventData.citations || []).map((c) => c.title).join(", ");
    const fullSummary = (eventData.agent_answer || "") + (citeNames ? " (Sources: " + citeNames + ")" : "");
    if (ragBanner && ragText && eventData.agent_answer) {
      ragBanner.classList.remove("hidden");
      ragText.textContent = fullSummary;
    }

    // 2. Update Full-Screen Knowledge Agent Pill
    const fsRagPill = document.getElementById("fullscreen-rag-pill");
    if (fsRagPill && eventData.agent_answer) {
      fsRagPill.classList.remove("hidden");
      fsRagPill.textContent = "Knowledge Agent: " + fullSummary.slice(0, 160);
    }

    // 3. Append to Knowledge view agent feed
    const feed = document.getElementById("agent-events-feed");
    if (!feed) return;

    const card = document.createElement("div");
    card.className = "agent-event-item";

    const header = document.createElement("div");
    header.style.fontWeight = "600";
    header.textContent = "Q: " + (eventData.query || "");

    const body = document.createElement("div");
    body.style.marginTop = "4px";
    body.textContent = eventData.agent_answer || "";

    card.appendChild(header);
    card.appendChild(body);
    feed.insertBefore(card, feed.firstChild);
  }

  // -------------------------------------------------------------------------
  // Full-Screen Avatar Call Mode & Fit/Fill Toggle
  // -------------------------------------------------------------------------
  function syncFullScreenUiState(isFull) {
    const stageCard = document.getElementById("avatar-stage-card");
    const btnOverlay = document.getElementById("btn-fullscreen-overlay");
    const btnDock = document.getElementById("btn-fullscreen-dock");
    const btnFit = document.getElementById("btn-toggle-fit");
    const btnFsTranscript = document.getElementById("btn-toggle-fs-transcript");
    const btnFsTranscriptDock = document.getElementById("btn-toggle-fs-transcript-dock");
    const captionOverlay = document.getElementById("fullscreen-caption-overlay");
    const quickChat = document.getElementById("fullscreen-quick-chat");

    if (stageCard) {
      stageCard.classList.toggle("fullscreen-call-mode", isFull);
      if (!isFull) stageCard.classList.remove("fill-screen-mode");
    }
    if (btnOverlay) {
      btnOverlay.textContent = isFull ? "\u2715 Exit Full Screen" : "\u26F6 Full Screen";
    }
    if (btnDock) {
      btnDock.textContent = isFull ? "\u2715 Exit Full Screen" : "\u26F6 Full Screen";
    }
    if (btnFit) {
      btnFit.classList.toggle("hidden", !isFull);
      if (!isFull) btnFit.textContent = "Fill Screen";
    }
    const transcriptLabel = state.fullscreenTranscriptEnabled
      ? "\uD83D\uDCAC Transcript: ON"
      : "\uD83D\uDCAC Transcript: OFF";
    if (btnFsTranscript) {
      btnFsTranscript.classList.toggle("hidden", !isFull);
      btnFsTranscript.classList.toggle("transcript-off", !state.fullscreenTranscriptEnabled);
      btnFsTranscript.textContent = transcriptLabel;
    }
    if (btnFsTranscriptDock) {
      btnFsTranscriptDock.classList.toggle("hidden", !isFull);
      btnFsTranscriptDock.classList.toggle("transcript-off", !state.fullscreenTranscriptEnabled);
      btnFsTranscriptDock.textContent = transcriptLabel;
    }
    if (captionOverlay) {
      captionOverlay.classList.toggle("hidden", !(isFull && state.fullscreenTranscriptEnabled));
    }
    if (quickChat) {
      quickChat.classList.toggle("hidden", !isFull);
    }
  }

  function toggleFullScreenTranscript() {
    state.fullscreenTranscriptEnabled = !state.fullscreenTranscriptEnabled;
    const stageCard = document.getElementById("avatar-stage-card");
    const isFull =
      (stageCard && stageCard.classList.contains("fullscreen-call-mode")) ||
      Boolean(document.fullscreenElement);
    syncFullScreenUiState(isFull);
    showToast(
      state.fullscreenTranscriptEnabled
        ? "Full-screen live transcript enabled"
        : "Full-screen live transcript hidden"
    );
  }

  function toggleAvatarFullScreen() {
    const stageCard = document.getElementById("avatar-stage-card");
    if (!stageCard) return;

    const currentlyFull =
      stageCard.classList.contains("fullscreen-call-mode") || Boolean(document.fullscreenElement);

    if (currentlyFull) {
      syncFullScreenUiState(false);
      if (document.fullscreenElement && document.exitFullscreen) {
        document.exitFullscreen().catch(() => {});
      }
    } else {
      syncFullScreenUiState(true);
      if (stageCard.requestFullscreen) {
        stageCard.requestFullscreen().catch(() => {});
      }
    }
  }

  function toggleFitFillScreen() {
    const stageCard = document.getElementById("avatar-stage-card");
    const btnFit = document.getElementById("btn-toggle-fit");
    if (!stageCard) return;
    const isFill = stageCard.classList.toggle("fill-screen-mode");
    if (btnFit) {
      btnFit.textContent = isFill ? "Fit 9:16 Portrait" : "Fill Screen";
    }
  }

  // -------------------------------------------------------------------------
  // Live Video Streaming (`MediaSource` fMP4) & WebSocket Session
  // -------------------------------------------------------------------------
  function snapToLiveEdgeWithCushion(minLagToSnap, targetCushion = 0.22) {
    const videoEl = document.getElementById("live-avatar-video");
    if (!videoEl || !videoEl.buffered || videoEl.buffered.length === 0) return false;
    try {
      const bufferedEnd = videoEl.buffered.end(videoEl.buffered.length - 1);
      const lag = bufferedEnd - videoEl.currentTime;
      // Leave a 220ms buffer cushion so playback NEVER underruns or stutters
      if (lag > minLagToSnap) {
        videoEl.currentTime = Math.max(0, bufferedEnd - targetCushion);
        if (videoEl.playbackRate !== 1.0) {
          videoEl.playbackRate = 1.0;
        }
        return true;
      }
      if (videoEl.playbackRate !== 1.0) {
        videoEl.playbackRate = 1.0;
      }
    } catch (e) {}
    return false;
  }

  function updatePlayheadServo() {
    const videoEl = document.getElementById("live-avatar-video");
    if (!videoEl || !videoEl.buffered || videoEl.buffered.length === 0) return;
    try {
      // STRICT RULE: While the avatar is actively speaking her answer, allow AT MOST
      // ONE onset snap during the first 110ms (once the speech-onset fMP4 burst lands in SourceBuffer),
      // and then NEVER seek for the entire rest of the turn so speech flows with zero stutters!
      if (state.isAvatarSpeaking) {
        const elapsedSinceOnset = performance.now() - (state.speechStartMs || 0);
        if (!state.speechOnsetSnapped && elapsedSinceOnset >= 75 && elapsedSinceOnset <= 260) {
          if (snapToLiveEdgeWithCushion(0.36, 0.22)) {
            state.speechOnsetSnapped = true;
          }
        }
        if (videoEl.playbackRate !== 1.0) {
          videoEl.playbackRate = 1.0;
        }
        return;
      }

      // While IDLE (not speaking): keep the playhead within ~220ms of bufferedEnd
      // so silent listening frames never pile up ahead of the next turn.
      snapToLiveEdgeWithCushion(0.38, 0.22);
    } catch (e) {}
  }

  function setupMediaSourceVideoPlayer() {
    const videoEl = document.getElementById("live-avatar-video");
    if (!videoEl || !window.MediaSource) return;

    videoEl.preservesPitch = true;
    videoEl.mozPreservesPitch = true;
    videoEl.webkitPreservesPitch = true;
    videoEl.classList.remove("is-streaming");
    if (state.activeAvatar && state.activeAvatar.photo_data_url) {
      videoEl.poster = state.activeAvatar.photo_data_url;
    }

    if (state.servoTimer) {
      clearInterval(state.servoTimer);
      state.servoTimer = null;
    }
    state.servoTimer = setInterval(updatePlayheadServo, 60);

    state.videoQueue = [];
    state.sourceBuffer = null;
    state.isAvatarSpeaking = false;
    state.speechStartMs = 0;
    state.speechOnsetSnapped = false;
    state.currentAvatarBubble = null;
    state.currentAvatarText = "";
    state.mediaSource = new MediaSource();
    videoEl.src = URL.createObjectURL(state.mediaSource);

    state.mediaSource.addEventListener("sourceopen", () => {
      try {
        const nativeMime = 'video/mp4; codecs="avc1.42c01f, mp4a.40.2"';
        const baselineMime = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"';
        const chosenMime = MediaSource.isTypeSupported(nativeMime) ? nativeMime : baselineMime;
        state.sourceBuffer = state.mediaSource.addSourceBuffer(chosenMime);
        // Native gemini-3.8-live fMP4 uses separate video/audio moof boxes with exact tfdt timestamps
        state.sourceBuffer.mode = "segments";
        state.sourceBuffer.addEventListener("updateend", () => {
          flushVideoQueue();
          if (!state.usedBuiltinFallback) {
            videoEl.classList.add("is-streaming");
          }
          if (videoEl.paused) {
            videoEl.play().catch(() => {
              // If browser blocked unmuted autoplay before first user click, play muted until user clicks
              videoEl.muted = true;
              videoEl.play().catch(() => {});
            });
          }
          // Trim old buffered segments > 20s behind currentTime when idle
          try {
            if (
              !state.isAvatarSpeaking &&
              state.sourceBuffer &&
              !state.sourceBuffer.updating &&
              videoEl.buffered.length > 0 &&
              videoEl.currentTime > 25 &&
              videoEl.buffered.start(0) < videoEl.currentTime - 20
            ) {
              state.sourceBuffer.remove(0, videoEl.currentTime - 15);
            }
          } catch (e) {}
        });
        flushVideoQueue();
      } catch (err) {}
    });
  }

  function flushVideoQueue() {
    if (!state.sourceBuffer || state.sourceBuffer.updating || state.videoQueue.length === 0) {
      return;
    }
    try {
      if (state.videoQueue.length === 1) {
        state.sourceBuffer.appendBuffer(state.videoQueue.shift());
      } else {
        let totalLen = 0;
        for (let i = 0; i < state.videoQueue.length; i++) {
          totalLen += state.videoQueue[i].byteLength;
        }
        const merged = new Uint8Array(totalLen);
        let offset = 0;
        while (state.videoQueue.length > 0) {
          const buf = new Uint8Array(state.videoQueue.shift());
          merged.set(buf, offset);
          offset += buf.byteLength;
        }
        state.sourceBuffer.appendBuffer(merged.buffer);
      }
    } catch (err) {}
  }

  function appendTranscriptBubble(role, text, isFinished) {
    const log = document.getElementById("transcript-log");
    if (!log || !text) return;

    const emptyState = log.querySelector(".empty-chat-state");
    if (emptyState) emptyState.remove();

    if (role === "avatar" || role === "avatar_text") {
      const avName = state.activeAvatar ? state.activeAvatar.name : "Avatar";
      if (!state.currentAvatarBubble) {
        const div = document.createElement("div");
        div.className = "msg-bubble msg-avatar";
        state.currentAvatarBubble = div;
        state.currentAvatarText = text;
        div.textContent = avName + ": " + state.currentAvatarText;
        log.appendChild(div);
      } else {
        state.currentAvatarText += text;
        state.currentAvatarBubble.textContent = avName + ": " + state.currentAvatarText;
      }
      log.scrollTop = log.scrollHeight;
      const fsCaption = document.getElementById("fullscreen-caption-text");
      if (fsCaption) {
        fsCaption.textContent = avName + ": " + state.currentAvatarText;
      }
      if (isFinished) {
        state.currentAvatarBubble = null;
        state.currentAvatarText = "";
      }
      return;
    }

    state.currentAvatarBubble = null;
    state.currentAvatarText = "";

    const div = document.createElement("div");
    let displayLine = text;
    if (role === "user") {
      div.className = "msg-bubble msg-user";
      div.textContent = text;
      displayLine = "You: " + text;
    } else {
      div.className = "msg-bubble msg-system";
      div.textContent = text;
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;

    const fsCaption = document.getElementById("fullscreen-caption-text");
    if (fsCaption) {
      fsCaption.textContent = displayLine;
    }
  }

  function setSessionUiStatus(mode, text) {
    const dot = document.getElementById("session-status-dot");
    const label = document.getElementById("session-status-text");
    const btnStart = document.getElementById("btn-connect-live");
    const btnEnd = document.getElementById("btn-disconnect-live");
    const pill = document.getElementById("video-live-pill");

    if (dot) {
      dot.className =
        "status-dot " + (mode === "live" ? "status-live" : mode === "connecting" ? "status-connecting" : "status-idle");
    }
    if (label) label.textContent = text;
    if (btnStart && btnEnd) {
      if (mode === "live" || mode === "connecting") {
        btnStart.classList.add("hidden");
        btnEnd.classList.remove("hidden");
      } else {
        btnStart.classList.remove("hidden");
        btnEnd.classList.add("hidden");
      }
    }
    if (pill) {
      if (mode === "live") pill.classList.remove("hidden");
      else pill.classList.add("hidden");
    }
  }

  // True when the page is served through the Cloudtop ÜberProxy (vblinux.c.googlers.com:8080,
  // which Chrome turns into a https://…proxy.googlers.com URL). There, Chrome can abort the live
  // WebSocket before it ever reaches the server (crbug.com/903513) unless it is primed first.
  function isBehindCloudtopProxy() {
    const host = window.location.hostname || "";
    if (/(^|\.)(googlers\.com|corp\.google\.com|googleplex\.com)$/i.test(host)) return true;
    return new URLSearchParams(window.location.search).get("wsprime") === "1";
  }

  // The server answers 421 Misdirected Request: Chrome then retries once on a dedicated connection
  // for this exact hostname, which fills its client-certificate cache so the WebSocket opened right
  // after is not aborted. Never blocks for more than 3s.
  async function primeCloudtopProxyForWebSocket() {
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), 3000) : null;
    try {
      await fetch("/api/ws-prime?t=" + Date.now(), {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller ? controller.signal : undefined,
      });
    } catch (e) {
      // Ignore: the WebSocket attempt (and its watchdog) still runs.
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function startLiveSession(isAutoRetry = false) {
    if (!state.activeAvatar) {
      showToast("Select or create an avatar first.");
      return;
    }
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    if (isAutoRetry !== true) state.wsAutoRetried = false;
    // Every start/stop bumps this id, so an attempt that is still priming can't open a stale socket.
    const attemptId = (state.connectAttemptId || 0) + 1;
    state.connectAttemptId = attemptId;

    state.usedBuiltinFallback = false;
    setupMediaSourceVideoPlayer();
    const modelSel = document.getElementById("model-selector");
    const modelId = modelSel ? modelSel.value : "gemini-3.8-live";
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl =
      proto +
      "//" +
      window.location.host +
      "/ws/live/" +
      encodeURIComponent(state.activeAvatar.id) +
      "?model=" +
      encodeURIComponent(modelId);

    setSessionUiStatus("connecting", "Connecting...");
    appendTranscriptBubble("system", "Connecting to Vertex AI " + modelId + " for " + state.activeAvatar.name + "...");

    const connectStartedAt = performance.now();
    const behindProxy = isBehindCloudtopProxy();
    if (behindProxy) {
      await primeCloudtopProxyForWebSocket();
      if (state.connectAttemptId !== attemptId) return; // superseded or stopped while priming
    }

    const ws = new WebSocket(wsUrl);
    state.ws = ws;

    // Watchdog: the server sends a status message right after accepting the socket, so if nothing
    // arrives (proxy dropped / blocked the WebSocket) show a clear error and retry once instead of
    // hanging on "Connecting..." forever.
    let wsOpened = false;
    let wsGotMessage = false;
    let wsFailureReported = false;
    function reportLiveConnectFailure(kind, code) {
      if (wsFailureReported || state.ws !== ws) return;
      wsFailureReported = true;
      const elapsedMs = Math.round(performance.now() - connectStartedAt);
      const detail = kind === "closed" ? "closed with code " + code : "no response after " + Math.round(elapsedMs / 1000) + "s";
      const willRetry = !state.wsAutoRetried;
      let notice;
      if (willRetry) {
        notice = "Notice: The live connection was blocked before reaching the server (" + detail + "). Retrying automatically...";
      } else if (behindProxy) {
        notice =
          "Notice: Chrome is still blocking the live connection on the Cloudtop proxy (" + detail + "). " +
          "Fix: open chrome://net-internals/#sockets, click \"Flush socket pools\", then reload this page (Ctrl+Shift+R). " +
          "Always-works option: on your Mac run  gcert  then  ssh -L 8080:127.0.0.1:8080 vblinux.c.googlers.com  and open http://localhost:8080";
      } else {
        notice =
          "Notice: The live connection couldn't reach the Studio server (" + detail + "). " +
          "Make sure the local server on port 8080 is running, then press Start again.";
      }
      setSessionUiStatus(willRetry ? "connecting" : "idle", willRetry ? "Retrying..." : "Connection failed");
      appendTranscriptBubble("system", notice);
      showToast("Live connection failed (" + detail + ")" + (willRetry ? " \u2014 retrying..." : ""));
      fetch("/api/client-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event: "ws_connect_failed",
          kind: kind,
          code: code,
          elapsed_ms: elapsedMs,
          opened: wsOpened,
          url: wsUrl,
          page: window.location.href,
          ua: navigator.userAgent,
        }),
      }).catch(() => {});
      if (willRetry) {
        state.wsAutoRetried = true;
        setTimeout(() => {
          if (state.ws === ws && !state.userManuallyStopped) startLiveSession(true);
        }, 1000);
      }
    }
    const connectWatchdog = setTimeout(() => {
      if (!wsGotMessage) {
        reportLiveConnectFailure(wsOpened ? "no_data" : "timeout", null);
        try { ws.close(); } catch (e) {}
      }
    }, 10000);
    ws.onopen = () => {
      wsOpened = true;
    };

    ws.onmessage = (evt) => {
      if (!wsGotMessage) {
        wsGotMessage = true;
        clearTimeout(connectWatchdog);
      }
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === "setup_complete") {
          state.isLive = true;
          state.wsAutoRetried = false;
          state.usedBuiltinFallback = Boolean(msg.used_builtin_fallback);
          setSessionUiStatus("live", "Live Connected");

          // Reflect the voice the live session is ACTUALLY using (not just what is saved).
          const badgeVoiceEl = document.getElementById("stage-badge-voice");
          const appliedVoice = String(msg.voice_applied || "");
          let voiceNote = "";
          if (appliedVoice === "custom_voice") {
            if (badgeVoiceEl) badgeVoiceEl.textContent = "\u2713 Custom Cloned Voice (active)";
            voiceNote = " Your custom cloned voice is active.";
          } else if (msg.voice_fallback) {
            const fallbackName = appliedVoice.replace("prebuilt:", "") || "Aoede";
            if (badgeVoiceEl) badgeVoiceEl.textContent = "\u26A0 Fallback Voice: " + fallbackName;
            appendTranscriptBubble(
              "system",
              "Notice: Your custom voice sample was rejected for this session, so the fallback voice '" +
                fallbackName +
                "' is being used. Try reconnecting, or re-record your voice sample (clear speech, quiet room)."
            );
            showToast("Custom voice not applied \u2014 using fallback voice " + fallbackName);
          }

          const cloudNote =
            msg.model && msg.project
              ? " Model " + msg.model + " \u00B7 project " + msg.project + " (" + (msg.location || "") + ")."
              : "";
          appendTranscriptBubble(
            "system",
            "Connected! Speak into your microphone or type a message below." + voiceNote + cloudNote
          );
          showToast("Connected to Gemini 3.8 Live!");
          if (state.pendingTextOnConnect) {
            const pending = state.pendingTextOnConnect;
            state.pendingTextOnConnect = null;
            appendTranscriptBubble("user", pending);
            ws.send(JSON.stringify({ type: "user_text", text: pending }));
          }
        } else if (msg.type === "video_fmp4" && msg.data) {
          const binStr = atob(msg.data);
          const bytes = new Uint8Array(binStr.length);
          for (let i = 0; i < binStr.length; i++) {
            bytes[i] = binStr.charCodeAt(i);
          }
          state.videoQueue.push(bytes.buffer);
          flushVideoQueue();
        } else if (msg.type === "transcript") {
          if (msg.role === "user") {
            state.isAvatarSpeaking = false;
            state.speechStartMs = 0;
            state.speechOnsetSnapped = false;
            updatePlayheadServo();
          } else {
            const videoEl = document.getElementById("live-avatar-video");
            if (videoEl && videoEl.muted) {
              videoEl.muted = false;
            }
            if (!state.isAvatarSpeaking) {
              // Snap past any pre-speech silent frames and record speechStartMs so
              // the 75-110ms onset check catches the initial fMP4 burst once, then locks!
              snapToLiveEdgeWithCushion(0.32, 0.22);
              state.isAvatarSpeaking = true;
              state.speechStartMs = performance.now();
              state.speechOnsetSnapped = false;
            }
          }
          if (msg.text) {
            appendTranscriptBubble(
              msg.role === "user" ? "user" : "avatar",
              msg.text,
              Boolean(msg.finished)
            );
          }
        } else if (msg.type === "turn_complete") {
          state.isAvatarSpeaking = false;
          state.speechStartMs = 0;
          state.speechOnsetSnapped = false;
          state.currentAvatarBubble = null;
          state.currentAvatarText = "";
          updatePlayheadServo();
        } else if (msg.type === "interrupted") {
          state.isAvatarSpeaking = false;
          state.speechStartMs = 0;
          state.speechOnsetSnapped = false;
          state.currentAvatarBubble = null;
          state.currentAvatarText = "";
          updatePlayheadServo();
        } else if (msg.type === "knowledge_agent_event") {
          appendAgentEvent(msg);
        } else if (msg.type === "error") {
          appendTranscriptBubble("system", "Notice: " + msg.message);
          showToast(msg.message);
        }
      } catch (e) {}
    };

    ws.onclose = (evt) => {
      clearTimeout(connectWatchdog);
      // Ignore close events from a superseded or manually-stopped socket, so reconnecting never
      // tears down the NEW session's playhead servo / live state.
      if (state.ws !== ws) return;
      if (!wsGotMessage) {
        reportLiveConnectFailure("closed", evt ? evt.code : null);
      }
      state.isLive = false;
      state.isAvatarSpeaking = false;
      if (state.servoTimer) {
        clearInterval(state.servoTimer);
        state.servoTimer = null;
      }
      const videoEl = document.getElementById("live-avatar-video");
      if (videoEl) videoEl.classList.remove("is-streaming");
      stopMicStreaming();
      if (wsGotMessage) setSessionUiStatus("idle", "Ready");
    };
  }

  function stopLiveSession() {
    state.userManuallyStopped = true;
    state.connectAttemptId = (state.connectAttemptId || 0) + 1; // cancel any start still priming
    stopMicStreaming();
    if (state.servoTimer) {
      clearInterval(state.servoTimer);
      state.servoTimer = null;
    }
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    state.isLive = false;
    state.isAvatarSpeaking = false;
    const videoEl = document.getElementById("live-avatar-video");
    if (videoEl) {
      videoEl.classList.remove("is-streaming");
      try { videoEl.pause(); } catch (e) {}
    }
    setSessionUiStatus("idle", "Ready");
  }

  function sendUserText() {
    const input = document.getElementById("chat-text-input");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    state.userManuallyStopped = false;
    const videoEl = document.getElementById("live-avatar-video");
    if (videoEl) {
      videoEl.muted = false;
      if (videoEl.paused) videoEl.play().catch(() => {});
    }

    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      state.pendingTextOnConnect = text;
      input.value = "";
      showToast("Starting Live Session...");
      startLiveSession();
      return;
    }

    state.isAvatarSpeaking = false;
    updatePlayheadServo();
    appendTranscriptBubble("user", text);
    state.ws.send(JSON.stringify({ type: "user_text", text: text }));
    input.value = "";
  }

  // -------------------------------------------------------------------------
  // Live 16kHz Microphone Streaming
  // -------------------------------------------------------------------------
  async function toggleMicStreaming() {
    if (state.isMicStreaming) {
      stopMicStreaming();
      return;
    }
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      showToast("Start the Live Video Session first before turning on the microphone.");
      return;
    }
    try {
      state.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      state.micAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 16000,
        latencyHint: "interactive",
      });
      const source = state.micAudioCtx.createMediaStreamSource(state.micStream);
      const proc = state.micAudioCtx.createScriptProcessor(1024, 1, 1);
      state.micProcessor = proc;

      proc.onaudioprocess = (e) => {
        if (!state.isMicStreaming || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
        const inputData = e.inputBuffer.getChannelData(0);
        const pcm16 = new Int16Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
          const s = Math.max(-1, Math.min(1, inputData[i]));
          pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        const u8 = new Uint8Array(pcm16.buffer);
        let bin = "";
        for (let i = 0; i < u8.length; i++) {
          bin += String.fromCharCode(u8[i]);
        }
        state.ws.send(JSON.stringify({ type: "audio_pcm", data: btoa(bin) }));
      };

      source.connect(proc);
      proc.connect(state.micAudioCtx.destination);
      state.isMicStreaming = true;

      const btnMic = document.getElementById("btn-toggle-mic");
      const micLabel = document.getElementById("mic-label");
      if (btnMic) btnMic.classList.add("recording");
      if (micLabel) micLabel.textContent = "Mic LIVE";
      showToast("Microphone streaming enabled!");
    } catch (err) {
      showToast("Microphone permission unavailable.");
    }
  }

  function stopMicStreaming() {
    state.isMicStreaming = false;
    if (state.micProcessor) {
      try { state.micProcessor.disconnect(); } catch (e) {}
      state.micProcessor = null;
    }
    if (state.micStream) {
      state.micStream.getTracks().forEach((t) => t.stop());
      state.micStream = null;
    }
    if (state.micAudioCtx) {
      try { state.micAudioCtx.close(); } catch (e) {}
      state.micAudioCtx = null;
    }
    const btnMic = document.getElementById("btn-toggle-mic");
    const micLabel = document.getElementById("mic-label");
    if (btnMic) btnMic.classList.remove("recording");
    if (micLabel) micLabel.textContent = "Mic Off";
  }

  // -------------------------------------------------------------------------
  // Direct Photo Upload Helper (Applies photo to active avatar & stage)
  // -------------------------------------------------------------------------
  async function applyUploadedPhotoToActiveAvatar(file, photoDataUrl) {
    if (!state.activeAvatar) return null;
    const fd = new FormData();
    if (file) {
      fd.append("photo_file", file, file.name || "custom_photo.jpg");
    } else if (photoDataUrl) {
      fd.append("photo_data_url", photoDataUrl);
    } else {
      return null;
    }
    try {
      const resp = await fetch("/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/photo", {
        method: "POST",
        body: fd,
      });
      if (resp.ok) {
        const updated = await resp.json();
        state.activeAvatar = updated;
        const idx = state.avatars.findIndex((x) => x.id === updated.id);
        if (idx >= 0) state.avatars[idx] = updated;
        renderAvatarsList();
        renderActiveAvatarStage();
        if (state.isLive) {
          showToast("Reconnecting Live Session with your new photo...");
          startLiveSession();
        }
        return updated;
      }
    } catch (e) {}
    return null;
  }

  // -------------------------------------------------------------------------
  // Builder Modal: 3-Step Wizard (Photo -> Voice -> Name & Save)
  // -------------------------------------------------------------------------
  function initBuilderModal() {
    const modal = document.getElementById("builder-modal");
    const openBtn = document.getElementById("btn-open-builder");
    const openSideBtn = document.getElementById("btn-new-avatar-side");
    const editHeaderBtn = document.getElementById("btn-edit-avatar-header");
    const editRibbonBtn = document.getElementById("btn-edit-avatar-ribbon");
    const editStageBtn = document.getElementById("btn-edit-active-avatar");
    const closeBtn = document.getElementById("btn-close-builder");
    const stageDirectInput = document.getElementById("stage-direct-photo-input");

    if (stageDirectInput) {
      stageDirectInput.addEventListener("change", async (e) => {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        const objectUrl = URL.createObjectURL(file);
        const posterEl = document.getElementById("stage-poster-img");
        if (posterEl) posterEl.src = objectUrl;
        showToast("Uploading & applying your photo to stage...");
        const updated = await applyUploadedPhotoToActiveAvatar(file, "");
        if (updated) {
          showToast("Your custom photo is now active on stage!");
        }
      });
    }

    function openCreateModal() {
      state.editingAvatarId = null;
      state.uploadedPhotoFile = null;
      state.builderPhotoDataUrl = "";
      state.builderVoiceDataUrl = "";
      state.builderAvatarMode = "custom_photo";
      state.builderPresetId = "";

      const titleEl = document.getElementById("builder-modal-title");
      const subEl = document.getElementById("builder-modal-subtitle");
      const btnSave = document.getElementById("btn-save-new-avatar");
      const nameIn = document.getElementById("builder-name");
      const tagIn = document.getElementById("builder-tagline");
      const sysIn = document.getElementById("builder-instructions");

      if (titleEl) titleEl.textContent = "Create Custom Photo & Voice Avatar";
      if (subEl) subEl.textContent = "Saved avatars can be reused anytime with custom knowledge and instructions";
      if (btnSave) btnSave.textContent = "Save Reusable Avatar to Studio Library";
      if (nameIn) nameIn.value = "";
      if (tagIn) tagIn.value = "";
      if (sysIn) sysIn.value = "";

      if (modal) modal.classList.remove("hidden");
      switchWizardStep(1);
      const stalePreviewImg = document.getElementById("builder-photo-preview-img");
      if (stalePreviewImg) stalePreviewImg.style.display = "none";
      const defaultUrl =
        (state.activeAvatar && state.activeAvatar.photo_data_url) || "/static/presets/aria.jpg";
      loadUrlIntoPortraitCanvas(
        defaultUrl,
        "Default 9:16 portrait ready — upload your photo on the left!",
        false
      );
      populateBuilderPresetsAndVoices();
    }

    function openEditAvatarModal(av, startStep) {
      const target = av || state.activeAvatar;
      if (!target) {
        showToast("Select a saved avatar first.");
        return;
      }
      state.editingAvatarId = target.id;
      state.uploadedPhotoFile = null;
      state.builderPhotoDataUrl = "";
      state.builderVoiceDataUrl = "";
      state.builderPresetId = "";
      state.builderAvatarMode = target.avatar_mode || "custom_photo";
      state.builderBuiltinName = target.builtin_avatar_name || "Kira";
      state.builderVoiceMode = target.voice_mode || "prebuilt";
      state.builderPrebuiltVoice = target.prebuilt_voice || "Aoede";

      const titleEl = document.getElementById("builder-modal-title");
      const subEl = document.getElementById("builder-modal-subtitle");
      const btnSave = document.getElementById("btn-save-new-avatar");
      const nameIn = document.getElementById("builder-name");
      const tagIn = document.getElementById("builder-tagline");
      const sysIn = document.getElementById("builder-instructions");

      if (titleEl) titleEl.textContent = "Edit Saved Avatar: " + target.name;
      if (subEl) subEl.textContent = "Re-upload a new photo, record or pick a different voice, or update name & instructions";
      if (btnSave) btnSave.textContent = "\u2713 Save Changes to '" + target.name + "'";
      if (nameIn) nameIn.value = target.name || "";
      if (tagIn) tagIn.value = target.role_tagline || "";
      if (sysIn) sysIn.value = target.system_instruction || "";

      if (modal) modal.classList.remove("hidden");
      switchWizardStep(startStep || 1);

      const tabRecVoice = document.getElementById("tab-btn-record-voice");
      const tabPreVoice = document.getElementById("tab-btn-prebuilt-voice");
      const paneRecVoice = document.getElementById("voice-pane-record");
      const panePreVoice = document.getElementById("voice-pane-prebuilt");
      const audioPlayer = document.getElementById("recorded-voice-audio");

      if (audioPlayer) {
        audioPlayer.src = target.custom_voice_data_url || "";
      }
      if (tabRecVoice && tabPreVoice && paneRecVoice && panePreVoice) {
        if (target.voice_mode === "prebuilt") {
          tabPreVoice.classList.add("active");
          tabRecVoice.classList.remove("active");
          panePreVoice.classList.add("active");
          paneRecVoice.classList.remove("active");
        } else {
          tabRecVoice.classList.add("active");
          tabPreVoice.classList.remove("active");
          paneRecVoice.classList.add("active");
          panePreVoice.classList.remove("active");
        }
      }

      // Display-only: show the current photo WITHOUT flagging it as a new upload, so saving a
      // voice-only change never re-uploads (and re-compresses) the existing photo.
      const currentPhotoUrl = target.photo_data_url || "/static/presets/aria.jpg";
      const previewImgEl = document.getElementById("builder-photo-preview-img");
      if (previewImgEl) {
        previewImgEl.src = currentPhotoUrl;
        previewImgEl.style.display = "block";
      }
      loadUrlIntoPortraitCanvas(
        currentPhotoUrl,
        "Current Photo: " + target.name + " (upload a new image on the left to replace)",
        false
      );
      populateBuilderPresetsAndVoices();
    }

    window.__openEditAvatarModal = openEditAvatarModal;

    function closeModal() {
      if (modal) modal.classList.add("hidden");
      stopWebcam();
    }

    if (openBtn) openBtn.addEventListener("click", openCreateModal);
    if (openSideBtn) openSideBtn.addEventListener("click", openCreateModal);
    if (editHeaderBtn) editHeaderBtn.addEventListener("click", () => openEditAvatarModal(state.activeAvatar, 1));
    if (editRibbonBtn) editRibbonBtn.addEventListener("click", () => openEditAvatarModal(state.activeAvatar, 1));
    if (editStageBtn) editStageBtn.addEventListener("click", () => openEditAvatarModal(state.activeAvatar, 1));
    if (closeBtn) closeBtn.addEventListener("click", closeModal);

    // Stepper navigation buttons
    document.querySelectorAll(".wizard-step-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchWizardStep(btn.getAttribute("data-step"));
      });
    });

    document.querySelectorAll(".wizard-next-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchWizardStep(btn.getAttribute("data-next"));
      });
    });

    // Photo source tabs
    const tabUpload = document.getElementById("tab-btn-upload-photo");
    const tabWebcam = document.getElementById("tab-btn-webcam-photo");
    const tabPreset = document.getElementById("tab-btn-preset-photo");
    const paneUpload = document.getElementById("photo-pane-upload");
    const paneWebcam = document.getElementById("photo-pane-webcam");
    const panePreset = document.getElementById("photo-pane-preset");

    function activatePhotoTab(which) {
      [tabUpload, tabWebcam, tabPreset].forEach((b) => b && b.classList.remove("active"));
      [paneUpload, paneWebcam, panePreset].forEach((p) => p && p.classList.remove("active"));
      if (which === "upload") {
        tabUpload.classList.add("active");
        paneUpload.classList.add("active");
      } else if (which === "webcam") {
        tabWebcam.classList.add("active");
        paneWebcam.classList.add("active");
      } else {
        tabPreset.classList.add("active");
        panePreset.classList.add("active");
      }
    }

    if (tabUpload) tabUpload.addEventListener("click", () => activatePhotoTab("upload"));
    if (tabWebcam) tabWebcam.addEventListener("click", () => activatePhotoTab("webcam"));
    if (tabPreset) tabPreset.addEventListener("click", () => activatePhotoTab("preset"));

    // Custom Photo File Picker + Drag & Drop
    const photoInput = document.getElementById("builder-photo-input");
    const uploadBox = document.querySelector(".upload-photo-box");
    const btnApplyStep1 = document.getElementById("btn-apply-photo-step1");

    async function handlePhotoFileSelected(file) {
      if (!file) return;
      state.uploadedPhotoFile = file;
      state.builderAvatarMode = "custom_photo";

      // 1. Immediate local preview via Object URL + FileReader
      const objUrl = URL.createObjectURL(file);
      loadUrlIntoPortraitCanvas(objUrl, "Uploaded '" + file.name + "' (9:16)", true);
      const posterEl = document.getElementById("stage-poster-img");
      if (posterEl) posterEl.src = objUrl;

      // 2. Server-side FFmpeg 704x1280 9:16 normalization preview + immediate stage apply
      try {
        const fd = new FormData();
        fd.append("photo_file", file, file.name || "custom_photo.jpg");
        const resp = await fetch("/api/photo-preview", { method: "POST", body: fd });
        if (resp.ok) {
          const data = await resp.json();
          if (data.photo_data_url) {
            state.builderPhotoDataUrl = data.photo_data_url;
            loadUrlIntoPortraitCanvas(data.photo_data_url, "Uploaded '" + file.name + "' (9:16)", true);
          }
        }
        // Also immediately apply to the currently active avatar so even if the modal is closed,
        // the user's uploaded photo is persisted and displayed on the main stage!
        const updated = await applyUploadedPhotoToActiveAvatar(file, state.builderPhotoDataUrl);
        if (updated && updated.photo_data_url) {
          state.builderPhotoDataUrl = updated.photo_data_url;
          const previewImg = document.getElementById("builder-photo-preview-img");
          if (previewImg) {
            previewImg.src = updated.photo_data_url;
            previewImg.style.display = "block";
          }
        }
      } catch (e) {}

      showToast("Custom photo uploaded, normalized to 9:16 & applied to stage!");
    }

    if (photoInput) {
      photoInput.addEventListener("change", (e) => {
        const file = e.target.files && e.target.files[0];
        handlePhotoFileSelected(file);
      });
    }

    if (uploadBox) {
      uploadBox.addEventListener("dragover", (e) => {
        e.preventDefault();
      });
      uploadBox.addEventListener("drop", (e) => {
        e.preventDefault();
        const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        handlePhotoFileSelected(file);
      });
    }

    if (btnApplyStep1) {
      btnApplyStep1.addEventListener("click", async () => {
        if (state.uploadedPhotoFile || state.builderPhotoDataUrl) {
          await applyUploadedPhotoToActiveAvatar(state.uploadedPhotoFile, state.builderPhotoDataUrl);
          closeModal();
          showToast("Saved & applied your custom photo to the stage!");
        } else {
          showToast("Please upload a photo or choose a portrait first.");
        }
      });
    }

    // Webcam Snapshot
    const btnStartCam = document.getElementById("btn-start-webcam");
    const btnSnapCam = document.getElementById("btn-capture-webcam");
    const camVideo = document.getElementById("builder-webcam-video");

    if (btnStartCam) {
      btnStartCam.addEventListener("click", async () => {
        try {
          state.webcamStream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } });
          if (camVideo) camVideo.srcObject = state.webcamStream;
        } catch (err) {
          showToast("Could not start camera.");
        }
      });
    }

    if (btnSnapCam) {
      btnSnapCam.addEventListener("click", async () => {
        if (camVideo && camVideo.videoWidth) {
          const snapUrl = drawImageToPortraitCanvas(camVideo, "Webcam snapshot captured (9:16)!", true);
          state.uploadedPhotoFile = null;
          await applyUploadedPhotoToActiveAvatar(null, snapUrl);
          showToast("Webcam photo captured & applied to stage!");
        }
      });
    }

    // Voice source tabs
    const tabRecVoice = document.getElementById("tab-btn-record-voice");
    const tabPreVoice = document.getElementById("tab-btn-prebuilt-voice");
    const paneRecVoice = document.getElementById("voice-pane-record");
    const panePreVoice = document.getElementById("voice-pane-prebuilt");

    if (tabRecVoice && tabPreVoice) {
      tabRecVoice.addEventListener("click", () => {
        tabRecVoice.classList.add("active");
        tabPreVoice.classList.remove("active");
        paneRecVoice.classList.add("active");
        panePreVoice.classList.remove("active");
        state.builderVoiceMode = "custom_voice";
      });
      tabPreVoice.addEventListener("click", () => {
        tabPreVoice.classList.add("active");
        tabRecVoice.classList.remove("active");
        panePreVoice.classList.add("active");
        paneRecVoice.classList.remove("active");
        state.builderVoiceMode = "prebuilt";
      });
    }

    // Custom Voice Recorder (24kHz PCM WAV)
    const btnRecStart = document.getElementById("btn-record-start");
    const btnRecStop = document.getElementById("btn-record-stop");
    const timerEl = document.getElementById("record-timer");
    const audioPlayer = document.getElementById("recorded-voice-audio");
    const waveCanvas = document.getElementById("voice-waveform-canvas");

    if (btnRecStart && btnRecStop) {
      btnRecStart.addEventListener("click", async () => {
        try {
          // Voice-clone sample: AGC off (it pumps fan/AC noise up during pauses and the cloned voice
          // copies that noise), echo cancellation off (nothing is playing), noise suppression on.
          state.recStream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: false, noiseSuppression: true, autoGainControl: false },
          });
          state.recAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
          const src = state.recAudioCtx.createMediaStreamSource(state.recStream);
          const proc = state.recAudioCtx.createScriptProcessor(4096, 1, 1);
          state.recProcessor = proc;
          state.recSamples = [];
          state.recStartTime = Date.now();

          proc.onaudioprocess = (e) => {
            const chan = e.inputBuffer.getChannelData(0);
            for (let i = 0; i < chan.length; i++) {
              state.recSamples.push(chan[i]);
            }
            if (waveCanvas) {
              const wctx = waveCanvas.getContext("2d");
              wctx.fillStyle = "#0f172a";
              wctx.fillRect(0, 0, waveCanvas.width, waveCanvas.height);
              wctx.strokeStyle = "#10b981";
              wctx.lineWidth = 2;
              wctx.beginPath();
              const step = Math.floor(chan.length / waveCanvas.width);
              for (let x = 0; x < waveCanvas.width; x++) {
                const sample = chan[x * step] || 0;
                const y = (sample * 0.5 + 0.5) * waveCanvas.height;
                if (x === 0) wctx.moveTo(x, y);
                else wctx.lineTo(x, y);
              }
              wctx.stroke();
            }
          };

          src.connect(proc);
          proc.connect(state.recAudioCtx.destination);

          btnRecStart.disabled = true;
          btnRecStop.disabled = false;
          state.recTimerInterval = setInterval(() => {
            const sec = Math.floor((Date.now() - state.recStartTime) / 1000);
            const mm = String(Math.floor(sec / 60)).padStart(2, "0");
            const ss = String(sec % 60).padStart(2, "0");
            if (timerEl) timerEl.textContent = mm + ":" + ss;
          }, 300);
        } catch (err) {
          showToast("Microphone access is required to record your voice profile.");
        }
      });

      btnRecStop.addEventListener("click", () => {
        if (state.recTimerInterval) clearInterval(state.recTimerInterval);
        if (state.recProcessor) state.recProcessor.disconnect();
        if (state.recStream) state.recStream.getTracks().forEach((t) => t.stop());
        const sampleRate = state.recAudioCtx ? state.recAudioCtx.sampleRate : 24000;
        if (state.recAudioCtx) state.recAudioCtx.close();

        const wavDataUrl = encodeWavFromFloat32(state.recSamples, sampleRate);
        state.builderVoiceDataUrl = wavDataUrl;
        state.builderVoiceMode = "custom_voice";
        if (audioPlayer) audioPlayer.src = wavDataUrl;

        btnRecStart.disabled = false;
        btnRecStop.disabled = true;
        showToast("Custom voice recorded!");
      });
    }

    // Audio file upload option for custom voice
    const voiceUpload = document.getElementById("builder-voice-upload");
    if (voiceUpload) {
      voiceUpload.addEventListener("change", (e) => {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
          state.builderVoiceDataUrl = ev.target.result;
          state.builderVoiceMode = "custom_voice";
          if (audioPlayer) audioPlayer.src = ev.target.result;
          showToast("Custom voice audio uploaded: " + file.name);
        };
        reader.readAsDataURL(file);
      });
    }

    // Quick 1-Click Save Voice Change in Step 2 (for editing existing or active avatar)
    const btnApplyVoiceStep2 = document.getElementById("btn-apply-voice-step2");
    if (btnApplyVoiceStep2) {
      btnApplyVoiceStep2.addEventListener("click", async () => {
        const targetId = state.editingAvatarId || (state.activeAvatar && state.activeAvatar.id);
        if (!targetId) {
          showToast("Select a saved avatar first.");
          return;
        }
        const fd = new FormData();
        fd.append("voice_mode", state.builderVoiceDataUrl ? "custom_voice" : state.builderVoiceMode);
        fd.append("prebuilt_voice", state.builderPrebuiltVoice);
        if (state.builderVoiceDataUrl && state.builderVoiceDataUrl.indexOf(",") !== -1) {
          fd.append("voice_file", dataUrlToBlob(state.builderVoiceDataUrl), "custom_voice.wav");
        }
        if (state.uploadedPhotoFile) {
          fd.append("photo_file", state.uploadedPhotoFile, state.uploadedPhotoFile.name || "custom_photo.jpg");
        } else if (state.builderPhotoDataUrl && state.builderPhotoDataUrl.indexOf(",") !== -1) {
          fd.append("photo_file", dataUrlToBlob(state.builderPhotoDataUrl), "custom_photo.jpg");
        }

        btnApplyVoiceStep2.disabled = true;
        btnApplyVoiceStep2.textContent = "Saving Voice...";
        try {
          const resp = await fetch("/api/avatars/" + encodeURIComponent(targetId) + "/update", {
            method: "POST",
            body: fd,
          });
          if (!resp.ok) {
            const errBody = await resp.json();
            throw new Error(errBody.detail || "Failed to update voice");
          }
          const updated = await resp.json();
          const idx = state.avatars.findIndex((a) => a.id === updated.id);
          if (idx >= 0) state.avatars[idx] = updated;
          await selectAvatar(updated.id);
          closeModal();
          showToast("Updated voice for '" + updated.name + "'!");
          if (state.isLive) {
            startLiveSession();
          }
        } catch (err) {
          showToast("Error: " + err.message);
        } finally {
          btnApplyVoiceStep2.disabled = false;
          btnApplyVoiceStep2.textContent = "\u2713 Save Voice Change Now";
        }
      });
    }

    // Save New or Edited Reusable Avatar Button
    const btnSaveAvatar = document.getElementById("btn-save-new-avatar");
    if (btnSaveAvatar) {
      btnSaveAvatar.addEventListener("click", async () => {
        const nameInput = document.getElementById("builder-name");
        const tagInput = document.getElementById("builder-tagline");
        const sysInput = document.getElementById("builder-instructions");

        const nameVal = (nameInput && nameInput.value.trim()) || "My Custom Avatar";
        const tagVal = (tagInput && tagInput.value.trim()) || "Custom Photo & Voice Persona";
        const sysVal = (sysInput && sysInput.value.trim()) || "";

        const fd = new FormData();
        fd.append("name", nameVal);
        fd.append("role_tagline", tagVal);
        fd.append("avatar_mode", state.builderAvatarMode);
        fd.append("builtin_avatar_name", state.builderBuiltinName);
        if (state.builderPresetId) {
          fd.append("preset_photo_id", state.builderPresetId);
        }
        if (state.uploadedPhotoFile) {
          fd.append("photo_file", state.uploadedPhotoFile, state.uploadedPhotoFile.name || "custom_photo.jpg");
        } else if (state.builderPhotoDataUrl && state.builderPhotoDataUrl.indexOf(",") !== -1) {
          fd.append("photo_file", dataUrlToBlob(state.builderPhotoDataUrl), "custom_photo.jpg");
        }
        fd.append("voice_mode", state.builderVoiceDataUrl ? "custom_voice" : state.builderVoiceMode);
        fd.append("prebuilt_voice", state.builderPrebuiltVoice);
        if (state.builderVoiceDataUrl && state.builderVoiceDataUrl.indexOf(",") !== -1) {
          fd.append("voice_file", dataUrlToBlob(state.builderVoiceDataUrl), "custom_voice.wav");
        }
        fd.append("system_instruction", sysVal);

        const isEditing = Boolean(state.editingAvatarId);
        const endpoint = isEditing
          ? "/api/avatars/" + encodeURIComponent(state.editingAvatarId) + "/update"
          : "/api/avatars";

        btnSaveAvatar.disabled = true;
        btnSaveAvatar.textContent = isEditing ? "Saving Changes..." : "Saving Avatar...";
        try {
          const resp = await fetch(endpoint, { method: "POST", body: fd });
          if (!resp.ok) {
            const errBody = await resp.json();
            throw new Error(errBody.detail || "Failed to save avatar");
          }
          const saved = await resp.json();
          if (isEditing) {
            const idx = state.avatars.findIndex((a) => a.id === saved.id);
            if (idx >= 0) state.avatars[idx] = saved;
          } else {
            state.avatars.unshift(saved);
          }
          await selectAvatar(saved.id);
          closeModal();
          showToast(
            isEditing
              ? "Updated '" + saved.name + "' (Photo & Voice saved)!"
              : "Saved '" + saved.name + "' to your Avatar Library!"
          );
          if (state.isLive && isEditing) {
            startLiveSession();
          }
        } catch (err) {
          showToast("Error: " + err.message);
        } finally {
          btnSaveAvatar.disabled = false;
          btnSaveAvatar.textContent = isEditing
            ? "\u2713 Save Changes to '" + nameVal + "'"
            : "Save Reusable Avatar to Studio Library";
        }
      });
    }
  }

  function stopWebcam() {
    if (state.webcamStream) {
      state.webcamStream.getTracks().forEach((t) => t.stop());
      state.webcamStream = null;
    }
  }

  function populateBuilderPresetsAndVoices() {
    if (!state.config) return;

    const presetGrid = document.getElementById("builder-preset-grid");
    if (presetGrid) {
      presetGrid.replaceChildren();
      (state.config.preset_avatars || []).forEach((p) => {
        const tile = document.createElement("div");
        tile.className = "preset-tile" + (state.builderPresetId === p.id ? " selected" : "");

        const img = document.createElement("img");
        img.src = p.preview_url;
        img.alt = p.name;

        const title = document.createElement("div");
        title.className = "preset-tile-title";
        title.textContent = p.name;

        tile.appendChild(img);
        tile.appendChild(title);

        tile.addEventListener("click", () => {
          state.builderPresetId = p.id;
          if (p.type === "builtin") {
            state.builderAvatarMode = "builtin";
            state.builderBuiltinName = p.avatar_name;
          } else {
            state.builderAvatarMode = "custom_photo";
          }
          loadUrlIntoPortraitCanvas(p.preview_url, "Selected: " + p.name);
          populateBuilderPresetsAndVoices();
        });

        presetGrid.appendChild(tile);
      });
    }

    const voiceGrid = document.getElementById("builder-voice-list");
    if (voiceGrid) {
      voiceGrid.replaceChildren();
      (state.config.prebuilt_voices || []).forEach((v) => {
        const card = document.createElement("div");
        card.className = "voice-option-card" + (state.builderPrebuiltVoice === v.id ? " selected" : "");

        const vName = document.createElement("div");
        vName.style.fontWeight = "600";
        vName.style.fontSize = "13.5px";
        vName.textContent = v.name + " (" + v.gender + ")";

        const vStyle = document.createElement("div");
        vStyle.className = "subtle-hint";
        vStyle.textContent = v.style;

        card.appendChild(vName);
        card.appendChild(vStyle);

        card.addEventListener("click", () => {
          state.builderPrebuiltVoice = v.id;
          state.builderVoiceMode = "prebuilt";
          state.builderVoiceDataUrl = "";
          populateBuilderPresetsAndVoices();
        });

        voiceGrid.appendChild(card);
      });
    }
  }

  // -------------------------------------------------------------------------
  // Knowledge & Navigation Controls
  // -------------------------------------------------------------------------
  function initKnowledgeControls() {
    // Top workspace nav tabs
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchWorkspaceView(btn.getAttribute("data-view"));
      });
    });

    const btnJumpKb = document.getElementById("btn-jump-knowledge");
    if (btnJumpKb) {
      btnJumpKb.addEventListener("click", () => {
        switchWorkspaceView("view-knowledge");
      });
    }

    // Knowledge ingestion sub-tabs
    const tabs = document.querySelectorAll(".kb-tab");
    tabs.forEach((btn) => {
      btn.addEventListener("click", () => {
        tabs.forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".kb-tab-body").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const target = document.getElementById(btn.getAttribute("data-tab"));
        if (target) target.classList.add("active");
      });
    });

    // Add URL
    const btnAddUrl = document.getElementById("btn-add-url");
    if (btnAddUrl) {
      btnAddUrl.addEventListener("click", async () => {
        if (!state.activeAvatar) return;
        const urlIn = document.getElementById("kb-url-input");
        const titleIn = document.getElementById("kb-url-title");
        const urlVal = urlIn ? urlIn.value.trim() : "";
        if (!urlVal) {
          showToast("Enter a valid https:// URL.");
          return;
        }
        btnAddUrl.disabled = true;
        btnAddUrl.textContent = "Indexing Link...";
        try {
          const resp = await fetch(
            "/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/knowledge/url",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ url: urlVal, title: titleIn ? titleIn.value.trim() : "" }),
            }
          );
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || "Failed to index URL");
          if (urlIn) urlIn.value = "";
          if (titleIn) titleIn.value = "";
          await selectAvatar(state.activeAvatar.id);
          showToast("Indexed link into Avatar Knowledge Base!");
        } catch (err) {
          showToast("URL Error: " + err.message);
        } finally {
          btnAddUrl.disabled = false;
          btnAddUrl.textContent = "Index Web Link";
        }
      });
    }

    // File selection label
    const kbFileInput = document.getElementById("kb-file-input");
    const kbFileLabel = document.getElementById("kb-file-selected");
    if (kbFileInput) {
      kbFileInput.addEventListener("change", (e) => {
        const f = e.target.files && e.target.files[0];
        if (kbFileLabel) kbFileLabel.textContent = f ? "Selected: " + f.name : "No file selected";
      });
    }

    // Add File
    const btnAddFile = document.getElementById("btn-add-file");
    if (btnAddFile) {
      btnAddFile.addEventListener("click", async () => {
        if (!state.activeAvatar || !kbFileInput || !kbFileInput.files[0]) {
          showToast("Choose a file (.pdf, .txt, .md, .csv, .json, .html) first.");
          return;
        }
        const fd = new FormData();
        fd.append("file", kbFileInput.files[0]);
        btnAddFile.disabled = true;
        btnAddFile.textContent = "Indexing File...";
        try {
          const resp = await fetch(
            "/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/knowledge/file",
            { method: "POST", body: fd }
          );
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || "Failed to index file");
          kbFileInput.value = "";
          if (kbFileLabel) kbFileLabel.textContent = "No file selected";
          await selectAvatar(state.activeAvatar.id);
          showToast("File attached to Avatar Knowledge Base!");
        } catch (err) {
          showToast("File Error: " + err.message);
        } finally {
          btnAddFile.disabled = false;
          btnAddFile.textContent = "Upload & Index File";
        }
      });
    }

    // Add Note
    const btnAddNote = document.getElementById("btn-add-note");
    if (btnAddNote) {
      btnAddNote.addEventListener("click", async () => {
        if (!state.activeAvatar) return;
        const tIn = document.getElementById("kb-note-title");
        const cIn = document.getElementById("kb-note-content");
        const content = cIn ? cIn.value.trim() : "";
        if (!content) {
          showToast("Enter knowledge text to attach.");
          return;
        }
        const resp = await fetch(
          "/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/knowledge/note",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: tIn ? tIn.value.trim() : "Knowledge Note", content: content }),
          }
        );
        if (resp.ok) {
          if (tIn) tIn.value = "";
          if (cIn) cIn.value = "";
          await selectAvatar(state.activeAvatar.id);
          showToast("Knowledge note saved!");
        }
      });
    }

    // Save System Instructions
    const btnSaveSys = document.getElementById("btn-save-instructions");
    if (btnSaveSys) {
      btnSaveSys.addEventListener("click", async () => {
        if (!state.activeAvatar) return;
        const sysIn = document.getElementById("active-system-instruction");
        const resp = await fetch("/api/avatars/" + encodeURIComponent(state.activeAvatar.id), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ system_instruction: sysIn ? sysIn.value : "" }),
        });
        if (resp.ok) {
          await selectAvatar(state.activeAvatar.id);
          showToast("System instructions saved for " + state.activeAvatar.name + "!");
        }
      });
    }

    // Direct Backend Knowledge Agent Tester
    const btnTestAgent = document.getElementById("btn-test-agent");
    const agentQueryIn = document.getElementById("agent-test-query");
    if (btnTestAgent && agentQueryIn) {
      btnTestAgent.addEventListener("click", async () => {
        if (!state.activeAvatar) return;
        const q = agentQueryIn.value.trim() || "Summarize the attached knowledge sources.";
        btnTestAgent.disabled = true;
        try {
          const resp = await fetch(
            "/api/avatars/" + encodeURIComponent(state.activeAvatar.id) + "/ask-agent",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ query: q }),
            }
          );
          const data = await resp.json();
          appendAgentEvent({
            source: "direct_agent_query",
            query: q,
            agent_answer: data.agent_answer,
            citations: data.citations || [],
          });
        } finally {
          btnTestAgent.disabled = false;
        }
      });
    }
  }

  async function initApp() {
    initBuilderModal();
    initKnowledgeControls();

    const btnConnect = document.getElementById("btn-connect-live");
    const btnDisconnect = document.getElementById("btn-disconnect-live");
    const btnSend = document.getElementById("btn-send-text");
    const chatInput = document.getElementById("chat-text-input");
    const btnMic = document.getElementById("btn-toggle-mic");

    const btnFsOverlay = document.getElementById("btn-fullscreen-overlay");
    const btnFsDock = document.getElementById("btn-fullscreen-dock");
    const btnToggleFit = document.getElementById("btn-toggle-fit");
    const btnFsTranscript = document.getElementById("btn-toggle-fs-transcript");
    const btnFsTranscriptDock = document.getElementById("btn-toggle-fs-transcript-dock");
    const fsChatInput = document.getElementById("fullscreen-chat-input");
    const btnFsSend = document.getElementById("btn-fullscreen-send");

    if (btnConnect) {
      btnConnect.addEventListener("click", () => {
        state.userManuallyStopped = false;
        startLiveSession();
      });
    }
    if (btnDisconnect) btnDisconnect.addEventListener("click", stopLiveSession);
    if (btnSend) btnSend.addEventListener("click", sendUserText);
    if (chatInput) {
      chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendUserText();
      });
    }
    if (btnMic) btnMic.addEventListener("click", toggleMicStreaming);

    if (btnFsOverlay) btnFsOverlay.addEventListener("click", toggleAvatarFullScreen);
    if (btnFsDock) btnFsDock.addEventListener("click", toggleAvatarFullScreen);
    if (btnToggleFit) btnToggleFit.addEventListener("click", toggleFitFillScreen);
    if (btnFsTranscript) btnFsTranscript.addEventListener("click", toggleFullScreenTranscript);
    if (btnFsTranscriptDock) btnFsTranscriptDock.addEventListener("click", toggleFullScreenTranscript);

    function sendFullScreenText() {
      if (!fsChatInput) return;
      const val = fsChatInput.value.trim();
      if (!val) return;
      if (chatInput) chatInput.value = val;
      fsChatInput.value = "";
      sendUserText();
    }

    if (btnFsSend) btnFsSend.addEventListener("click", sendFullScreenText);
    if (fsChatInput) {
      fsChatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendFullScreenText();
      });
    }

    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement) {
        const stageCard = document.getElementById("avatar-stage-card");
        if (stageCard && stageCard.classList.contains("fullscreen-call-mode")) {
          syncFullScreenUiState(false);
        }
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const stageCard = document.getElementById("avatar-stage-card");
        if (stageCard && stageCard.classList.contains("fullscreen-call-mode")) {
          syncFullScreenUiState(false);
        }
      }
    });

    const ensureAudioUnmuted = () => {
      const videoEl = document.getElementById("live-avatar-video");
      if (videoEl) {
        if (videoEl.muted) videoEl.muted = false;
        if (videoEl.paused && state.isLive) videoEl.play().catch(() => {});
      }
    };
    document.addEventListener("pointerdown", ensureAudioUnmuted, { passive: true });
    document.addEventListener("keydown", ensureAudioUnmuted, { passive: true });

    try {
      const [cfgResp, avResp] = await Promise.all([fetch("/api/config"), fetch("/api/avatars")]);
      state.config = await cfgResp.json();
      const avData = await avResp.json();
      state.avatars = avData.avatars || [];

      populateBuilderPresetsAndVoices();

      if (state.avatars.length > 0) {
        await selectAvatar(state.avatars[0].id);
      }
    } catch (err) {
      showToast("Error loading studio.");
    }
  }

  window.addEventListener("DOMContentLoaded", initApp);
})();

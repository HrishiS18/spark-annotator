import { api } from "/static/common.js";

// The browser is only the microphone/speaker and WebRTC client. Codex app-server
// owns the native voice session, its model, and the Codex-agent handoffs.
export class NativeVoiceClient {
  constructor(sessionId, audioElement, onState = () => {}) {
    this.sessionId = sessionId;
    this.audioElement = audioElement;
    this.onState = onState;
    this.pc = null;
    this.stream = null;
    this.muted = false;
    this.cancelled = false;
  }

  ensureOpen() {
    if (this.cancelled) throw new Error("Voice chat was cancelled.");
  }

  async start() {
    if (!window.RTCPeerConnection || !navigator.mediaDevices?.getUserMedia) {
      throw new Error("Voice chat needs a browser with microphone and WebRTC support on localhost or HTTPS.");
    }
    this.onState("microphone");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      this.ensureOpen();
      this.pc = new RTCPeerConnection();
      this.stream.getAudioTracks().forEach(track => this.pc.addTrack(track, this.stream));
      this.pc.createDataChannel("oai-events");
      this.pc.ontrack = event => {
        this.audioElement.srcObject = event.streams[0] || new MediaStream([event.track]);
        this.audioElement.play().catch(() => this.onState("playback-blocked"));
      };
      this.pc.onconnectionstatechange = () => {
        if (this.pc?.connectionState === "connected") this.onState("active");
        else if (this.pc?.connectionState === "failed") this.onState("failed");
      };
      await this.pc.setLocalDescription(await this.pc.createOffer());
      await this.waitForIce();
      this.ensureOpen();
      this.onState("connecting");
      const { sdp } = await api("POST", `/api/sessions/${this.sessionId}/runtime/voice/start`,
        { sdp: this.pc.localDescription.sdp });
      this.ensureOpen();
      if (!this.pc || this.pc.signalingState === "closed") throw new Error("Voice chat was closed while connecting.");
      await this.pc.setRemoteDescription({ type: "answer", sdp });
    } catch (error) {
      this.closeLocal();
      throw error;
    }
  }

  waitForIce() {
    if (this.pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      let finished = false;
      const done = () => {
        if (finished) return;
        finished = true;
        this.pc?.removeEventListener("icegatheringstatechange", check);
        clearTimeout(timeout);
        resolve();
      };
      const check = () => { if (this.pc?.iceGatheringState === "complete") done(); };
      this.pc.addEventListener("icegatheringstatechange", check);
      const timeout = setTimeout(done, 10000);
    });
  }

  toggleMute() {
    this.muted = !this.muted;
    this.stream?.getAudioTracks().forEach(track => { track.enabled = !this.muted; });
    return this.muted;
  }

  closeLocal() {
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
    this.pc?.close();
    this.pc = null;
    this.audioElement.srcObject = null;
    this.muted = false;
  }

  async stop() {
    this.cancelled = true;
    this.closeLocal();
    this.onState("stopping");
    await api("POST", `/api/sessions/${this.sessionId}/runtime/voice/stop`, {});
    this.onState("inactive");
  }
}

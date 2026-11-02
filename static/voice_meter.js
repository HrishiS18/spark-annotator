import { api } from "/static/common.js";

const percentile = (values, p) => {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(p / 100 * (sorted.length - 1))))];
};
const median = values => percentile(values, 50);
const semitones = hz => 12 * Math.log2(hz / 100);
const sampleId = () => "vs_" + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, "0")).join("");

// Measures the participant's own microphone while voice chat runs. Frames from
// the audio worklet become utterances; only each utterance's summary numbers
// are sent to the study server. The agent's audio is measured only to know
// when it was playing, so overlap and echo can be excluded.
export class VoiceMeter {
  constructor(sessionId, settings) {
    this.sid = sessionId;
    this.cfg = settings.acoustic;
    this.queue = [];
    this.levels = [];
    this.pending = null;
    this.open = null;
    this.remoteLast = null;
    this.sending = null;
  }

  async start(stream) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context || !window.AudioWorkletNode) throw new Error("This browser cannot measure voice locally.");
    this.ctx = new Context();
    await this.ctx.audioWorklet.addModule("/static/voice_meter_worklet.js");
    const track = stream.getAudioTracks()[0];
    const used = track?.getSettings?.() || {};
    this.quality = { sample_rate: this.ctx.sampleRate, auto_gain_control: used.autoGainControl ?? null,
                     echo_cancellation: used.echoCancellation ?? null, noise_suppression: used.noiseSuppression ?? null };
    this.local = this.connect(stream, true, frame => this.onFrame(frame));
    // Autoplay rules can start the context suspended until the next click.
    this.resume = () => { if (this.ctx?.state === "suspended") this.ctx.resume().catch(() => {}); };
    document.addEventListener("pointerdown", this.resume);
    this.resume();
    this.timer = setInterval(() => this.send(), this.cfg.send_interval_ms);
  }

  connect(stream, pitch, onFrame) {
    const c = this.cfg;
    const source = this.ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(this.ctx, "voice-frames", { processorOptions: {
      pitch, hopMs: c.hop_ms, windowMs: c.window_ms, targetRate: c.target_rate_hz,
      pitchMinHz: c.pitch_min_hz, pitchMaxHz: c.pitch_max_hz, yinThreshold: c.yin_threshold } });
    const sink = this.ctx.createGain();
    sink.gain.value = 0;   // keeps the graph running without playing anything
    source.connect(node).connect(sink).connect(this.ctx.destination);
    node.port.onmessage = event => onFrame(event.data);
    return { source, node, sink };
  }

  attachRemote(stream) {
    if (!this.ctx || this.remote) return;
    this.remote = this.connect(stream, false, frame => {
      if (frame.db > this.cfg.remote_active_db) this.remoteLast = this.epoch(frame.t);
    });
  }

  epoch(contextTime) {
    const stamp = this.ctx.getOutputTimestamp?.();
    if (stamp?.contextTime) return performance.timeOrigin + stamp.performanceTime + (contextTime - stamp.contextTime) * 1000;
    return Date.now() - (this.ctx.currentTime - contextTime) * 1000;
  }

  remotePlaying(at) {
    return this.remoteLast != null && at - this.remoteLast <= this.cfg.remote_hold_ms;
  }

  onFrame({ t, db, f0, confidence }) {
    const c = this.cfg, now = this.epoch(t), remote = this.remotePlaying(now);
    // The room's noise floor is learned only while the agent is silent, so its
    // voice leaking into the microphone cannot raise the bar for the participant.
    if (!remote) {
      this.levels.push(db);
      if (this.levels.length > c.noise_floor_window_s * 1000 / c.hop_ms) this.levels.shift();
    }
    const floor = percentile(this.levels, c.noise_floor_percentile) ?? db;
    const speech = db > Math.max(c.vad_min_db, floor + c.vad_margin_db);
    const pitch = f0 && confidence >= c.pitch_min_confidence && f0 >= c.pitch_min_hz && f0 <= c.pitch_max_hz && !remote ? f0 : null;
    const frame = { at: now, db, pitch, remote, speech };
    if (!this.open) {
      if (!speech) { this.pending = null; return; }
      this.pending ??= { frames: [], remoteAtStart: remote };
      this.pending.frames.push(frame);
      if (this.pending.frames.length * c.hop_ms >= c.min_speech_ms) {
        const first = this.pending.frames[0];
        this.open = { frames: this.pending.frames, pauses: [], silence: null, startedDuringRemote: this.pending.remoteAtStart,
                      start: first.at - c.hop_ms, remoteGap: this.pending.remoteAtStart || this.remoteLast == null ? null : Math.max(0, first.at - this.remoteLast) };
        this.pending = null;
      }
      return;
    }
    const u = this.open;
    u.frames.push(frame);
    if (speech) {
      if (u.silence != null && now - u.silence >= c.pause_min_ms) u.pauses.push(now - u.silence);
      u.silence = null;
    } else u.silence ??= now;
    if (u.silence != null && now - u.silence >= c.end_silence_ms) this.close(u.silence);
    else if (now - u.start >= c.max_utterance_ms) this.close(now);
  }

  syllables(frames) {
    // Peaks in the smoothed loudness envelope approximate syllable nuclei.
    const c = this.cfg, reach = Math.floor(c.syllable_smooth_frames / 2), env = frames.map((f, i) => {
      const near = frames.slice(Math.max(0, i - reach), i + reach + 1).map(g => g.db);
      return near.reduce((a, b) => a + b, 0) / near.length;
    });
    let count = 0, lastPeak = -Infinity;
    for (let i = 1; i < env.length - 1; i++) {
      if (!frames[i].speech || frames[i].remote || env[i] < env[i - 1] || env[i] < env[i + 1]) continue;
      let left = env[i], right = env[i];
      for (let j = i - 1; j >= 0 && env[j] <= env[i]; j--) left = Math.min(left, env[j]);
      for (let j = i + 1; j < env.length && env[j] <= env[i]; j++) right = Math.min(right, env[j]);
      if (env[i] - Math.max(left, right) < c.syllable_prominence_db) continue;
      if (frames[i].at - lastPeak < c.syllable_min_gap_ms) continue;
      count++; lastPeak = frames[i].at;
    }
    return count;
  }

  close(end) {
    const u = this.open, c = this.cfg;
    this.open = null;
    const frames = u.frames.filter(f => f.at <= end);
    const spoken = frames.filter(f => f.speech), own = spoken.filter(f => !f.remote);
    const pitches = frames.filter(f => f.pitch).map(f => f.pitch);
    const tones = frames.filter(f => f.pitch).map(f => semitones(f.pitch));
    const tail = Math.max(1, Math.round(tones.length * c.pitch_end_fraction));
    const loud = own.map(f => f.db);
    // Sound heard only while the agent was playing is most likely its own echo.
    if (own.length * c.hop_ms < c.min_speech_ms) return;
    this.queue.push({
      id: sampleId(), start_ms: u.start, end_ms: end,
      speech_ms: spoken.length * c.hop_ms, voiced_ms: pitches.length * c.hop_ms,
      overlap_ms: spoken.filter(f => f.remote).length * c.hop_ms,
      pause_count: u.pauses.length, longest_pause_ms: Math.max(0, ...u.pauses),
      syllables: this.syllables(frames),
      pitch_hz_median: median(pitches), pitch_hz_p10: percentile(pitches, 10), pitch_hz_p90: percentile(pitches, 90),
      pitch_end_slope_st: tones.length >= c.pitch_slope_min_frames ? median(tones.slice(-tail)) - median(tones.slice(0, -tail)) : null,
      loudness_db_mean: loud.length ? loud.reduce((a, b) => a + b, 0) / loud.length : null,
      loudness_db_p90: percentile(loud, 90),
      remote_gap_ms: u.remoteGap, started_during_remote: u.startedDuringRemote, quality: this.quality,
    });
    void this.send();
  }

  async send() {
    if (this.sending) return this.sending;
    if (!this.queue.length) return;
    const batch = this.queue.slice(0, this.cfg.max_batch);
    this.sending = api("POST", `/api/sessions/${this.sid}/voice-samples`, { samples: batch })
      .then(() => { this.queue = this.queue.filter(v => !batch.includes(v)); })
      // A rejected batch (for example after the conversation closed) is dropped;
      // a network failure keeps it for the next attempt.
      .catch(error => { if (error.status && error.status < 500) this.queue = this.queue.filter(v => !batch.includes(v)); })
      .finally(() => { this.sending = null; });
    await this.sending;
    if (this.queue.length && batch.length === this.cfg.max_batch) return this.send();
  }

  async stop() {
    if (this.open) this.close(this.open.silence ?? Date.now());
    clearInterval(this.timer);
    document.removeEventListener("pointerdown", this.resume);
    for (const part of [this.local, this.remote]) part?.source.disconnect();
    await this.ctx?.close().catch(() => {});
    this.ctx = null;
    for (let tries = 0; tries < 3 && this.queue.length; tries++) await this.send();
  }
}

// Runs on the audio thread. Every hop it reports the level (dBFS) of the input
// and, when asked, its pitch from the YIN estimator. Audio never leaves this
// node; only these numbers are posted to the page.
class VoiceFrames extends AudioWorkletProcessor {
  constructor({ processorOptions: o }) {
    super();
    this.pitch = o.pitch;
    this.factor = Math.max(1, Math.floor(sampleRate / o.targetRate));
    this.rate = sampleRate / this.factor;
    this.hop = Math.round(this.rate * o.hopMs / 1000);
    this.window = Math.round(this.rate * o.windowMs / 1000);
    this.tauMin = Math.max(2, Math.floor(this.rate / o.pitchMaxHz));
    this.tauMax = Math.ceil(this.rate / o.pitchMinHz);
    this.threshold = o.yinThreshold;
    this.ring = new Float32Array(this.window + this.tauMax);   // circular, oldest at this.head
    this.lin = new Float32Array(this.ring.length);              // in time order, for YIN
    this.head = 0;
    this.filled = 0;
    this.sinceHop = 0;
    this.sum = 0; this.count = 0;      // energy since the last hop, at the device rate
    this.acc = 0; this.accN = 0;       // averaging decimator
    this.diff = new Float32Array(this.tauMax + 1);
  }

  yin() {
    const x = this.lin, W = this.window, d = this.diff;
    x.set(this.ring.subarray(this.head));
    x.set(this.ring.subarray(0, this.head), this.ring.length - this.head);
    let running = 0, best = -1;
    d[0] = 1;
    for (let tau = 1; tau <= this.tauMax; tau++) {
      let s = 0;
      for (let j = 0; j < W; j++) { const v = x[j] - x[j + tau]; s += v * v; }
      running += s;
      d[tau] = running > 0 ? s * tau / running : 1;   // cumulative mean normalised difference
      if (best < 0 && tau >= this.tauMin && d[tau] < this.threshold) best = tau;
    }
    if (best < 0) return [null, 0];
    while (best + 1 <= this.tauMax && d[best + 1] < d[best]) best++;
    const a = d[best - 1], b = d[best], c = best + 1 <= this.tauMax ? d[best + 1] : b;
    const shift = (a + c - 2 * b) ? (a - c) / (2 * (a + c - 2 * b)) : 0;
    return [this.rate / (best + shift), Math.max(0, 1 - b)];
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      const v = channel[i];
      this.sum += v * v; this.count++;
      this.acc += v; this.accN++;
      if (this.accN < this.factor) continue;
      const sample = this.acc / this.accN; this.acc = 0; this.accN = 0;
      this.ring[this.head] = sample;
      this.head = (this.head + 1) % this.ring.length;
      if (this.filled < this.ring.length) this.filled++;
      if (++this.sinceHop < this.hop) continue;
      this.sinceHop = 0;
      const db = 10 * Math.log10(this.sum / Math.max(1, this.count) + 1e-12);
      this.sum = 0; this.count = 0;
      const [f0, confidence] = this.pitch && this.filled === this.ring.length ? this.yin() : [null, 0];
      this.port.postMessage({ t: currentTime, db, f0, confidence });
    }
    return true;
  }
}
registerProcessor("voice-frames", VoiceFrames);

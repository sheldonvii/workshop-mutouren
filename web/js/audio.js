// 合成音效：口令鼓点、运动会哨子、淘汰蜂鸣、胜利音阶、倒计时滴答、过关提示。
// 浏览器要求用户操作后才能出声，第一次按键或点击时调用 unlock()。口令语音以后可以换成录好的音频。
export class Sound {
  constructor() {
    this.ctx = null;
    this.master = null;
  }

  get ready() {
    return this.ctx?.state === 'running';
  }

  unlock() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.master = this.ctx.createGain();
      this.master.gain.value = 0.8;
      this.master.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
  }

  _gain(t, peak, attack, release) {
    const gain = this.ctx.createGain();
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(peak, t + attack);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + attack + release);
    gain.connect(this.master);
    return gain;
  }

  _osc(type, freq, t, duration, peak, attack = 0.005) {
    const osc = this.ctx.createOscillator();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    osc.connect(this._gain(t, peak, attack, duration));
    osc.start(t);
    osc.stop(t + attack + duration + 0.05);
    return osc;
  }

  _noise(t, duration, peak, highpass) {
    const buffer = this.ctx.createBuffer(1, Math.ceil(this.ctx.sampleRate * duration), this.ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < data.length; i += 1) data[i] = Math.random() * 2 - 1;
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    const filter = this.ctx.createBiquadFilter();
    filter.type = 'highpass';
    filter.frequency.value = highpass;
    source.connect(filter).connect(this._gain(t, peak, 0.002, duration));
    source.start(t);
  }

  /** 口令每个字一下鼓点，最后一个字更重。 */
  drum(last = false) {
    if (!this.ready) return;
    const t = this.ctx.currentTime;
    const osc = this._osc('sine', last ? 170 : 150, t, last ? 0.32 : 0.22, last ? 1 : 0.8);
    osc.frequency.exponentialRampToValueAtTime(46, t + 0.2);
    this._noise(t, 0.05, last ? 0.35 : 0.22, 1800);
  }

  /** 运动会裁判哨：高频加快速颤音。 */
  whistle() {
    if (!this.ready) return;
    const t = this.ctx.currentTime;
    const osc = this._osc('sine', 2950, t, 0.5, 0.35, 0.02);
    const trill = this.ctx.createOscillator();
    const depth = this.ctx.createGain();
    trill.frequency.value = 34;
    depth.gain.value = 140;
    trill.connect(depth).connect(osc.frequency);
    trill.start(t);
    trill.stop(t + 0.6);
    this._noise(t, 0.4, 0.05, 4000);
  }

  buzzer() {
    if (!this.ready) return;
    const t = this.ctx.currentTime;
    this._osc('sawtooth', 98, t, 0.75, 0.35, 0.01);
    this._osc('square', 103, t, 0.75, 0.18, 0.01);
  }

  fanfare() {
    if (!this.ready) return;
    const t = this.ctx.currentTime;
    [523.25, 659.25, 783.99, 1046.5].forEach((freq, i) => {
      this._osc('square', freq, t + i * 0.13, i === 3 ? 0.6 : 0.12, 0.18);
    });
  }

  tick(final = false) {
    if (!this.ready) return;
    this._osc('square', final ? 1320 : 880, this.ctx.currentTime, 0.06, 0.15);
  }

  ding() {
    if (!this.ready) return;
    const t = this.ctx.currentTime;
    this._osc('triangle', 1046.5, t, 0.4, 0.3);
    this._osc('triangle', 1568, t + 0.08, 0.4, 0.2);
  }
}

/* JARVIS AI Core — Canvas 2D, no WebGL.
 *
 * Every moving part maps to something real:
 *   - rotation speed and colour follow the brain's state events;
 *   - the waveform ring and pulse rings are drawn from the real mic / TTS level
 *     events (the brain only sends mic levels while it is actually capturing);
 *   - the sweep arc appears only while a tool is running.
 * Idle draws at 20 fps (the rotation is slow), active states at 60; "balanced"
 * caps at 30 (15 idle), "minimal" at 20 (12 idle) without particles. Hidden window: no frames at all. Reduce Motion: nothing
 * rotates; only the level-driven parts move.
 */

const TAU = Math.PI * 2;

const STATE = {
  idle:      { speed: 0.06, energy: 0.30, fps: 20 },
  listening: { speed: 0.16, energy: 0.62, fps: 60 },
  thinking:  { speed: 0.42, energy: 0.72, fps: 60 },
  working:   { speed: 0.55, energy: 0.80, fps: 60 },
  speaking:  { speed: 0.18, energy: 0.62, fps: 60 },
  confirm:   { speed: 0.10, energy: 0.55, fps: 24 },
  muted:     { speed: 0.03, energy: 0.18, fps: 15 },
  error:     { speed: 0.04, energy: 0.45, fps: 20 },
  offline:   { speed: 0.02, energy: 0.20, fps: 12 },
};

const IDLE_BLUE = [96, 160, 255];
const AMBER = [255, 181, 71];
const ERROR = [255, 106, 80];
const MUTED = [90, 112, 140];

const lerp = (a, b, t) => a + (b - a) * t;
const mix = (c1, c2, t) => [lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t)];
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a.toFixed(3)})`;

export class Core {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: true, desynchronized: true });
    this.state = "offline";
    this.accent = [56, 225, 255];
    this.color = MUTED.slice();
    this.quality = "high";
    this.reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.visible = true;
    this.active = true;
    this.running = false;
    this.speed = 0.02;
    this.energy = 0.2;
    this.rot = [0, 0, 0, 0];
    this.level = 0;          // smoothed level of whichever source is live
    this.levelTarget = 0;
    this.history = new Float32Array(96);
    this.pulses = [];
    this.lastPulseAt = 0;
    this.sweep = 0;
    this.particles = [];
    this.last = 0;
    this.lastDraw = 0;
    this.frames = 0;
    this.fpsWindowStart = performance.now();
    this.fps = 0;
    this.drawMs = 0;
    this.sprites = {};
    this.loop = this.loop.bind(this);
    matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", (e) => {
      this.reduceMotion = e.matches;
      this.seedParticles();
    });
    new ResizeObserver(() => this.resize()).observe(canvas);
    document.addEventListener("visibilitychange", () => this.kick());
    this.resize();
  }

  /* ------------------------------------------------------------- inputs */
  setState(name) {
    this.state = STATE[name] ? name : "idle";
    if (this.state !== "listening" && this.state !== "speaking") this.levelTarget = 0;
    this.kick();
  }

  pushLevel(value) {
    const v = Math.max(0, Math.min(1, Number(value) || 0));
    this.levelTarget = v;
    this.history.copyWithin(0, 1);
    this.history[this.history.length - 1] = v;
    const now = performance.now();
    if (v > 0.42 && now - this.lastPulseAt > 260 && this.pulses.length < 6) {
      this.pulses.push({ r: 0.6, a: Math.min(0.75, v) });
      this.lastPulseAt = now;
    }
    this.kick();
  }

  setAccent(rgb) {
    this.accent = rgb;
    this.sprites = {};
    this.kick();
  }

  setQuality(q) {
    q = ["high", "balanced", "minimal"].includes(q) ? q : "high";
    if (q === this.quality) return;
    this.quality = q;
    this.canvas.width = 0;  // force a resize at the new resolution
    this.resize();
    this.seedParticles();
    this.kick();
  }

  /** visible: the window can be seen at all. active: it is the frontmost window. */
  setVisibility(visible, active) {
    this.visible = visible;
    this.active = active;
    if (visible) this.kick();
  }

  /* ----------------------------------------------------------- lifecycle */
  kick() {
    if (!this.running && this.visible && document.visibilityState === "visible") {
      this.running = true;
      this.last = performance.now();
      requestAnimationFrame(this.loop);
    }
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(this.quality === "high" ? 2 : 1.5, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (w !== this.canvas.width || h !== this.canvas.height) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.dpr = dpr;
      this.geometry();
      this.seedParticles();
      this.sprites = {};
    }
    this.kick();
  }

  geometry() {
    const { width: w, height: h } = this.canvas;
    this.cx = w / 2;
    this.cy = h / 2;
    this.R = Math.min(w, h) * 0.44;
    // Tick ring as two Path2Ds (minor/major) at radius R, drawn rotated.
    const minor = new Path2D();
    const major = new Path2D();
    const n = 120;
    for (let i = 0; i < n; i++) {
      const a = (i / n) * TAU;
      const big = i % 10 === 0;
      const r0 = this.R * (big ? 0.935 : 0.955);
      const r1 = this.R * 0.985;
      const p = big ? major : minor;
      p.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
      p.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
    }
    this.ticks = { minor, major };
  }

  seedParticles() {
    const count = this.reduceMotion ? 0 : this.quality === "high" ? 110 : this.quality === "balanced" ? 50 : 0;
    this.particles = Array.from({ length: count }, () => this.spawn(true));
  }

  spawn(anywhere) {
    return {
      a: Math.random() * TAU,
      r: anywhere ? 0.45 + Math.random() * 0.6 : 1.02 + Math.random() * 0.08,
      s: (0.08 + Math.random() * 0.22) * (Math.random() < 0.5 ? -1 : 1),
      size: 0.6 + Math.random() * 1.6,
      alpha: 0.25 + Math.random() * 0.6,
    };
  }

  glow(color, key) {
    // A radial-gradient sprite is drawn once per colour, then blitted: far cheaper than shadowBlur.
    const id = key + color.map((c) => c | 0).join(",");
    if (this.sprites[id]) return this.sprites[id];
    const size = 256;
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, rgba(color, key === "core" ? 0.95 : 0.55));
    grad.addColorStop(key === "core" ? 0.25 : 0.35, rgba(color, key === "core" ? 0.45 : 0.16));
    grad.addColorStop(1, rgba(color, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    this.sprites[id] = c;
    return c;
  }

  targetColor() {
    switch (this.state) {
      case "idle": return mix(this.accent, IDLE_BLUE, 0.55);
      case "muted":
      case "offline": return MUTED;
      case "error": return ERROR;
      case "confirm": return AMBER;
      default: return this.accent;
    }
  }

  frameBudget() {
    if (!this.visible || document.visibilityState !== "visible") return 0;
    let fps = (STATE[this.state] || STATE.idle).fps;
    const calm = fps <= 20;  // idle-like states
    if (this.quality === "balanced") fps = Math.min(fps, calm ? 15 : 30);
    if (this.quality === "minimal" || this.reduceMotion) fps = Math.min(fps, calm ? 12 : 20);
    if (!this.active) fps = Math.min(fps, 24);
    return fps;
  }

  loop(now) {
    const fps = this.frameBudget();
    if (!fps) {
      this.running = false;
      return;
    }
    requestAnimationFrame(this.loop);
    if (now - this.lastDraw < 1000 / fps - 2) return;
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    this.lastDraw = now;
    const t0 = performance.now();
    this.update(dt);
    this.draw();
    this.drawMs = lerp(this.drawMs, performance.now() - t0, 0.1);
    this.frames++;
    if (now - this.fpsWindowStart >= 1000) {
      this.fps = (this.frames * 1000) / (now - this.fpsWindowStart);
      this.frames = 0;
      this.fpsWindowStart = now;
    }
  }

  /* -------------------------------------------------------------- update */
  update(dt) {
    const spec = STATE[this.state] || STATE.idle;
    const still = this.reduceMotion;
    this.speed = lerp(this.speed, still ? 0 : spec.speed, Math.min(1, dt * 3));
    this.color = mix(this.color, this.targetColor(), Math.min(1, dt * 4));
    // Fast attack, slow release, like a VU meter.
    const k = this.levelTarget > this.level ? 18 : 5;
    this.level = lerp(this.level, this.levelTarget, Math.min(1, dt * k));
    if (this.state !== "listening" && this.state !== "speaking") this.levelTarget *= Math.pow(0.02, dt);
    this.energy = lerp(this.energy, spec.energy + this.level * 0.5, Math.min(1, dt * 4));

    const dirs = [1, -0.7, 0.45, -1.4];
    for (let i = 0; i < 4; i++) this.rot[i] = (this.rot[i] + this.speed * dirs[i] * dt) % TAU;
    if (this.state === "working") this.sweep = (this.sweep + dt * 3.2) % TAU;

    for (const p of this.pulses) {
      p.r += dt * 0.55;
      p.a *= Math.pow(0.12, dt);
    }
    this.pulses = this.pulses.filter((p) => p.r < 1.05 && p.a > 0.02);

    const streaming = this.state === "thinking" || this.state === "working";
    const mult = 0.4 + this.speed * 3;
    for (let i = 0; i < this.particles.length; i++) {
      const p = this.particles[i];
      p.a += p.s * dt * mult;
      if (streaming) {
        p.r -= dt * (0.18 + Math.abs(p.s));
        if (p.r < 0.4) this.particles[i] = this.spawn(false);
      }
    }
  }

  /* ---------------------------------------------------------------- draw */
  draw() {
    const { ctx, cx, cy, R } = this;
    const c = this.color;
    const e = this.energy;
    const dpr = this.dpr || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.globalCompositeOperation = "lighter";

    // Ambient halo.
    const halo = this.glow(c, "halo");
    const hs = R * (2.5 + this.level * 0.35);
    ctx.globalAlpha = 0.35 + e * 0.45;
    ctx.drawImage(halo, cx - hs / 2, cy - hs / 2, hs, hs);
    ctx.globalAlpha = 1;

    ctx.translate(cx, cy);

    // Outer ticks.
    ctx.save();
    ctx.rotate(this.rot[0]);
    ctx.lineWidth = 1 * dpr;
    ctx.strokeStyle = rgba(c, 0.22 + e * 0.2);
    ctx.stroke(this.ticks.minor);
    ctx.lineWidth = 1.6 * dpr;
    ctx.strokeStyle = rgba(c, 0.55 + e * 0.35);
    ctx.stroke(this.ticks.major);
    ctx.restore();

    // Thin guide circle + segmented arcs.
    ring(ctx, R * 0.905, 0.8 * dpr, rgba(c, 0.12 + e * 0.08));
    ctx.save();
    ctx.rotate(this.rot[1]);
    ctx.lineCap = "round";
    ctx.lineWidth = 2.4 * dpr;
    ctx.strokeStyle = rgba(c, 0.5 + e * 0.4);
    for (const [start, len] of [[0, 0.95], [1.35, 0.32], [2.1, 1.25], [3.85, 0.55], [4.75, 0.9]]) {
      ctx.beginPath();
      ctx.arc(0, 0, R * 0.865, start, start + len);
      ctx.stroke();
    }
    ctx.restore();

    // EXECUTING: a bright sweep that only exists while a tool runs.
    if (this.state === "working") {
      ctx.save();
      ctx.rotate(this.sweep);
      const grad = ctx.createConicGradient ? ctx.createConicGradient(0, 0, 0) : null;
      ctx.lineWidth = 3.2 * dpr;
      ctx.lineCap = "round";
      if (grad) {
        grad.addColorStop(0, rgba(c, 0));
        grad.addColorStop(0.18, rgba(c, 0.95));
        grad.addColorStop(0.19, rgba(c, 0));
        ctx.strokeStyle = grad;
      } else {
        ctx.strokeStyle = rgba(c, 0.9);
      }
      ctx.beginPath();
      ctx.arc(0, 0, R * 0.80, 0, TAU * 0.19);
      ctx.stroke();
      ctx.restore();
    }

    // Dashed data ring with orbiting packets.
    ctx.save();
    ctx.rotate(this.rot[2]);
    ctx.setLineDash([2 * dpr, 7 * dpr]);
    ring(ctx, R * 0.74, 1.2 * dpr, rgba(c, 0.32 + e * 0.25));
    ctx.setLineDash([]);
    ctx.fillStyle = rgba(c, 0.85);
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * TAU + this.rot[3] * (1 + i * 0.12);
      const r = R * 0.74;
      ctx.beginPath();
      ctx.arc(Math.cos(a) * r, Math.sin(a) * r, (1.6 + (i % 2)) * dpr, 0, TAU);
      ctx.fill();
    }
    ctx.restore();

    // Pulse rings from real voice peaks.
    for (const p of this.pulses) ring(ctx, R * p.r, 1.5 * dpr, rgba(c, p.a));

    // Waveform ring: the recent level history laid around the circle.
    const live = this.state === "listening" || this.state === "speaking";
    const n = this.history.length;
    const base = R * 0.6;
    const amp = R * 0.16 * (live ? 1 : 0.15);
    ctx.lineWidth = 1.6 * dpr;
    ctx.strokeStyle = rgba(c, 0.55 + e * 0.35);
    ctx.beginPath();
    for (let i = 0; i <= n * 2; i++) {
      // Mirror the history so the ring closes without a seam.
      const idx = i < n ? i : 2 * n - i;
      const v = this.history[Math.min(n - 1, idx)];
      const a = (i / (n * 2)) * TAU - Math.PI / 2 + this.rot[2] * 0.25;
      const r = base + v * amp * (0.6 + 0.4 * Math.sin(i * 0.9));
      const x = Math.cos(a) * r;
      const y = Math.sin(a) * r;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.stroke();
    ring(ctx, base * 0.93, 0.8 * dpr, rgba(c, 0.18));

    // Inner rings with notches.
    ctx.save();
    ctx.rotate(-this.rot[3] * 0.5);
    ctx.lineWidth = 1.2 * dpr;
    ctx.strokeStyle = rgba(c, 0.4 + e * 0.3);
    for (let i = 0; i < 3; i++) {
      ctx.beginPath();
      ctx.arc(0, 0, R * 0.43, (i / 3) * TAU, (i / 3) * TAU + 1.6);
      ctx.stroke();
    }
    ctx.restore();

    // Particles (data motes).
    if (this.particles.length) {
      ctx.fillStyle = rgba(c, 1);
      for (const p of this.particles) {
        const r = R * p.r;
        const s = p.size * dpr;
        ctx.globalAlpha = p.alpha * (0.35 + e * 0.6) * (p.r > 1 ? Math.max(0, (1.1 - p.r) * 10) : 1);
        ctx.fillRect(Math.cos(p.a) * r - s / 2, Math.sin(p.a) * r - s / 2, s, s);
      }
      ctx.globalAlpha = 1;
    }

    // Core: the brightest thing on screen, swelling with the live level.
    const core = this.glow(c, "core");
    const cs = R * (0.78 + this.level * 0.22 + (e - 0.3) * 0.12);
    ctx.globalAlpha = 0.55 + e * 0.45;
    ctx.drawImage(core, -cs / 2, -cs / 2, cs, cs);
    ctx.globalAlpha = 1;
    ring(ctx, R * 0.3, 1.4 * dpr, rgba(c, 0.6 + e * 0.3));
    ring(ctx, R * 0.18, 1 * dpr, rgba([255, 255, 255], 0.25 + this.level * 0.5));

    ctx.globalCompositeOperation = "source-over";
  }

  stats() {
    return { fps: Math.round(this.fps), drawMs: +this.drawMs.toFixed(2), budget: this.frameBudget(), state: this.state };
  }
}

function ring(ctx, r, width, style) {
  ctx.lineWidth = width;
  ctx.strokeStyle = style;
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, TAU);
  ctx.stroke();
}

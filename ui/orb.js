// The orb: ~62k additive GL_POINTS on a noise-displaced sphere.
//
// Everything that changes per frame is a uniform. The vertex buffer is built
// once and never touched again, because rebuilding 60k points per frame is the
// only thing on this machine that would actually cost us the 60fps budget.
//
// The look comes from three things stacked, not from any single trick:
//   1. a Fibonacci lattice, which gives the faint even phyllotaxis weave,
//   2. fbm displacement, which folds that weave into ridges you see through,
//   3. additive blending, so the foreshortened band where the surface turns
//      edge-on piles up into a near-white silhouette on its own.

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const N_SHELL = 60000;
const N_DUST = 2400; // ~4% loose motes outside the shell
const TAU = Math.PI * 2;

const VERT = `#version 300 es
precision highp float;

in vec3 aPos;   // unit direction on the sphere
in vec4 aAux;   // x,y = stable randoms  z = kind (0 shell, 1 dust)  w = base radius

uniform mat4 uProj;
uniform mat4 uView;
uniform mat3 uRot;
uniform float uTime;
uniform float uAmp;        // fbm displacement depth
uniform float uFreq;       // fbm spatial frequency
uniform float uNoiseSpeed; // how fast the folds evolve
uniform float uSwirl;      // shears the noise field around Y
uniform float uBreath;     // uniform radial breathing
uniform float uWave;       // travelling wave through the lattice
uniform float uRipple;     // concentric ripples from the disc centre (speech)
uniform float uSwell;      // level-driven swell (listening)
uniform float uLevel;      // smoothed 0..1 audio level
uniform float uRimGain;
uniform float uRimPow;
uniform float uRimLevel;   // how much the rim flares with level
uniform float uBright;
uniform float uSparkle;
uniform float uDust;
uniform float uDepthFade;  // brightness of the far hemisphere
uniform float uPointSize;
uniform vec3  uTint;

out vec4 vColor;

vec3 hash33(vec3 p) {
  p = fract(p * vec3(0.1031, 0.1030, 0.0973));
  p += dot(p, p.yxz + 33.33);
  return fract((p.xxy + p.yxx) * p.zyx) * 2.0 - 1.0;
}

// Gradient (Perlin) noise. Value noise shows its grid on a slow-rotating
// sphere; gradient noise does not.
float gnoise(vec3 p) {
  vec3 i = floor(p);
  vec3 f = p - i;
  vec3 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
  float n000 = dot(hash33(i + vec3(0.0, 0.0, 0.0)), f - vec3(0.0, 0.0, 0.0));
  float n100 = dot(hash33(i + vec3(1.0, 0.0, 0.0)), f - vec3(1.0, 0.0, 0.0));
  float n010 = dot(hash33(i + vec3(0.0, 1.0, 0.0)), f - vec3(0.0, 1.0, 0.0));
  float n110 = dot(hash33(i + vec3(1.0, 1.0, 0.0)), f - vec3(1.0, 1.0, 0.0));
  float n001 = dot(hash33(i + vec3(0.0, 0.0, 1.0)), f - vec3(0.0, 0.0, 1.0));
  float n101 = dot(hash33(i + vec3(1.0, 0.0, 1.0)), f - vec3(1.0, 0.0, 1.0));
  float n011 = dot(hash33(i + vec3(0.0, 1.0, 1.0)), f - vec3(0.0, 1.0, 1.0));
  float n111 = dot(hash33(i + vec3(1.0, 1.0, 1.0)), f - vec3(1.0, 1.0, 1.0));
  float x00 = mix(n000, n100, u.x);
  float x10 = mix(n010, n110, u.x);
  float x01 = mix(n001, n101, u.x);
  float x11 = mix(n011, n111, u.x);
  return 1.45 * mix(mix(x00, x10, u.y), mix(x01, x11, u.y), u.z);
}

float fbm(vec3 p) {
  float s = gnoise(p) * 0.62;
  s += gnoise(p * 2.07 + 17.3) * 0.27;
  s += gnoise(p * 4.33 + 41.7) * 0.11;
  return s;
}

void main() {
  float kind = aAux.z;
  vec3 dir = aPos;

  vec3 sp = dir * uFreq;
  float a = uSwirl * (dir.y * 1.8 + uTime * 0.5);
  float ca = cos(a), sa = sin(a);
  sp = vec3(ca * sp.x - sa * sp.z, sp.y, sa * sp.x + ca * sp.z);
  vec3 drift = vec3(uTime * uNoiseSpeed * 0.21,
                    uTime * uNoiseSpeed * 0.37,
                    -uTime * uNoiseSpeed * 0.29);
  float n = fbm(sp + drift);
  n += uWave * sin(dir.y * 6.0 - uTime * 2.4 + n * 2.0);

  vec3 nrm = uRot * dir;
  vec3 Nv = normalize(mat3(uView) * nrm);

  // Provisional placement, only so the ripple and rim terms can use a true
  // perspective view vector instead of assuming an orthographic one.
  vec3 p0 = uRot * (dir * aAux.w);
  vec3 v0 = (uView * vec4(p0, 1.0)).xyz;
  vec3 V = normalize(-v0);
  float facing = dot(Nv, V);
  float ndv = abs(facing);
  float edge = 1.0 - ndv;              // 0 at the centre of the disc, 1 at the silhouette
  float rim = pow(edge, uRimPow);

  float ripple = uRipple * 0.030 * sin(edge * 13.0 - uTime * 6.0) * (0.30 + 0.95 * uLevel);
  float r = aAux.w * (1.0 + uBreath + uAmp * n + uSwell * uLevel + ripple);

  vec3 world = uRot * (dir * r);
  vec4 vp = uView * vec4(world, 1.0);
  gl_Position = uProj * vp;

  float depth = mix(1.0, uDepthFade, step(facing, 0.0));
  float star = step(0.9985, aAux.x);
  float tw = pow(max(0.0, sin(uTime * 0.55 + aAux.y * 62.83)), 24.0);

  float lum = uBright * (0.34 + uRimGain * rim) * depth;
  // Motes read as spray off the silhouette rather than a second shell, because
  // they only light up where the surface under them is turning edge-on.
  float dustLum = uBright * uDust * rim * rim * (0.30 + 0.70 * aAux.y) * 0.8 * depth;
  lum = mix(lum, dustLum, kind);
  lum *= 1.0 + uLevel * rim * uRimLevel;
  lum += uSparkle * star * tw * 1.7 * (1.0 - kind);

  vColor = vec4(uTint, clamp(lum, 0.0, 1.0));
  gl_PointSize = uPointSize * (1.0 + rim * 0.5 + star * tw * 1.3) * mix(1.0, 0.82, kind);
}
`;

const FRAG = `#version 300 es
precision mediump float;
in vec4 vColor;
out vec4 fragColor;
void main() {
  vec2 pc = gl_PointCoord * 2.0 - 1.0;
  float d2 = dot(pc, pc);
  if (d2 > 1.0) discard;
  float fall = smoothstep(1.0, 0.10, d2);
  fragColor = vec4(vColor.rgb, vColor.a * fall);
}
`;

// Per-state targets. Every one of these is smoothed, never assigned directly.
const STATES = {
  idle: {
    amp: 0.055, freq: 1.95, noiseSpeed: 0.20, swirl: 0.05, spin: 0.055,
    breathRate: 0.42, breathAmp: 0.013, wave: 0.0, ripple: 0.0, swell: 0.0,
    rimGain: 1.00, rimPow: 3.1, rimLevel: 0.0, bright: 1.0, sparkle: 1.0,
    dust: 1.0, depthFade: 0.42, pointSize: 2.10, tint: [1.0, 1.0, 1.0],
  },
  listening: {
    amp: 0.062, freq: 2.15, noiseSpeed: 0.34, swirl: 0.10, spin: 0.075,
    breathRate: 0.75, breathAmp: 0.012, wave: 0.0, ripple: 0.18, swell: 0.075,
    rimGain: 1.30, rimPow: 2.8, rimLevel: 1.35, bright: 1.10, sparkle: 1.2,
    dust: 1.35, depthFade: 0.46, pointSize: 2.20, tint: [0.80, 0.92, 1.0],
  },
  thinking: {
    amp: 0.088, freq: 2.45, noiseSpeed: 1.05, swirl: 0.95, spin: 0.20,
    breathRate: 1.10, breathAmp: 0.010, wave: 0.050, ripple: 0.0, swell: 0.0,
    rimGain: 1.15, rimPow: 3.0, rimLevel: 0.0, bright: 1.05, sparkle: 1.5,
    dust: 1.15, depthFade: 0.50, pointSize: 2.15, tint: [0.92, 0.96, 1.0],
  },
  speaking: {
    amp: 0.068, freq: 2.05, noiseSpeed: 0.45, swirl: 0.18, spin: 0.10,
    breathRate: 0.85, breathAmp: 0.009, wave: 0.0, ripple: 1.0, swell: 0.030,
    rimGain: 1.45, rimPow: 2.7, rimLevel: 1.70, bright: 1.18, sparkle: 1.3,
    dust: 1.55, depthFade: 0.50, pointSize: 2.25, tint: [1.0, 1.0, 1.0],
  },
  working: {
    amp: 0.076, freq: 2.25, noiseSpeed: 0.46, swirl: 0.55, spin: 0.105,
    breathRate: 0.50, breathAmp: 0.015, wave: 0.028, ripple: 0.0, swell: 0.0,
    rimGain: 1.10, rimPow: 3.0, rimLevel: 0.0, bright: 1.02, sparkle: 1.1,
    dust: 1.05, depthFade: 0.46, pointSize: 2.15, tint: [0.95, 0.97, 1.0],
  },
  error: {
    amp: 0.115, freq: 3.30, noiseSpeed: 2.40, swirl: 0.35, spin: 0.085,
    breathRate: 5.5, breathAmp: 0.022, wave: 0.070, ripple: 0.0, swell: 0.0,
    rimGain: 1.35, rimPow: 2.6, rimLevel: 0.0, bright: 1.10, sparkle: 0.6,
    dust: 1.3, depthFade: 0.55, pointSize: 2.25, tint: [1.0, 0.40, 0.28],
  },
  muted: {
    amp: 0.030, freq: 1.70, noiseSpeed: 0.05, swirl: 0.0, spin: 0.012,
    breathRate: 0.22, breathAmp: 0.006, wave: 0.0, ripple: 0.0, swell: 0.0,
    rimGain: 0.75, rimPow: 3.4, rimLevel: 0.0, bright: 0.45, sparkle: 0.2,
    dust: 0.45, depthFade: 0.30, pointSize: 2.00, tint: [0.84, 0.86, 0.90],
  },
};

const SMOOTHED = Object.keys(STATES.idle).filter((k) => k !== "tint");
const ERROR_MS = 900;

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildGeometry() {
  const total = N_SHELL + N_DUST;
  const pos = new Float32Array(total * 3);
  const aux = new Float32Array(total * 4);
  const rnd = mulberry32(0x5eed1a7e); // fixed seed: the sparkles sit in the same places every launch

  for (let i = 0; i < N_SHELL; i++) {
    const y = 1 - ((i + 0.5) * 2) / N_SHELL;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const phi = i * GOLDEN_ANGLE;
    pos[i * 3] = Math.cos(phi) * r;
    pos[i * 3 + 1] = y;
    pos[i * 3 + 2] = Math.sin(phi) * r;
    aux[i * 4] = rnd();
    aux[i * 4 + 1] = rnd();
    aux[i * 4 + 2] = 0;
    aux[i * 4 + 3] = 1.0;
  }

  for (let j = 0; j < N_DUST; j++) {
    const i = N_SHELL + j;
    // Uniform on the sphere, but unstructured - motes must not inherit the lattice.
    const u = rnd() * 2 - 1;
    const th = rnd() * TAU;
    const s = Math.sqrt(Math.max(0, 1 - u * u));
    pos[i * 3] = Math.cos(th) * s;
    pos[i * 3 + 1] = u;
    pos[i * 3 + 2] = Math.sin(th) * s;
    aux[i * 4] = rnd() * 0.9; // motes never become sparkles
    aux[i * 4 + 1] = rnd();
    aux[i * 4 + 2] = 1;
    aux[i * 4 + 3] = 1.0 + Math.pow(rnd(), 2.2) * 0.34;
  }
  return { pos, aux, count: total };
}

function perspective(out, fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  out[0] = f / aspect; out[1] = 0; out[2] = 0; out[3] = 0;
  out[4] = 0; out[5] = f; out[6] = 0; out[7] = 0;
  out[8] = 0; out[9] = 0; out[10] = (far + near) / (near - far); out[11] = -1;
  out[12] = 0; out[13] = 0; out[14] = (2 * far * near) / (near - far); out[15] = 0;
  return out;
}

function translationZ(out, z) {
  out.fill(0);
  out[0] = out[5] = out[10] = out[15] = 1;
  out[14] = z;
  return out;
}

// R = Rx(tilt) * Ry(angle), column-major mat3.
function orbitMatrix(out, angle, tilt) {
  const cy = Math.cos(angle), sy = Math.sin(angle);
  const cx = Math.cos(tilt), sx = Math.sin(tilt);
  out[0] = cy;       out[1] = sy * sx;  out[2] = -sy * cx;
  out[3] = 0;        out[4] = cx;       out[5] = sx;
  out[6] = sy;       out[7] = -cy * sx; out[8] = cy * cx;
  return out;
}

class OrbRenderer {
  constructor() {
    this.canvas = null;
    this.gl = null;
    this.raf = 0;
    this.running = false;
    this.paused = false;
    this.lost = false;
    this.state = "idle";
    this.prevState = "idle";
    this.errorUntil = 0;
    this.time = 0;
    this.angle = 0;
    this.breathPhase = 0;
    this.level = 0;
    this.rawLevel = 0;
    this.lastFrame = 0;
    this.dpr = 1;
    this.geo = null;
    this.cur = { ...STATES.idle, tint: [...STATES.idle.tint] };
    this.reduced = false;
    this.proj = new Float32Array(16);
    this.view = new Float32Array(16);
    this.rot = new Float32Array(9);
    this._onResize = () => this.resize();
    this._onVisibility = () => (document.hidden ? this.pause() : this.resume());
    this._onLost = (e) => { e.preventDefault(); this.lost = true; this.stopLoop(); };
    this._onRestored = () => { this.lost = false; this.initGL(); this.startLoop(); };
    this._mql = null;
  }

  mount(canvas) {
    if (this.canvas) this.destroy();
    this.canvas = typeof canvas === "string" ? document.querySelector(canvas) : canvas;
    if (!this.canvas) throw new Error("Orb.mount: no canvas");
    this.geo = this.geo || buildGeometry();

    this._mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    this.reduced = this._mql.matches;
    this._onMotion = () => { this.reduced = this._mql.matches; };
    this._mql.addEventListener("change", this._onMotion);

    this.canvas.addEventListener("webglcontextlost", this._onLost, false);
    this.canvas.addEventListener("webglcontextrestored", this._onRestored, false);
    window.addEventListener("resize", this._onResize);
    document.addEventListener("visibilitychange", this._onVisibility);
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(this._onResize);
      this.ro.observe(this.canvas);
    }

    if (!this.initGL()) return false;
    this.startLoop();
    return true;
  }

  initGL() {
    const gl = this.canvas.getContext("webgl2", {
      alpha: true,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      powerPreference: "high-performance",
      preserveDrawingBuffer: false,
    });
    if (!gl) {
      this.canvas.classList.add("orb-unavailable");
      return false;
    }
    this.gl = gl;

    const compile = (type, src) => {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        const log = gl.getShaderInfoLog(sh);
        gl.deleteShader(sh);
        throw new Error("orb shader: " + log);
      }
      return sh;
    };
    const vs = compile(gl.VERTEX_SHADER, VERT);
    const fs = compile(gl.FRAGMENT_SHADER, FRAG);
    const prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, "aPos");
    gl.bindAttribLocation(prog, 1, "aAux");
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error("orb link: " + gl.getProgramInfoLog(prog));
    }
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    this.prog = prog;

    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.bufPos = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bufPos);
    gl.bufferData(gl.ARRAY_BUFFER, this.geo.pos, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    this.bufAux = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bufAux);
    gl.bufferData(gl.ARRAY_BUFFER, this.geo.aux, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.u = {};
    const n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) {
      const info = gl.getActiveUniform(prog, i);
      this.u[info.name] = gl.getUniformLocation(prog, info.name);
    }

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
    gl.clearColor(0, 0, 0, 0);

    this.resize();
    return true;
  }

  resize() {
    const gl = this.gl;
    if (!gl || !this.canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round((rect.width || this.canvas.clientWidth || 300) * dpr));
    const h = Math.max(1, Math.round((rect.height || this.canvas.clientHeight || 300) * dpr));
    if (w !== this.canvas.width || h !== this.canvas.height || dpr !== this.dpr) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.dpr = dpr;
    }
    gl.viewport(0, 0, w, h);
    // Coverage scales with (point size / sphere size)^2; hold it constant.
    this.scale = Math.min(1.2, Math.max(0.42, Math.min(w, h) / dpr / 440));
    // Fit the sphere to ~75% of the shorter axis whatever the window shape is.
    const aspect = w / h;
    const base = 0.70; // vertical half-angle tangent at the fit distance
    const tan = base / Math.min(1, aspect);
    const fovy = 2 * Math.atan(tan);
    perspective(this.proj, fovy, aspect, 0.1, 20);
    translationZ(this.view, -3.55);
  }

  startLoop() {
    if (this.running || this.lost || !this.gl) return;
    this.running = true;
    this.lastFrame = performance.now();
    const tick = (now) => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(tick);
      this.frame(now);
    };
    this.raf = requestAnimationFrame(tick);
  }

  stopLoop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  setState(name) {
    if (!STATES[name]) return;
    if (name === "error") {
      if (this.state !== "error") this.prevState = this.state;
      this.errorUntil = performance.now() + ERROR_MS;
    } else {
      this.errorUntil = 0;
      this.prevState = name;
    }
    this.state = name;
  }

  setLevel(v) {
    this.rawLevel = Math.max(0, Math.min(1, Number(v) || 0));
  }

  pause() {
    this.paused = true;
    this.stopLoop();
  }

  resume() {
    this.paused = false;
    if (!document.hidden) this.startLoop();
  }

  destroy() {
    this.stopLoop();
    const gl = this.gl;
    if (gl) {
      gl.deleteBuffer(this.bufPos);
      gl.deleteBuffer(this.bufAux);
      gl.deleteVertexArray(this.vao);
      gl.deleteProgram(this.prog);
    }
    if (this.canvas) {
      this.canvas.removeEventListener("webglcontextlost", this._onLost);
      this.canvas.removeEventListener("webglcontextrestored", this._onRestored);
    }
    if (this.ro) this.ro.disconnect();
    if (this._mql && this._onMotion) this._mql.removeEventListener("change", this._onMotion);
    window.removeEventListener("resize", this._onResize);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this.gl = null;
    this.canvas = null;
    this.ro = null;
  }

  frame(now) {
    const gl = this.gl;
    if (!gl) return;
    let dt = (now - this.lastFrame) / 1000;
    this.lastFrame = now;
    if (!(dt > 0)) dt = 1 / 60;
    if (dt > 0.1) dt = 0.1; // a backgrounded tab must not jump the animation

    if (this.state === "error" && this.errorUntil && now > this.errorUntil) {
      this.state = this.prevState === "error" ? "idle" : this.prevState;
      this.errorUntil = 0;
    }

    // Level: fast attack, slow release, so speech reads as motion and not jitter.
    const ltau = this.rawLevel > this.level ? 0.045 : 0.28;
    this.level += (this.rawLevel - this.level) * (1 - Math.exp(-dt / ltau));

    const target = STATES[this.state] || STATES.idle;
    const damp = this.reduced ? 0.35 : 1;
    const k = 1 - Math.exp(-dt / 0.17); // ~500ms to settle
    for (const key of SMOOTHED) {
      this.cur[key] += (target[key] - this.cur[key]) * k;
    }
    for (let i = 0; i < 3; i++) {
      this.cur.tint[i] += (target.tint[i] - this.cur.tint[i]) * k;
    }

    this.time += dt;
    this.angle += this.cur.spin * damp * dt * TAU * 0.35; // spin is roughly revolutions/sec/3
    this.breathPhase += this.cur.breathRate * damp * dt * TAU;
    const breath = Math.sin(this.breathPhase) * this.cur.breathAmp * damp;

    orbitMatrix(this.rot, this.angle, 0.26);

    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);

    const u = this.u;
    gl.uniformMatrix4fv(u.uProj, false, this.proj);
    gl.uniformMatrix4fv(u.uView, false, this.view);
    gl.uniformMatrix3fv(u.uRot, false, this.rot);
    gl.uniform1f(u.uTime, this.time);
    gl.uniform1f(u.uAmp, this.cur.amp * damp);
    gl.uniform1f(u.uFreq, this.cur.freq);
    gl.uniform1f(u.uNoiseSpeed, this.cur.noiseSpeed * damp);
    gl.uniform1f(u.uSwirl, this.cur.swirl * damp);
    gl.uniform1f(u.uBreath, breath);
    gl.uniform1f(u.uWave, this.cur.wave * damp);
    gl.uniform1f(u.uRipple, this.cur.ripple * damp);
    gl.uniform1f(u.uSwell, this.cur.swell * damp);
    gl.uniform1f(u.uLevel, this.level);
    gl.uniform1f(u.uRimGain, this.cur.rimGain);
    gl.uniform1f(u.uRimPow, this.cur.rimPow);
    gl.uniform1f(u.uRimLevel, this.cur.rimLevel);
    gl.uniform1f(u.uBright, this.cur.bright * (0.55 + 0.45 * (this.scale || 1)));
    gl.uniform1f(u.uSparkle, this.cur.sparkle);
    gl.uniform1f(u.uDust, this.cur.dust);
    gl.uniform1f(u.uDepthFade, this.cur.depthFade);
    gl.uniform1f(u.uPointSize, this.cur.pointSize * this.dpr * (this.scale || 1));
    gl.uniform3fv(u.uTint, this.cur.tint);

    gl.drawArrays(gl.POINTS, 0, this.geo.count);
    gl.bindVertexArray(null);
  }
}

const renderer = new OrbRenderer();

export const Orb = {
  mount: (canvas) => renderer.mount(canvas),
  setState: (name) => renderer.setState(name),
  setLevel: (v) => renderer.setLevel(v),
  pause: () => renderer.pause(),
  resume: () => renderer.resume(),
  destroy: () => renderer.destroy(),
  get state() { return renderer.state; },
};

window.Orb = Orb;
export default Orb;

"use strict";
/* Horizon background: the sea and Astra's flare from Astra: Zenith, made light enough to sit behind the app.
   One small WebGL2 canvas rendered at reduced resolution and at most 30 fps, paused when the window is hidden.
   The flare takes the accent colour (and so the mood colour), and sends a ripple across the sea when you send a
   message or when Astra starts answering. The middle and bottom are kept dark so text stays easy to read. */
(function () {
  const cv = document.getElementById("horizon");
  if (!cv) return;
  let gl = null, prog = null, seaProg = null, flareProg = null, quad = null, sea = null, seaCount = 0;
  let running = false, raf = 0, last = 0, t0 = performance.now();
  const SCALE = 0.6, FPS = 30;
  const pulses = new Float32Array(6).fill(-100);
  let slot = 0, tint = [0.65, 0.55, 1], nextAuto = 0;
  const reduce = () => matchMedia("(prefers-reduced-motion: reduce)").matches || document.body.classList.contains("calm");

  const NOISE = `
  float hash12(vec2 p){ vec3 p3 = fract(vec3(p.xyx) * .1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
  float vn(vec2 p){ vec2 i = floor(p), f = fract(p); f = f * f * (3. - 2. * f);
    return mix(mix(hash12(i), hash12(i + vec2(1,0)), f.x), mix(hash12(i + vec2(0,1)), hash12(i + vec2(1,1)), f.x), f.y); }`;
  const SKY = `
  uniform float uTime; uniform vec3 uTint, uFlareDir;
  vec3 stars(vec2 uv, float scale, float dens){
    vec2 p = uv * scale, c = floor(p); float h = hash12(c);
    if (h > dens) return vec3(0.);
    vec2 sp = c + .2 + .6 * vec2(hash12(c + 3.1), hash12(c + 7.7));
    float d = length(p - sp), tw = .6 + .4 * sin(uTime * (.4 + h * 2.) + h * 50.);
    return vec3(.85, .88, 1.) * (1. - smoothstep(0., .09, d)) * (.4 + h * 1.2) * tw;
  }
  vec3 sky(vec3 d){
    float y = max(d.y, 0.);
    vec3 col = mix(vec3(.03,.032,.05), vec3(.006,.006,.012), smoothstep(0., .5, y));
    col += vec3(.09,.1,.14) * exp(-y * 16.);
    vec2 uv = vec2(atan(d.x, -d.z), d.y);
    float band = exp(-pow((uv.y - .32 - uv.x * .25) * 3.2, 2.));
    float n = vn(uv * 3. + vec2(uTime * .004, 0.)) * .6 + vn(uv * 7.) * .4;
    col += mix(vec3(.25,.27,.45), uTint * .6, .4) * smoothstep(.45, .85, n) * band * .2;
    col += (stars(uv, 90., .35) * .7 + stars(uv, 38., .12)) * smoothstep(-.01, .06, d.y);
    float a = max(dot(d, uFlareDir), 0.);
    col += uTint * pow(a, 24.) * .05;
    return col;
  }`;
  const VQ = `#version 300 es
  layout(location=0) in vec2 aP; out vec2 vUv; void main(){ vUv = aP * .5 + .5; gl_Position = vec4(aP, 0., 1.); }`;

  function sh(type, src) { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; }
  function program(vs, fs) { const p = gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, "#version 300 es\nprecision highp float;\n" + fs)); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) { const a = gl.getActiveUniform(p, i); u[a.name.replace(/\[0\]$/, "")] = gl.getUniformLocation(p, a.name); }
    return { p, u }; }

  /* camera: fixed, eye 1.6 m above the water, looking at the horizon */
  const EYE = [0, 1.6, 8], FLARE = [5.5, 2.6, -40];
  function viewProj(aspect) {
    const f = 1 / Math.tan(50 * Math.PI / 360), n = .1, fa = 600, nf = 1 / (n - fa);
    const P = [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (fa + n) * nf, -1, 0, 0, 2 * fa * n * nf, 0];
    const pitch = .06, c = Math.cos(pitch), s = Math.sin(pitch);       // look slightly up: horizon below the middle
    const V = [1, 0, 0, 0, 0, c, -s, 0, 0, s, c, 0, 0, -(c * EYE[1] + s * EYE[2]), -(-s * EYE[1] + c * EYE[2]), 1];
    const M = new Float32Array(16);
    for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) { let x = 0; for (let k = 0; k < 4; k++) x += P[k * 4 + j] * V[i * 4 + k]; M[i * 4 + j] = x; }
    return { M, pitch, f };
  }

  function init() {
    gl = cv.getContext("webgl2", { antialias: false, alpha: false, powerPreference: "low-power", preserveDrawingBuffer: false });
    if (!gl) return false;
    prog = program(VQ, NOISE + SKY + `uniform vec2 uRes; uniform float uPitch, uF, uAspect; in vec2 vUv; out vec4 o;
      void main(){ vec2 q = vUv * 2. - 1.; vec3 d = normalize(vec3(q.x * uAspect / uF, q.y / uF, -1.));
        float c = cos(uPitch), s = sin(uPitch); d = vec3(d.x, c * d.y - s * d.z, s * d.y + c * d.z);
        o = vec4(sky(d), 1.); }`);
    seaProg = program(`#version 300 es
      layout(location=0) in vec2 aXZ; uniform mat4 uVP; uniform float uTime, uPT[6]; uniform vec3 uFlare; out vec3 vW;
      float h(vec2 p){ float d = length(p - uFlare.xz), y = sin(d * 1.6 - uTime * 1.1) * .035 / (1. + d * .06);
        y += sin(p.x * .6 + uTime * .3) * sin(p.y * .5 - uTime * .26) * .03;
        for (int i = 0; i < 6; i++){ float a = uTime - uPT[i]; if (a < 0. || a > 12.) continue; float r = a * 4.5;
          y += exp(-pow(d - r, 2.) * 1.2) * .22 * exp(-a * .3) * sin((d - r) * 5.); }
        return y; }
      void main(){ vec3 p = vec3(aXZ.x, h(aXZ), aXZ.y); vW = p; gl_Position = uVP * vec4(p, 1.); }`,
      NOISE + SKY + `uniform vec3 uEye, uFlare; uniform float uPT[6]; in vec3 vW; out vec4 o;
      float h(vec2 p){ float d = length(p - uFlare.xz), y = sin(d * 1.6 - uTime * 1.1) * .035 / (1. + d * .06);
        y += sin(p.x * .6 + uTime * .3) * sin(p.y * .5 - uTime * .26) * .03;
        for (int i = 0; i < 6; i++){ float a = uTime - uPT[i]; if (a < 0. || a > 12.) continue; float r = a * 4.5;
          y += exp(-pow(d - r, 2.) * 1.2) * .22 * exp(-a * .3) * sin((d - r) * 5.); }
        return y; }
      float crest(vec2 p){ float d = length(p - uFlare.xz), e = 0.;
        for (int i = 0; i < 6; i++){ float a = uTime - uPT[i]; if (a < 0. || a > 12.) continue; e += exp(-pow(d - a * 4.5, 2.) * 2.) * exp(-a * .35); }
        return e; }
      void main(){
        vec2 p = vW.xz; float e = .05, dist = length(vW - uEye);
        float m = .06 / (1. + dist * .05);
        float mx = (vn(p * 1.8 + uTime * vec2(.3, .2)) - vn(p * 1.8 + vec2(e, 0.) + uTime * vec2(.3, .2))) * m;
        float mz = (vn(p * 1.8 + uTime * vec2(.3, .2)) - vn(p * 1.8 + vec2(0., e) + uTime * vec2(.3, .2))) * m;
        vec3 n = normalize(vec3(-(h(p + vec2(e, 0.)) - h(p - vec2(e, 0.))) / (2. * e) - mx / e, 1., -(h(p + vec2(0., e)) - h(p - vec2(0., e))) / (2. * e) - mz / e));
        vec3 v = normalize(uEye - vW), r = reflect(-v, n); r.y = abs(r.y);
        float fr = .03 + .97 * pow(1. - max(dot(n, v), 0.), 5.);
        vec3 col = sky(r) * (.2 + fr) + vec3(.004, .005, .009);
        float g = max(dot(r, normalize(uFlare - vW)), 0.);
        col += (vec3(.9) + uTint * .4) * (pow(g, 900.) * 3. + pow(g, 120.) * .35 + pow(g, 30.) * .03);
        col += uTint * crest(p) * .5 / (1. + length(p - uFlare.xz) * .05);
        float fog = 1. - exp(-pow(dist * .011, 2.)); col = mix(col, vec3(.05, .055, .075) + uTint * .015, fog);
        o = vec4(col, 1.); }`);
    flareProg = program(VQ, NOISE + `uniform vec2 uPos; uniform float uAspect, uTime, uGlow; uniform vec3 uTint; in vec2 vUv; out vec4 o;
      float ring(float x, float n){ float i = floor(x), f = fract(x); return mix(hash12(vec2(mod(i, n), n)), hash12(vec2(mod(i + 1., n), n)), f * f * (3. - 2. * f)); }
      void main(){
        vec2 d = vUv - uPos; d.x *= uAspect; float r = length(d), a = atan(d.y, d.x) / 6.28318;
        vec3 c = vec3(1.) * exp(-r * 140.) * 2.2 + mix(vec3(1.), uTint, .4) * exp(-r * 20.) * (.25 + uGlow * .25) + uTint * exp(-r * 5.) * .06;
        float rays = pow(ring(fract(a + uTime * .01) * 60., 60.), 8.) + pow(ring(fract(a - uTime * .016) * 110., 110.), 12.) * 1.3;
        c += mix(vec3(1.), uTint, .3) * rays * exp(-r * 13.) * (.08 + uGlow * .08);
        c += vec3(.75, .85, 1.) * exp(-abs(d.y) * 380.) * exp(-abs(d.x) * 3.) * .22;
        /* keep the reading area calm: darken the middle and the bottom where chats and settings are */
        vec2 q = vUv - vec2(.5, .38); float scrim = smoothstep(.75, .05, length(q * vec2(.9, 1.3)));
        o = vec4(c, scrim * .55); }`);
    quad = gl.createVertexArray(); gl.bindVertexArray(quad);
    const qb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, qb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    /* a modest grid: dense near you, sparse toward the horizon */
    const NX = 170, NZ = 120, pos = new Float32Array((NX + 1) * (NZ + 1) * 2); let k = 0;
    for (let j = 0; j <= NZ; j++) for (let i = 0; i <= NX; i++) { const u = i / NX * 2 - 1, v = j / NZ;
      pos[k++] = Math.sign(u) * Math.pow(Math.abs(u), 1.5) * 160; pos[k++] = 10 - Math.pow(v, 1.8) * 230; }
    sea = gl.createVertexArray(); gl.bindVertexArray(sea);
    const sb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, sb); gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const idx = new Uint32Array(NX * NZ * 6); k = 0;
    for (let j = 0; j < NZ; j++) for (let i = 0; i < NX; i++) { const a = j * (NX + 1) + i, b = a + 1, c = a + NX + 1, d = c + 1; idx.set([a, c, b, b, c, d], k); k += 6; }
    const ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    seaCount = idx.length; gl.bindVertexArray(null);
    return true;
  }

  function readTint() {
    const v = getComputedStyle(document.body).getPropertyValue("--lav").trim().split(/[,\s]+/).map(Number);
    if (v.length >= 3 && v.every(x => !isNaN(x))) tint = [v[0] / 255, v[1] / 255, v[2] / 255];
  }
  function resize() {
    const w = Math.max(2, Math.round(innerWidth * SCALE)), h = Math.max(2, Math.round(innerHeight * SCALE));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  }
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (document.hidden) return;
    const minGap = reduce() ? 1000 : 1000 / FPS;
    if (now - last < minGap - 2) return;
    last = now; resize();
    const T = (now - t0) / 1000 * (reduce() ? .2 : 1);
    if (T > nextAuto) { pulse(.6); nextAuto = T + 6 + Math.random() * 3; }
    const W = cv.width, H = cv.height, aspect = W / H, vp = viewProj(aspect);
    gl.viewport(0, 0, W, H); gl.disable(gl.BLEND); gl.disable(gl.DEPTH_TEST);
    const fdir = (() => { const d = [FLARE[0] - EYE[0], FLARE[1] - EYE[1], FLARE[2] - EYE[2]], l = Math.hypot(...d); return d.map(x => x / l); })();
    gl.useProgram(prog.p); gl.uniform1f(prog.u.uTime, T); gl.uniform3fv(prog.u.uTint, tint); gl.uniform3fv(prog.u.uFlareDir, fdir);
    gl.uniform1f(prog.u.uPitch, vp.pitch); gl.uniform1f(prog.u.uF, vp.f); gl.uniform1f(prog.u.uAspect, aspect);
    gl.bindVertexArray(quad); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.useProgram(seaProg.p); gl.uniformMatrix4fv(seaProg.u.uVP, false, vp.M); gl.uniform1f(seaProg.u.uTime, T);
    gl.uniform1fv(seaProg.u.uPT, pulses); gl.uniform3fv(seaProg.u.uFlare, FLARE); gl.uniform3fv(seaProg.u.uEye, EYE);
    gl.uniform3fv(seaProg.u.uTint, tint); gl.uniform3fv(seaProg.u.uFlareDir, fdir);
    gl.bindVertexArray(sea); gl.drawElements(gl.TRIANGLES, seaCount, gl.UNSIGNED_INT, 0);
    /* flare on top (additive) and the reading scrim (darkening) in one pass */
    const M = vp.M, p = FLARE, x = M[0] * p[0] + M[4] * p[1] + M[8] * p[2] + M[12], y = M[1] * p[0] + M[5] * p[1] + M[9] * p[2] + M[13],
      w = M[3] * p[0] + M[7] * p[1] + M[11] * p[2] + M[15];
    let glow = 0; for (const t of pulses) { const a = T - t; if (a >= 0 && a < 3) glow += Math.exp(-a * 2.5); }
    gl.enable(gl.BLEND); gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_ALPHA, gl.ZERO, gl.ONE);
    gl.useProgram(flareProg.p); gl.uniform2f(flareProg.u.uPos, x / w * .5 + .5, y / w * .5 + .5); gl.uniform1f(flareProg.u.uAspect, aspect);
    gl.uniform1f(flareProg.u.uTime, T); gl.uniform1f(flareProg.u.uGlow, Math.min(glow, 1.5)); gl.uniform3fv(flareProg.u.uTint, tint);
    gl.bindVertexArray(quad); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
  function pulse(a) { pulses[slot] = (performance.now() - t0) / 1000 * (reduce() ? .2 : 1); slot = (slot + 1) % pulses.length; }
  let tintTimer = 0;
  window.Horizon = {
    start() {
      if (running) return true;
      try { if (!gl && !init()) return false; } catch (e) { console.error(e); return false; }
      running = true; cv.classList.add("on"); readTint(); tintTimer = setInterval(readTint, 400); last = 0; raf = requestAnimationFrame(frame); return true;
    },
    stop() { running = false; cv.classList.remove("on"); cancelAnimationFrame(raf); clearInterval(tintTimer); },
    pulse() { if (running) pulse(1); },
    get running() { return running; },
  };
})();

"use strict";
/* Ethereal sky: twinkling sparkles, drifting stardust, rare shooting stars, the glass raindrop, star bursts. */
(function () {
  const cv = document.getElementById("sky");
  const ctx = cv.getContext("2d");
  let COLORS = ["167,139,250", "196,181,253", "165,180,252", "232,170,250", "139,122,220"];
  let BOOST = 1;
  function readPalette() {
    const cs = getComputedStyle(document.body), v = n => cs.getPropertyValue(n).trim().replace(/\s+/g, "");
    const lav = v("--lav") || "167,139,250", peri = v("--peri") || "165,180,252", rose = v("--rose") || "232,170,250";
    const dark = document.body.classList.contains("dark");
    COLORS = dark ? [lav, peri, rose, "236,232,255", "255,255,255"] : [lav, peri, rose, lav, peri];
    BOOST = dark ? 1.7 : 1;
  }
  let W = 0, H = 0, dpr = 1, stars = [], dust = [], shoot = null, nextShoot = 0, mx = 0, my = 0, last = 0;
  const calm = () => document.body.classList.contains("calm");
  const rnd = (a, b) => a + Math.random() * (b - a);

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.min(Math.round(W * H / 8500), 360);
    stars = Array.from({ length: n }, () => {
      const big = Math.random() < 0.1;
      return { x: Math.random() * W, y: Math.random() * H, r: big ? rnd(1.8, 3.6) : rnd(0.4, 1.3), big,
        ci: Math.random() * 5 | 0, p: Math.random() * 6.28, s: rnd(0.3, 1.1), a: big ? rnd(0.25, 0.55) : rnd(0.12, 0.42),
        z: rnd(0.2, 1) };
    });
    dust = Array.from({ length: Math.round(n / 5) }, () => ({ x: Math.random() * W, y: Math.random() * H, r: rnd(0.6, 1.8),
      vx: rnd(-0.05, 0.05), vy: rnd(-0.18, -0.04), a: rnd(0.05, 0.16), ci: Math.random() * 5 | 0 }));
    draw(performance.now(), true);
  }

  function sparkle(x, y, r, a, c) {
    const R = r * 2.6;
    const g = ctx.createRadialGradient(x, y, 0, x, y, R * 1.6);
    g.addColorStop(0, `rgba(${c},${a * 0.45})`); g.addColorStop(1, `rgba(${c},0)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, R * 1.6, 0, 6.283); ctx.fill();
    ctx.fillStyle = `rgba(${c},${a})`;
    ctx.beginPath(); ctx.moveTo(x, y - R);
    ctx.quadraticCurveTo(x + r * 0.25, y - r * 0.25, x + R, y);
    ctx.quadraticCurveTo(x + r * 0.25, y + r * 0.25, x, y + R);
    ctx.quadraticCurveTo(x - r * 0.25, y + r * 0.25, x - R, y);
    ctx.quadraticCurveTo(x - r * 0.25, y - r * 0.25, x, y - R);
    ctx.fill();
  }

  function draw(t, still) {
    ctx.clearRect(0, 0, W, H);
    const px = (mx / W - 0.5) * 14, py = (my / H - 0.5) * 10;
    for (const s of stars) {
      const tw = still ? 0.8 : 0.55 + 0.45 * Math.sin(t / 1000 * s.s + s.p);
      const x = s.x - px * s.z, y = s.y - py * s.z;
      const c = COLORS[s.ci], a = Math.min(1, s.a * tw * BOOST);
      if (s.big) sparkle(x, y, s.r * (0.85 + tw * 0.25), a, c);
      else { ctx.fillStyle = `rgba(${c},${a})`; ctx.beginPath(); ctx.arc(x, y, s.r, 0, 6.283); ctx.fill(); }
    }
    for (const d of dust) {
      if (!still) { d.x += d.vx; d.y += d.vy; if (d.y < -5) { d.y = H + 5; d.x = Math.random() * W; } }
      ctx.fillStyle = `rgba(${COLORS[d.ci]},${Math.min(1, d.a * BOOST)})`; ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, 6.283); ctx.fill();
    }
    if (!still && shoot) {
      shoot.k += 0.018;
      const k = shoot.k, x = shoot.x + shoot.dx * k, y = shoot.y + shoot.dy * k;
      const tail = ctx.createLinearGradient(x, y, x - shoot.dx * 0.18, y - shoot.dy * 0.18);
      const fade = Math.sin(Math.min(k, 1) * Math.PI);
      tail.addColorStop(0, `rgba(${COLORS[0]},${0.6 * fade})`); tail.addColorStop(1, `rgba(${COLORS[0]},0)`);
      ctx.strokeStyle = tail; ctx.lineWidth = 1.4; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - shoot.dx * 0.18, y - shoot.dy * 0.18); ctx.stroke();
      sparkle(x, y, 1.6, 0.8 * fade, COLORS[1]);
      if (k >= 1) shoot = null;
    }
  }

  function loop(t) {
    requestAnimationFrame(loop);
    if (document.hidden || calm() || t - last < 33) return;  // ~30 fps is plenty for a calm sky
    last = t;
    if (!shoot && t > nextShoot) {
      if (nextShoot) shoot = { x: rnd(W * 0.2, W * 0.9), y: rnd(0, H * 0.35), dx: -rnd(W * 0.25, W * 0.4), dy: rnd(H * 0.15, H * 0.3), k: 0 };
      nextShoot = t + rnd(14000, 30000);
    }
    draw(t, false);
  }

  addEventListener("resize", resize);
  addEventListener("mousemove", e => { mx = e.clientX; my = e.clientY; }, { passive: true });

  /* ---- the jelly star: soft rounded star that wobbles, glossy, bubbles inside, sparkles on its edge ---- */
  function starPoints(cx, cy, R, r, jit, rng) {
    const pts = [];
    for (let i = 0; i < 10; i++) {
      const a = -Math.PI / 2 + i * Math.PI / 5 + (jit ? rng(-0.035, 0.035) : 0);
      const rad = (i % 2 ? r : R) + (jit ? rng(-jit, jit) : 0);
      pts.push([cx + Math.cos(a) * rad, cy + Math.sin(a) * rad]);
    }
    return pts;
  }
  function smoothPath(pts, t = 0.32) {   // closed Catmull-Rom -> cubic Bezier: rounded, gummy corners
    const n = pts.length, f = v => v.toFixed(1);
    let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
    for (let i = 0; i < n; i++) {
      const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
      const c1 = [p1[0] + (p2[0] - p0[0]) * t, p1[1] + (p2[1] - p0[1]) * t];
      const c2 = [p2[0] - (p3[0] - p1[0]) * t, p2[1] - (p3[1] - p1[1]) * t];
      d += ` C${f(c1[0])} ${f(c1[1])} ${f(c2[0])} ${f(c2[1])} ${f(p2[0])} ${f(p2[1])}`;
    }
    return d + " Z";
  }
  let jellyN = 0;
  function jelly(el) {
    if (!el) return;
    const id = "j" + (++jellyN), cx = 110, cy = 118;
    const base = starPoints(cx, cy, 92, 50, 0, rnd);
    const A = smoothPath(base), B = smoothPath(starPoints(cx, cy, 92, 50, 5, rnd)), C = smoothPath(starPoints(cx, cy, 92, 50, 5, rnd));
    const inner = smoothPath(starPoints(cx, cy - 4, 74, 40, 0, rnd));
    el.innerHTML = `
      <svg class="jelly" viewBox="0 0 220 230" aria-hidden="true">
        <defs>
          <radialGradient id="${id}f" cx=".4" cy=".36" r=".72">
            <stop offset="0" style="stop-color:#fff;stop-opacity:.95"/>
            <stop offset=".45" style="stop-color:rgb(var(--lav));stop-opacity:.42"/>
            <stop offset=".85" style="stop-color:rgb(var(--peri));stop-opacity:.62"/>
            <stop offset="1" style="stop-color:rgb(var(--rose));stop-opacity:.7"/>
          </radialGradient>
          <linearGradient id="${id}s" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" style="stop-color:#fff;stop-opacity:.95"/><stop offset=".5" style="stop-color:rgb(var(--lav));stop-opacity:.7"/>
            <stop offset="1" style="stop-color:rgb(var(--rose));stop-opacity:.9"/>
          </linearGradient>
          <radialGradient id="${id}u" cx=".5" cy=".5" r=".5">
            <stop offset="0" style="stop-color:rgb(var(--rose));stop-opacity:.55"/><stop offset="1" style="stop-color:rgb(var(--rose));stop-opacity:0"/>
          </radialGradient>
          <filter id="${id}b" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="13"/></filter>
          <filter id="${id}s2" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2"/></filter>
          <clipPath id="${id}c"><path d="${A}"/></clipPath>
        </defs>
        <path class="glow" d="${A}" style="fill:rgb(var(--lav))" opacity=".5" filter="url(#${id}b)"/>
        <g class="squish">
          <path d="${A}" fill="url(#${id}f)" stroke="url(#${id}s)" stroke-width="1.6">
            <animate attributeName="d" dur="5.5s" repeatCount="indefinite" values="${A};${B};${C};${A}"
              calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1;.45 0 .55 1"/>
          </path>
          <g clip-path="url(#${id}c)">
            <ellipse cx="${cx + 18}" cy="${cy + 38}" rx="58" ry="34" fill="url(#${id}u)"/>
            <path d="${inner}" fill="none" stroke="#fff" stroke-opacity=".55" stroke-width="2.4" filter="url(#${id}s2)"/>
            <g class="bubbles"></g>
            <g class="inner-stars"></g>
          </g>
          <path d="M${cx - 34} ${cy - 22} C ${cx - 26} ${cy - 46}, ${cx - 6} ${cy - 62}, ${cx + 2} ${cy - 70}" fill="none"
                stroke="#fff" stroke-opacity=".9" stroke-width="7" stroke-linecap="round" filter="url(#${id}s2)"/>
          <ellipse cx="${cx - 44}" cy="${cy - 4}" rx="5" ry="9" fill="#fff" opacity=".85" transform="rotate(-30 ${cx - 44} ${cy - 4})"/>
          <circle cx="${cx + 30}" cy="${cy - 30}" r="3" fill="#fff" opacity=".8"/>
        </g>
        <g class="edge"></g>
      </svg>`;
    const NS = "http://www.w3.org/2000/svg";
    const path = el.querySelector(".squish > path"), L = path.getTotalLength();
    const edge = el.querySelector(".edge"), bub = el.querySelector(".bubbles"), ins = el.querySelector(".inner-stars");
    const cols = ["rgb(var(--lav))", "rgb(var(--rose))", "rgb(var(--peri))", "#ffffff"];
    for (let i = 0; i < 26; i++) {
      const pt = path.getPointAtLength((i / 26) * L + rnd(-3, 3));
      const dx = pt.x - cx, dy = pt.y - cy, len = Math.hypot(dx, dy) || 1, out = rnd(0, 10);
      const sz = i % 6 === 0 ? rnd(11, 16) : rnd(4, 8);
      const u = document.createElementNS(NS, "use");
      u.setAttribute("href", "#star4");
      u.setAttribute("x", pt.x + dx / len * out - sz / 2); u.setAttribute("y", pt.y + dy / len * out - sz / 2);
      u.setAttribute("width", sz); u.setAttribute("height", sz); u.setAttribute("class", "tw" + (i % 3));
      u.style.animationDelay = rnd(0, 4).toFixed(2) + "s"; u.style.fill = cols[i % 4];
      edge.appendChild(u);
    }
    for (let i = 0; i < 7; i++) {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", rnd(cx - 40, cx + 40)); c.setAttribute("cy", rnd(cy + 10, cy + 60)); c.setAttribute("r", rnd(1.5, 4));
      c.setAttribute("fill", "none"); c.setAttribute("stroke", "#fff"); c.setAttribute("stroke-opacity", ".8"); c.setAttribute("stroke-width", "1");
      c.setAttribute("class", "bub"); c.style.animationDelay = (-rnd(0, 7)).toFixed(2) + "s";
      bub.appendChild(c);
    }
    for (let i = 0; i < 6; i++) {
      const sz = rnd(4, 8), u = document.createElementNS(NS, "use");
      u.setAttribute("href", "#star4"); u.setAttribute("x", rnd(cx - 50, cx + 40)); u.setAttribute("y", rnd(cy - 30, cy + 40));
      u.setAttribute("width", sz); u.setAttribute("height", sz); u.setAttribute("class", "tw" + (i % 3));
      u.style.fill = "#fff"; u.style.opacity = ".8"; u.style.animationDelay = rnd(0, 4).toFixed(2) + "s";
      ins.appendChild(u);
    }
  }


  /* ---- the lens flare (replaces the jelly star): bright core, anamorphic streaks, slow rays, ring, and ghosts
     that slide along the flare axis when the mouse moves, like a real lens ---- */
  let flareN = 0;
  const flares = [];
  function flare(el) {
    if (!el) return;
    const id = "f" + (++flareN), W2 = 400, H2 = 240, cx = 200, cy = 120;
    const ray = (a, len, w, op) => `<path d="M${cx} ${cy - w}L${cx + len} ${cy}L${cx} ${cy + w}Z" transform="rotate(${a} ${cx} ${cy})" opacity="${op}"/>`;
    let rays = "", spinA = "", spinB = "";
    for (let i = 0; i < 12; i++) rays += ray(i * 30 + rnd(-6, 6), rnd(60, 110), rnd(1.2, 2.6), rnd(.12, .3).toFixed(2));
    // long, thin, tapered rays that turn slowly: the anime-style starburst, kept soft so it still reads as light
    for (let i = 0; i < 18; i++) spinA += ray(i * 20 + rnd(-4, 4), rnd(90, 190) * (i % 3 === 0 ? 1.15 : .8), rnd(.5, 1.1), rnd(.1, .32).toFixed(2));
    for (let i = 0; i < 10; i++) spinB += ray(i * 36 + 9 + rnd(-6, 6), rnd(70, 140), rnd(.8, 1.6), rnd(.08, .2).toFixed(2));
    const hex = (x, y, r, cls, fill, op) => {
      const p = [...Array(6)].map((_, k) => `${(x + r * Math.cos(Math.PI / 3 * k)).toFixed(1)},${(y + r * Math.sin(Math.PI / 3 * k)).toFixed(1)}`).join(" ");
      return `<polygon class="${cls}" points="${p}" style="fill:${fill}" opacity="${op}"/>`;
    };
    el.innerHTML = `
      <svg class="flare" viewBox="0 0 ${W2} ${H2}" aria-hidden="true">
        <defs>
          <radialGradient id="${id}b"><stop offset="0" style="stop-color:#fff;stop-opacity:1"/>
            <stop offset=".12" style="stop-color:#fff;stop-opacity:.9"/>
            <stop offset=".35" style="stop-color:rgb(var(--lav));stop-opacity:.45"/>
            <stop offset=".7" style="stop-color:rgb(var(--peri));stop-opacity:.12"/>
            <stop offset="1" style="stop-color:rgb(var(--peri));stop-opacity:0"/></radialGradient>
          <linearGradient id="${id}s" x1="0" x2="1"><stop offset="0" style="stop-color:rgb(var(--peri));stop-opacity:0"/>
            <stop offset=".3" style="stop-color:rgb(var(--lav));stop-opacity:.55"/><stop offset=".5" style="stop-color:#fff;stop-opacity:1"/>
            <stop offset=".7" style="stop-color:rgb(var(--rose));stop-opacity:.55"/><stop offset="1" style="stop-color:rgb(var(--rose));stop-opacity:0"/></linearGradient>
          <radialGradient id="${id}g"><stop offset=".55" style="stop-color:rgb(var(--lav));stop-opacity:0"/>
            <stop offset=".8" style="stop-color:rgb(var(--lav));stop-opacity:.35"/><stop offset=".92" style="stop-color:rgb(var(--rose));stop-opacity:.45"/>
            <stop offset="1" style="stop-color:rgb(var(--peri));stop-opacity:0"/></radialGradient>
          <filter id="${id}f" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter>
          <filter id="${id}h" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.1"/></filter>
          <filter id="${id}F" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="9"/></filter>
        </defs>
        <circle class="bloom" cx="${cx}" cy="${cy}" r="92" fill="url(#${id}b)"/>
        <g class="spin spin-a" style="fill:#fff" filter="url(#${id}h)">${spinA}</g>
        <g class="spin spin-b" style="fill:rgb(var(--lav))" filter="url(#${id}f)">${spinB}</g>
        <g class="rays" style="fill:rgb(var(--lav))" filter="url(#${id}f)">${rays}</g>
        <circle class="ring" cx="${cx}" cy="${cy}" r="62" fill="url(#${id}g)"/>
        <rect class="streak" x="0" y="${cy - 4}" width="${W2}" height="8" rx="4" fill="url(#${id}s)" filter="url(#${id}F)"/>
        <rect class="streak" x="10" y="${cy - 1}" width="${W2 - 20}" height="2" rx="1" fill="url(#${id}s)"/>
        <rect class="streak2" x="${cx - 70}" y="${cy - .6}" width="140" height="1.2" rx=".6" fill="#fff" opacity=".8" transform="rotate(-62 ${cx} ${cy})"/>
        <g class="core">
          <path d="M${cx} ${cy - 46}L${cx + 3.2} ${cy - 3.2}L${cx + 46} ${cy}L${cx + 3.2} ${cy + 3.2}L${cx} ${cy + 46}L${cx - 3.2} ${cy + 3.2}L${cx - 46} ${cy}L${cx - 3.2} ${cy - 3.2}Z" fill="#fff" filter="url(#${id}f)" opacity=".9"/>
          <path d="M${cx} ${cy - 40}L${cx + 2.2} ${cy - 2.2}L${cx + 40} ${cy}L${cx + 2.2} ${cy + 2.2}L${cx} ${cy + 40}L${cx - 2.2} ${cy + 2.2}L${cx - 40} ${cy}L${cx - 2.2} ${cy - 2.2}Z" fill="#fff"/>
          <circle cx="${cx}" cy="${cy}" r="7" fill="#fff"/>
        </g>
        <g class="ghosts">
          ${hex(cx + 70, cy - 34, 13, "ghost", "rgb(var(--peri))", .32)}
          ${hex(cx + 104, cy - 52, 6, "ghost", "rgb(var(--lav))", .45)}
          ${hex(cx - 76, cy + 38, 20, "ghost", "rgb(var(--rose))", .2)}
          <circle class="ghost" cx="${cx - 118}" cy="${cy + 58}" r="4" style="fill:rgb(var(--peri))" opacity=".5"/>
        </g>
      </svg>`;
    const g = el.querySelector(".ghosts");
    flares.push({ el, g });
    return el.firstElementChild;
  }
  // ghosts slide along the flare's axis with the mouse, like reflections inside a lens
  addEventListener("mousemove", e => {
    if (calm()) return;
    for (const f of flares) {
      if (!f.el.isConnected) continue;
      const r = f.el.getBoundingClientRect();
      const dx = (e.clientX - (r.left + r.width / 2)) / innerWidth, dy = (e.clientY - (r.top + r.height / 2)) / innerHeight;
      f.g.style.transform = `translate(${(-dx * 40).toFixed(1)}px, ${(-dy * 26).toFixed(1)}px)`;
    }
  }, { passive: true });

  /* ---- star burst (on send etc.) ---- */
  function burst(x, y, n = 9) {
    if (calm()) return;
    for (let i = 0; i < n; i++) {
      const s = document.createElement("i");
      s.className = "spark";
      const ang = (i / n) * 6.283 + rnd(-0.3, 0.3), dist = rnd(26, 58);
      s.style.left = x + "px"; s.style.top = y + "px";
      s.style.setProperty("--dx", Math.cos(ang) * dist + "px"); s.style.setProperty("--dy", Math.sin(ang) * dist + "px");
      s.style.setProperty("--sz", rnd(6, 12) + "px");
      document.body.appendChild(s);
      setTimeout(() => s.remove(), 950);
    }
  }

  window.Sky = { jelly: flare, drop: flare, flare, burst, redraw: () => draw(performance.now(), true), refresh: () => { readPalette(); draw(performance.now(), true); } };
  readPalette();
  resize();
  requestAnimationFrame(loop);
})();

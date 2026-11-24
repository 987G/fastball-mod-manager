// Window backdrop: drifting smoke cut to dots, after the Smoke Break theme. Soft blobs drift across a grey field; a fixed
// 8x8 Bayer tile cuts the field to one bit, so dots stay on the grid and only the brightness under them moves. Lit dots
// are teal; where the smoke is thickest they turn mint. One canvas pixel = one dot (CELL device px), scaled up crisp.
// It moves only while the window has focus, so it costs nothing while the game is being played.
(() => {
  "use strict";
  const canvas = document.querySelector(".smoke");
  if (!canvas || !canvas.getContext) return;
  const host = canvas.parentElement;

  const CELL = 2;                 // device px per dot
  const FPS = 10;                 // drift steps a second (Smoke Break uses 12)
  const FIELD = 4;                // the soft field is drawn at 1/4 of the dot grid, then smoothed up
  const DOT = [24, 104, 104];     // teal dot
  const EMBER = [74, 222, 176];   // mint dot, where the smoke is thickest
  const LEVEL = 0.2;              // grey under the blobs: about a third of the dots lit, as at the old bottom edge
  const MINT = 1.2;               // how readily a lit dot under a mint patch turns mint
  const still = matchMedia("(prefers-reduced-motion: reduce)");

  const hash = (x, y) => {
    let n = (Math.imul(x, 374761393) + Math.imul(y, 668265263)) | 0;
    n = Math.imul(n ^ (n >>> 13), 1274126177);
    return ((n ^ (n >>> 16)) >>> 0) / 4294967296;
  };
  const B8 = Array.from({ length: 8 }, (_, y) => Array.from({ length: 8 }, (_, x) => {
    let v = 0;
    for (let b = 0; b < 3; b++) {
      const xb = (x >> b) & 1, yb = (y >> b) & 1;
      v |= ((xb ^ yb) << (2 * (2 - b) + 1)) | (yb << (2 * (2 - b)));
    }
    return (v + 0.5) / 64;
  }));

  const field = document.createElement("canvas");
  const fctx = field.getContext("2d", { willReadFrequently: true });
  const ember = document.createElement("canvas");
  const ectx = ember.getContext("2d");
  const grid = document.createElement("canvas");
  const gctx = grid.getContext("2d", { willReadFrequently: true });
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, out = null, timer = 0;
  const t0 = performance.now() / 1000 - 37;   // start mid-drift, not at the layout's seams

  function size() {
    const dpr = window.devicePixelRatio || 1;
    // the canvas's CSS box is its host's (style.css); only its pixel count follows the host, so a resize here can
    // never change the layout and feed back into the observer
    const w = Math.max(1, Math.round(host.clientWidth * dpr / CELL));
    const h = Math.max(1, Math.round(host.clientHeight * dpr / CELL));
    if (w === W && h === H) return false;
    W = w; H = h;
    canvas.width = grid.width = W; canvas.height = grid.height = H;
    field.width = ember.width = Math.ceil(W / FIELD); field.height = ember.height = Math.ceil(H / FIELD);
    out = ctx.createImageData(W, H);
    return true;
  }

  // one blob layer on c: n blobs per period, repeated across the width, sliding one period per loop (speed < 0 runs
  // left to right); big = blob width as a share of the field's height
  function layer(c, seed, n, period, speed, big, op, fills, now) {
    const fw = field.width, fh = field.height, shift = ((now * speed) % period + period) % period;
    for (let rep = -1; rep * period < fw + period; rep++) {
      for (let i = 0; i < n; i++) {
        const u = hash(seed * 31, i), v = hash(seed * 57, i + 99), q = hash(seed * 91, i + 7);
        const x = rep * period + (i + 0.1 + 0.8 * u) * period / n - shift;
        const y = fh * (1.05 - 1.1 * v);                    // spread over the whole height
        const rx = fh * big * (0.6 + 0.8 * q), ry = rx * (0.28 + 0.22 * hash(seed, i + 300));   // long, low wisps
        const k = fills[i % fills.length];
        const g = c.createRadialGradient(0, 0, 0, 0, 0, 1);
        g.addColorStop(0, `rgba(${k},${k},${k},1)`);
        g.addColorStop(0.5, `rgba(${k},${k},${k},0.55)`);
        g.addColorStop(1, `rgba(${k},${k},${k},0)`);
        c.save();
        c.translate(x, y); c.scale(rx, ry);
        c.globalAlpha = op; c.fillStyle = g; c.fillRect(-1, -1, 2, 2);
        c.restore();
      }
    }
  }

  function draw(now) {
    const fw = field.width, fh = field.height, lv = Math.round(LEVEL * 255);
    // the smoke, grey in every channel
    fctx.globalCompositeOperation = "source-over"; fctx.globalAlpha = 1;
    fctx.fillStyle = `rgb(${lv},${lv},${lv})`; fctx.fillRect(0, 0, fw, fh);
    layer(fctx, 1, 7, fw * 1.1, fw / 70, 0.55, 0.55, [255, 0, 255, 255, 0, 255, 0], now);
    layer(fctx, 2, 9, fw * 0.7, fw / 38, 0.32, 0.45, [0, 255, 0, 255, 255, 0, 255, 0, 255], now);
    layer(fctx, 3, 7, fw * 0.5, fw / 22, 0.18, 0.35, [255, 0, 255, 0, 255, 0, 255], now);
    // no fall-off: the dots are as dense at the top as at the bottom, and the smoke is the light and dark drifting
    // through them (the user's call, 2026-11-23)
    // the mint: its own slow patches, drifting against the smoke, in green only (magenta x patches, multiplied in),
    // so green = smoke x mint share, as Smoke Break does its terracotta
    ectx.globalCompositeOperation = "source-over"; ectx.globalAlpha = 1;
    ectx.fillStyle = "#f0f"; ectx.fillRect(0, 0, fw, fh);
    ectx.globalCompositeOperation = "lighter";
    ectx.fillStyle = "rgba(255,255,255,0.12)"; ectx.fillRect(0, 0, fw, fh);
    layer(ectx, 4, 3, fw * 1.3, -fw / 90, 0.5, 0.8, [255], now);
    fctx.globalCompositeOperation = "multiply"; fctx.drawImage(ember, 0, 0);

    gctx.imageSmoothingEnabled = true; gctx.imageSmoothingQuality = "high";
    gctx.drawImage(field, 0, 0, W, H);
    const s = gctx.getImageData(0, 0, W, H).data, o = out.data;
    for (let y = 0; y < H; y++) {
      const row = B8[y & 7];
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4, T = row[x & 7];
        if (s[i] / 255 < T) { o[i + 3] = 0; continue; }
        const c = s[i + 1] / 255 * MINT >= T ? EMBER : DOT;
        o[i] = c[0]; o[i + 1] = c[1]; o[i + 2] = c[2]; o[i + 3] = 255;
      }
    }
    ctx.putImageData(out, 0, 0);
  }

  const showing = () => host.clientWidth > 0 && host.clientHeight > 0;
  const live = () => showing() && !document.hidden && document.hasFocus() && !still.matches;

  function tick() {
    timer = 0;
    if (!showing()) return;
    const resized = size();
    if (live() || resized) draw(performance.now() / 1000 - t0);
    if (live()) timer = setTimeout(tick, 1000 / FPS);
  }
  const wake = () => { if (!timer) tick(); };

  for (const ev of ["focus", "resize", "hashchange"]) window.addEventListener(ev, wake);
  document.addEventListener("visibilitychange", wake);
  still.addEventListener("change", wake);
  new ResizeObserver(wake).observe(host);
  tick();
})();

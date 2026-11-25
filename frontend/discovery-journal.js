/* Discovery questionnaire: the sections as an open journal. A vanilla port of the
 * "journal spread carousel". The current section lies open in the middle (left page: what the
 * section is, right page: its questions); the others stack on either side and fold over as you
 * move. Drag, swipe, use the arrow keys, or click a stacked spread to turn to it.
 * On narrow screens each section is a single page instead of a double spread.
 *
 * Markup it expects inside `root`:  .jr-stage > .jr-view > .jr-scene > .jr-item[data-i] > .jr-page[data-side=l|r] > .jr-pg
 *
 * window.MMGAJournal.create(root, { onIndex }) -> { go(i), enter(i), exit(cb), index(), measure() }
 */
(function () {
  'use strict';

  const OPEN = 3;     // the open spread's slight V at the spine, degrees
  const SIDE = 80;    // how far a stacked page turns towards the middle
  const FOLD = 174;   // a page folding shut over its partner

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  const easeIn = (t) => t * t * t;
  const durationFor = (steps) => (steps <= 0 ? 0 : Math.round(620 * Math.sqrt(Math.min(steps, 12))));
  const settle = (pos, v, n) => clamp(Math.round(pos + clamp(v * 0.18, -1.5, 1.5)), 0, Math.max(0, n - 1));

  function sizeFor(sw, sh, wide) {
    if (sw <= 0 || sh <= 0) return null;
    if (sw < 760) {
      const w = Math.round(Math.min(sw * 0.86, 560));
      return { single: true, w, h: Math.round(Math.min(sh * 0.94, w * 1.55)) };
    }
    const ar = wide ? 1.72 : 1.36;
    const w = Math.round(Math.min(sw * (wide ? 0.8 : 0.62), sh * 0.96 * ar, wide ? 1400 : 1060));
    return { single: false, w, h: Math.round(w / ar) };
  }

  /** Where the spread `p` steps from the centre sits (double-page mode). */
  function poseSpread(p, w) {
    const d = Math.abs(p), a = d < 1 ? d : 1, s = p < 0 ? -1 : 1, out = d > 1 ? d - 1 : 0;
    const x = s * (a * (w * 0.5 + w * 0.03) + out * w * 0.055);
    const z = -(a * w * 0.42 + out * w * 0.06);
    const fold = a * a;
    const left = p > 0 ? OPEN + (FOLD - OPEN) * fold : OPEN + (SIDE - OPEN) * a;
    const right = p < 0 ? -(OPEN + (FOLD - OPEN) * fold) : -(OPEN + (SIDE - OPEN) * a);
    const shade = (deg) => clamp((Math.abs(deg) - OPEN) / 95, 0, 0.55);
    return { x, z, left, right, shadeLeft: shade(left), shadeRight: shade(right), opacity: clamp(7.2 - d, 0, 1), hidden: d > 7.2 };
  }

  /** Single-page mode: a gentle coverflow of pages. */
  function poseSingle(p, w) {
    const d = Math.abs(p), a = d < 1 ? d : 1, s = p < 0 ? -1 : 1, out = d > 1 ? d - 1 : 0;
    return {
      x: s * (a * w * 0.86 + out * w * 0.06), z: -(a * w * 0.55 + out * w * 0.08),
      right: -s * a * 48, shadeRight: clamp(a * 0.45 + out * 0.05, 0, 0.6),
      opacity: clamp(4.5 - d, 0, 1), hidden: d > 4.5,
    };
  }


  // ---- corner-peel page turn (ported from the BookCard fold: reflect the folded corner over the crease) ----
  function constrain(rx, ry, W, H) {
    let x = rx, y = Math.min(ry, H), d = Math.hypot(x, y - H);
    if (d > W) { x = (x / d) * W; y = H + ((y - H) / d) * W; }
    const diag = Math.hypot(W, H); d = Math.hypot(x, y);
    if (d > diag) { x = (x / d) * diag; y = (y / d) * diag; }
    return [x, y];
  }
  function clipHalf(poly, nx, ny, k, keepCorner) {
    const side = (p) => p[0] * nx + p[1] * ny - k, inside = (v) => (keepCorner ? v > 0 : v <= 0), out = [];
    poly.forEach((a, i) => {
      const b = poly[(i + 1) % poly.length], sa = side(a), sb = side(b);
      if (inside(sa)) out.push(a);
      if (inside(sa) !== inside(sb)) { const t = sa / (sa - sb); out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]); }
    });
    return out;
  }
  const HIDDEN = 'polygon(0px 0px, 0px 0px, 0px 0px)';
  const toPoly = (pts) => (pts.length < 3 ? HIDDEN : 'polygon(' + pts.map((p) => p[0].toFixed(2) + 'px ' + p[1].toFixed(2) + 'px').join(', ') + ')');
  function computeFold(rx, ry, W, H) {
    const [x, y] = constrain(rx, ry, W, H), vx = W - x, vy = H - y, dist = Math.hypot(vx, vy);
    if (dist < 0.5) return { cover: 'none', flap: HIDDEN, mat: 'none', under: 'none', fshade: 'none' };
    const nx = vx / dist, ny = vy / dist, k = ((W + x) / 2) * nx + ((H + y) / 2) * ny;
    const page = [[0, 0], [W, 0], [W, H], [0, H]];
    const cover = clipHalf(page, nx, ny, k, false);
    const flap = clipHalf(page, nx, ny, k, true).map((p) => { const t = p[0] * nx + p[1] * ny - k; return [p[0] - 2 * t * nx, p[1] - 2 * t * ny]; });
    const a = 2 * nx * nx - 1, b = 2 * nx * ny, c = -2 * nx * ny, d = 1 - 2 * ny * ny;
    const e = W * (1 - 2 * nx * nx) + 2 * k * nx, f = -2 * nx * ny * W + 2 * k * ny;
    const angle = (Math.atan2(nx, -ny) * 180) / Math.PI, depth = Math.min(1, dist / 80);
    const underStop = k - ((W / 2) * nx + (H / 2) * ny) + (W * Math.abs(nx) + H * Math.abs(ny)) / 2;
    const flapStop = -(k - (H / 2) * ny) + (2 * W * Math.abs(nx) + H * Math.abs(ny)) / 2;
    return {
      cover: toPoly(cover), flap: toPoly(flap), mat: 'matrix(' + [a, b, c, d, e, f].join(', ') + ')',
      under: 'linear-gradient(' + angle + 'deg, rgba(0,0,0,' + 0.3 * depth + ') ' + underStop + 'px, rgba(0,0,0,0) ' + (underStop + 36) + 'px)',
      fshade: 'linear-gradient(' + (angle + 180) + 'deg, rgba(0,0,0,' + 0.35 * depth + ') ' + flapStop + 'px, rgba(0,0,0,0) ' + (flapStop + 72) + 'px)',
    };
  }

  function create(root, opts) {
    opts = opts || {};
    const stage = root.querySelector('.jr-stage');
    const items = Array.from(root.querySelectorAll('.jr-item'));
    const pages = items.map((it) => [it.querySelector('.jr-page[data-side=l]'), it.querySelector('.jr-page[data-side=r]')]);
    const n = items.length;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let w = 0, single = false, pos = 0, target = 0, tween = null, raf = 0, drag = null, after = null, exitId = 0, flipping = false;

    function setCurrent(i) {
      items.forEach((it, k) => {
        const on = k === i;
        it.setAttribute('aria-hidden', String(!on));
        it.classList.toggle('on', on);
        it.querySelectorAll('.jr-pg').forEach((pg) => { pg.inert = !on; });
      });
      const live = root.querySelector('.jr-live');
      if (live && items[i]) live.textContent = items[i].getAttribute('aria-label') || '';
    }

    function paint() {
      if (!w) return;
      for (let i = 0; i < n; i++) {
        const el = items[i], p = i - pos;
        const q = single ? poseSingle(p, w) : poseSpread(p, w);
        if (q.hidden) { el.style.visibility = 'hidden'; continue; }
        el.style.visibility = 'visible';
        el.style.opacity = String(q.opacity);
        el.style.transform = `translate3d(${q.x.toFixed(2)}px,0,${q.z.toFixed(2)}px)`;
        const [l, r] = pages[i];
        if (l && !single) { l.style.transform = `rotateY(${q.left.toFixed(2)}deg)`; l.style.setProperty('--js', q.shadeLeft.toFixed(3)); }
        if (r) { r.style.transform = `rotateY(${q.right.toFixed(2)}deg)`; r.style.setProperty('--js', q.shadeRight.toFixed(3)); }
      }
    }

    function frame(t) {
      raf = 0;
      if (tween) {
        const k = tween.dur > 0 ? clamp((t - tween.t0) / tween.dur, 0, 1) : 1;
        pos = tween.from + (tween.to - tween.from) * tween.ease(k);
        if (k >= 1) {
          tween = null;
          if (after) { const cb = after; after = null; cb(); }
        }
      }
      paint();
      if (tween) raf = requestAnimationFrame(frame);
    }
    const kick = () => { if (!raf) raf = requestAnimationFrame(frame); };

    function tweenTo(to, dur, ease) {
      tween = { from: pos, to, t0: performance.now(), dur: reduce ? 0 : dur, ease: ease || easeInOut };
      kick();
    }

    function go(to, ease) {
      const next = clamp(Math.round(to), 0, Math.max(0, n - 1));
      after = null;
      exitId++; // cancels a pending exit
      if (next !== target) {
        target = next;
        setCurrent(next);
        if (opts.onIndex) opts.onIndex(next);
      }
      tweenTo(next, durationFor(Math.abs(next - pos)), ease);
    }

    function measure() {
      const s = sizeFor(stage.clientWidth, stage.clientHeight, opts.wide); // layout size, unaffected by the view's fade-in transform
      if (!s) return;
      w = s.w; single = s.single;
      root.classList.toggle('single', single);
      root.style.setProperty('--jw', s.w + 'px');
      root.style.setProperty('--jh', s.h + 'px');
      paint();
    }
    if ('ResizeObserver' in window) new ResizeObserver(measure).observe(stage);

    // ---- drag / swipe ----
    const NO_DRAG = 'input,textarea,select,button,a,label,.jr-toc,.jr-dots,.jr-qlist';
    stage.addEventListener('pointerdown', (e) => {
      if (opts.locked || e.button !== 0 || n < 1) return;
      const hitItem = e.target.closest('.jr-item');
      if (hitItem && hitItem.classList.contains('on') && e.target.closest(NO_DRAG)) return;
      drag = { id: e.pointerId, x: e.clientX, y: e.clientY, from: pos, lastX: e.clientX, lastT: e.timeStamp, v: 0, moved: false, peel: null, mode: '', onOpen: !!(hitItem && hitItem.classList.contains('on')) };
    });
    stage.addEventListener('pointermove', (e) => {
      const d = drag;
      if (!d || d.id !== e.pointerId || !w) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      // a mostly-vertical gesture is the page scrolling, never a swipe to the next section
      if (!d.moved && Math.abs(dy) > 8 && Math.abs(dy) > Math.abs(dx) * 0.6) { drag = null; return; }
      if (!d.moved && Math.abs(dx) > 8) {
        d.moved = true; tween = null; after = null;
        // on the open spread a sideways drag turns the page; at the end of a section it slides on to the next one
        const dir = dx < 0 ? 1 : -1, st = d.onOpen && !single && opts.turnStep ? opts.turnStep(dir) : null;
        const c = st ? startFlip(dir, st.mid, st.done) : null;
        if (c) { d.mode = 'peel'; d.peel = c; d.dir = dir; d.st = st; } else d.mode = 'slide';
        stage.setAttribute('data-drag', '');
        try { stage.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
      }
      if (!d.moved) return;
      const dt = Math.max(1, e.timeStamp - d.lastT);
      if (d.mode === 'peel') {
        const span = d.peel.W * 1.1;
        d.v = d.v * 0.6 + ((e.clientX - d.lastX) * -d.dir / span / (dt / 1000)) * 0.4;
        d.lastX = e.clientX; d.lastT = e.timeStamp;
        d.t = clamp((e.clientX - d.x) * -d.dir / span, 0, 1);
        d.peel.set(d.t);
        return;
      }
      const span = w * (single ? 0.8 : 0.42);
      d.v = d.v * 0.6 + (-(e.clientX - d.lastX) / span / (dt / 1000)) * 0.4;
      d.lastX = e.clientX; d.lastT = e.timeStamp;
      pos = clamp(d.from - (e.clientX - d.x) / span, -0.3, n - 0.7);
      kick();
    });
    const up = (e) => {
      const d = drag;
      if (!d || d.id !== e.pointerId) return;
      drag = null;
      stage.removeAttribute('data-drag');
      if (d.mode === 'peel') {
        const c = d.peel, go1 = (d.t || 0) > 0.3 || d.v > 1.1, t0 = performance.now(), from = d.t || 0, goal = go1 ? 1 : 0, dur = 360 * Math.abs(goal - from) + 80;
        const step = (now) => {
          if (!c.alive()) return;
          const k = clamp((now - t0) / dur, 0, 1), t = from + (goal - from) * (1 - Math.pow(1 - k, 3));
          c.set(t);
          if (k < 1) requestAnimationFrame(step); else if (go1) c.complete(); else c.cancel(d.st.revert);
        };
        requestAnimationFrame(step);
        return;
      }
      if (d.moved) { go(settle(pos, d.v, n), easeOut); return; }
    };
    stage.addEventListener('pointerup', up);
    stage.addEventListener('pointercancel', up);

    // ---- keys and sideways trackpad swipes ----
    root.addEventListener('keydown', (e) => {
      if (opts.locked || e.target !== root) return;
      const map = { ArrowRight: 1, ArrowDown: 1, PageDown: 1, ArrowLeft: -1, ArrowUp: -1, PageUp: -1 };
      if (e.key in map) go(target + map[e.key]);
      else if (e.key === 'Home') go(0);
      else if (e.key === 'End') go(n - 1);
      else return;
      e.preventDefault();
    });
    const wheel = { acc: 0, at: 0 };
    stage.addEventListener('wheel', (e) => {
      if (opts.locked || Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
      wheel.acc += e.deltaX;
      if (Math.abs(wheel.acc) < 40 || e.timeStamp - wheel.at < 340) return;
      wheel.at = e.timeStamp;
      go(target + (wheel.acc > 0 ? 1 : -1));
      wheel.acc = 0;
    }, { passive: true });

    setCurrent(0);

    // phones: a top-bound notepad. The sheet lifts from the bottom, swings up over its top edge and lands behind.
    function startNotepad(dir, mid, done) {
      const item = items[target], rp = pages[target] && pages[target][1];
      if (!item || !rp || !w || tween || flipping || reduce) return null;
      flipping = true;
      const base = rp.style.transform || '';
      const fresh = () => {
        const c = rp.cloneNode(true);
        c.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
        c.querySelectorAll('[for]').forEach((n) => n.removeAttribute('for'));
        const a = rp.querySelectorAll('textarea,input'), b = c.querySelectorAll('textarea,input');
        a.forEach((n, i) => { if (b[i]) { b[i].value = n.value; b[i].tabIndex = -1; } });
        c.classList.add('jr-fl'); c.setAttribute('aria-hidden', 'true'); c.inert = true;
        c.style.transformOrigin = '50% 0';
        return c;
      };
      item.classList.add('flipping');
      let under = null;
      if (dir < 0) { under = fresh(); under.style.transform = base + ' translateZ(1px)'; item.appendChild(under); }
      const front = dir > 0 ? fresh() : null;
      mid();
      const sheet = front || fresh();
      const back = rp.cloneNode(false);
      back.classList.add('jr-fl', 'jr-fl-back', 'jr-fl-sheetback');
      back.style.transformOrigin = '50% 0';
      item.appendChild(sheet); item.appendChild(back);
      let ended = false;
      const cleanup = () => {
        if (ended) return false;
        ended = true;
        [under, sheet, back].forEach((n) => n && n.remove());
        item.classList.remove('flipping'); flipping = false;
        return true;
      };
      const set = (t) => {
        if (ended) return;
        t = clamp(t, 0, 1);
        const ang = dir > 0 ? 180 * t : 180 * (1 - t);
        sheet.style.transform = base + ' translateZ(3px) rotateX(' + ang.toFixed(2) + 'deg)';
        back.style.transform = base + ' translateZ(3px) rotateX(' + (ang + 180).toFixed(2) + 'deg)';
        sheet.style.filter = 'brightness(' + (1 - 0.18 * Math.sin(Math.PI * Math.min(1, ang / 180))).toFixed(3) + ')';
      };
      set(0);
      return { W: w, set, complete() { if (cleanup() && done) done(); }, cancel(revert) { if (cleanup() && revert) revert(); }, alive: () => !ended };
    }

    function startFlip(dir, mid, done) {
        const item = items[target], rp = pages[target] && pages[target][1];
        if (!item || !rp || !w || tween || flipping || reduce) return null;
        flipping = true;
        const W = rp.offsetWidth, H = rp.offsetHeight, base = rp.style.transform || '';
        const fresh = () => {
          const c = rp.cloneNode(true);
          c.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
          c.querySelectorAll('[for]').forEach((n) => n.removeAttribute('for'));
          const a = rp.querySelectorAll('textarea,input'), b = c.querySelectorAll('textarea,input');
          a.forEach((n, i) => { if (b[i]) { b[i].value = n.value; b[i].tabIndex = -1; } });
          c.classList.add('jr-fl'); c.setAttribute('aria-hidden', 'true'); c.inert = true;
          return c;
        };
        const shell = (z) => { const c = rp.cloneNode(false); c.removeAttribute('data-side'); c.classList.add('jr-fl', 'jr-fl-shell'); c.style.transform = base + ' translateZ(' + z + 'px)'; return c; };
        item.classList.add('flipping');
        const lp = single ? null : pages[target][0], lbase = lp ? (lp.style.transform || '') : '';
        const leftClone = () => {
          const c = lp.cloneNode(true);
          c.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
          c.classList.add('jr-fl'); c.setAttribute('aria-hidden', 'true'); c.inert = true;
          return c;
        };
        let leftOld = null, flapPg = null;
        if (lp) {
          const lc = leftClone();
          if (dir > 0) { leftOld = lc; leftOld.style.transform = lbase + ' translateZ(0.5px)'; item.appendChild(leftOld); }
          else flapPg = lc; // the old left page is what unfolds back
        }
        const old = fresh(); old.style.transform = base + ' translateZ(1px)';
        let under = null;
        if (dir < 0) { under = old; item.appendChild(under); }
        mid();
        if (lp && dir > 0) flapPg = leftClone(); // the new left page lands on the spine
        const cover = dir < 0 ? fresh() : old;
        cover.style.transform = base + ' translateZ(2px)';
        const shade = shell(3), flapW = shell(18), flapIn = document.createElement('div'), fsh = document.createElement('div');
        flapIn.className = 'jr-fl-back';
        flapIn.style.cssText = 'position:absolute;left:0;top:0;width:' + W + 'px;height:' + H + 'px;transform-origin:0 0';
        fsh.style.cssText = 'position:absolute;top:0;height:100%;left:' + (-W) + 'px;width:' + 2 * W + 'px;pointer-events:none';
        if (flapPg) {
          flapIn.style.overflow = 'hidden'; flapIn.style.background = '#f2ecdf';
          Array.from(flapPg.children).forEach((c) => { if (!c.classList.contains('jr-shade')) { c.style.removeProperty('transform'); flapIn.appendChild(c); } });
        }
        flapW.appendChild(flapIn); flapW.appendChild(fsh);
        item.appendChild(cover); item.appendChild(shade); item.appendChild(flapW);
        const from = dir > 0 ? W : -W, to = dir > 0 ? -W : W;
        let ended = false;
        const cleanup = () => {
          if (ended) return false;
          ended = true;
          [under, leftOld, cover, shade, flapW].forEach((n) => n && n.remove());
          item.classList.remove('flipping'); flipping = false;
          return true;
        };
        // the turning leaf must follow the book's V: right-page plane at the start, left-page plane once it has crossed the spine
        const rot = (el) => { const m = el && /rotateY\((-?[\d.]+)deg\)/.exec(el.style.transform || ''); return m ? parseFloat(m[1]) : 0; };
        const angR = rot(rp), angL = lp ? rot(lp) : angR;
        const set = (t) => {
          if (ended) return;
          t = clamp(t, 0, 1);
          const left = dir > 0 ? t : 1 - t;
          flapW.style.transform = 'rotateY(' + (angR + (angL - angR) * left).toFixed(3) + 'deg) translateZ(18px)';
          const x = from + (to - from) * t, y = t >= 1 ? H : H * (1 - 0.3 * Math.sin(Math.PI * t));
          const f = computeFold(x, y, W, H);
          cover.style.clipPath = f.cover; flapW.style.clipPath = f.flap; flapIn.style.transform = f.mat;
          shade.style.backgroundImage = f.under; fsh.style.backgroundImage = f.fshade;
        };
        set(0);
        return { W, set, complete() { if (cleanup() && done) done(); }, cancel(revert) { if (cleanup() && revert) revert(); }, alive: () => !ended };
    }

    return {
      go,
      index: () => target,
      measure,
      /** Turn the open spread's right page like a sheet of paper. dir 1 = forward. `mid` swaps the content, `done` runs at the end. */
      flip(dir, mid, done) {
        const c = single ? startNotepad(dir, mid, done) : startFlip(dir, mid, done);
        if (!c) { mid(); if (done) done(); return; }
        const t0 = performance.now(), dur = 720;
        const e2 = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
        const step = (now) => {
          if (!c.alive()) return;
          const t = clamp((now - t0) / dur, 0, 1);
          c.set(e2(t));
          if (t < 1) requestAnimationFrame(step); else c.complete();
        };
        step(t0);
        setTimeout(() => c.complete(), dur + 500); // frames are throttled in a background tab
      },
      /** Riffle in from the right-hand stack and open on spread i. */
      enter(i) {
        measure();
        const next = clamp(i, 0, n - 1);
        target = next; setCurrent(next);
        pos = reduce ? next : next - 4.5;
        paint();
        tweenTo(next, 1250, easeInOut);
      },
      /** Close the journal: the open spread turns away to the right, then `cb` runs. */
      exit(cb) {
        if (reduce) { if (cb) cb(); return; }
        let ran = false;
        const id = ++exitId;
        const done = () => { if (ran || id !== exitId) return; ran = true; if (cb) cb(); };
        after = done;
        tweenTo(-1.4, 560, easeIn);
        setTimeout(done, 800); // in case frames are throttled (background tab)
      },
    };
  }

  window.MMGAJournal = { create };
})();

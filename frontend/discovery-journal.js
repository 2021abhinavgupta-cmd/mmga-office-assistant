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

  function sizeFor(sw, sh) {
    if (sw <= 0 || sh <= 0) return null;
    if (sw < 760) {
      const w = Math.round(Math.min(sw * 0.86, 560));
      return { single: true, w, h: Math.round(Math.min(sh * 0.94, w * 1.55)) };
    }
    const ar = 1.36;
    const w = Math.round(Math.min(sw * 0.62, sh * 0.9 * ar, 1060));
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

  function create(root, opts) {
    opts = opts || {};
    const stage = root.querySelector('.jr-stage');
    const items = Array.from(root.querySelectorAll('.jr-item'));
    const pages = items.map((it) => [it.querySelector('.jr-page[data-side=l]'), it.querySelector('.jr-page[data-side=r]')]);
    const n = items.length;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let w = 0, single = false, pos = 0, target = 0, tween = null, raf = 0, drag = null, after = null, exitId = 0;

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
      const s = sizeFor(stage.clientWidth, stage.clientHeight); // layout size, unaffected by the view's fade-in transform
      if (!s) return;
      w = s.w; single = s.single;
      root.classList.toggle('single', single);
      root.style.setProperty('--jw', s.w + 'px');
      root.style.setProperty('--jh', s.h + 'px');
      paint();
    }
    if ('ResizeObserver' in window) new ResizeObserver(measure).observe(stage);

    // ---- drag / swipe ----
    const NO_DRAG = 'input,textarea,select,button,a,label,.jr-qs,.jr-toc';
    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || n < 1) return;
      const hitItem = e.target.closest('.jr-item');
      if (hitItem && hitItem.classList.contains('on') && e.target.closest(NO_DRAG)) return;
      drag = { id: e.pointerId, x: e.clientX, from: pos, lastX: e.clientX, lastT: e.timeStamp, v: 0, moved: false };
    });
    stage.addEventListener('pointermove', (e) => {
      const d = drag;
      if (!d || d.id !== e.pointerId || !w) return;
      const dx = e.clientX - d.x;
      if (!d.moved && Math.abs(dx) > 6) {
        d.moved = true; tween = null; after = null;
        stage.setAttribute('data-drag', '');
        try { stage.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
      }
      if (!d.moved) return;
      const span = w * (single ? 0.8 : 0.42);
      const dt = Math.max(1, e.timeStamp - d.lastT);
      d.v = d.v * 0.6 + (-(e.clientX - d.lastX) / span / (dt / 1000)) * 0.4;
      d.lastX = e.clientX; d.lastT = e.timeStamp;
      pos = clamp(d.from - dx / span, -0.3, n - 0.7);
      kick();
    });
    const up = (e) => {
      const d = drag;
      if (!d || d.id !== e.pointerId) return;
      drag = null;
      stage.removeAttribute('data-drag');
      if (d.moved) { go(settle(pos, d.v, n), easeOut); return; }
      const hit = e.target.closest('.jr-item');
      if (hit && !hit.classList.contains('on')) go(Number(hit.dataset.i));
    };
    stage.addEventListener('pointerup', up);
    stage.addEventListener('pointercancel', up);

    // ---- keys and sideways trackpad swipes ----
    root.addEventListener('keydown', (e) => {
      if (e.target !== root) return;
      const map = { ArrowRight: 1, ArrowDown: 1, PageDown: 1, ArrowLeft: -1, ArrowUp: -1, PageUp: -1 };
      if (e.key in map) go(target + map[e.key]);
      else if (e.key === 'Home') go(0);
      else if (e.key === 'End') go(n - 1);
      else return;
      e.preventDefault();
    });
    const wheel = { acc: 0, at: 0 };
    stage.addEventListener('wheel', (e) => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
      wheel.acc += e.deltaX;
      if (Math.abs(wheel.acc) < 40 || e.timeStamp - wheel.at < 340) return;
      wheel.at = e.timeStamp;
      go(target + (wheel.acc > 0 ? 1 : -1));
      wheel.acc = 0;
    }, { passive: true });

    setCurrent(0);

    return {
      go,
      index: () => target,
      measure,
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

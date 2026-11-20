/* Discovery questionnaire: the isometric stack that sits beside "Your details".
 * A vanilla port of the "syntra" exploded core. Five ceramic plates, one per part of the
 * questionnaire (bottom = your details, then the sections in four groups). Hover (or focus the
 * details form, or tap) and the stack opens: the plates lift apart and a card beside each plate
 * says what that part asks. Everything is one SVG artboard of 1000 x 1000 units.
 *
 * window.MMGAStack.create(host, { groups, onPick }) -> { setDetails({company, email, valid}), hold(bool), open(bool) }
 *   groups: 5 items, bottom to top: { title, items: [..], count, main? }
 */
(function () {
  'use strict';

  // ---------- geometry (shared isometric frame) ----------
  const ISO_X = 0.866, ISO_Y = 0.5, CX = 500, CY = 500;
  const RING_FLATTEN = 0.57, RING_RX = [400, 530];
  const BASE = { half: 168, radius: 40, top: 475, depth: 34 };
  const SEAM_GAP = 9;
  const MID = { half: 151, radius: 36, top: BASE.top - SEAM_GAP - 28, depth: 28 };
  const CAP = { half: 101, radius: 28, top: MID.top - 14, depth: 14 };

  const HEAT = ['#8593ea', '#a98ae6', '#e88bb3', '#f5a07a', '#f6d08a'];
  const ACC = '#ec8470';

  const round = (n) => Math.round(n * 100) / 100;
  const f2 = (n) => n.toFixed(2);
  const project = (x, y, cx = CX, cy = CY) => [cx + (x - y) * ISO_X, cy + (x + y) * ISO_Y];

  function footprint(half, radius, steps = 14) {
    const r = Math.min(radius, half), c = half - r, pts = [];
    for (const [ox, oy, start] of [[c, c, 0], [-c, c, 90], [-c, -c, 180], [c, -c, 270]]) {
      for (let i = 0; i <= steps; i++) {
        const a = ((start + (90 * i) / steps) * Math.PI) / 180;
        pts.push([ox + r * Math.cos(a), oy + r * Math.sin(a)]);
      }
    }
    return pts;
  }
  const toPath = (pts) => 'M' + pts.map(([x, y]) => f2(x) + ' ' + f2(y)).join('L') + 'Z';
  const topPath = (slab, inset = 0, cx = CX, dy = 0) => {
    const half = slab.half - inset, radius = Math.max(slab.radius - inset, 1);
    return toPath(footprint(half, radius).map(([x, y]) => project(x, y, cx, slab.top + dy)));
  };
  function hull(points) {
    const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const build = (list) => {
      const out = [];
      for (const p of list) {
        while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
        out.push(p);
      }
      out.pop();
      return out;
    };
    return build(pts).concat(build(pts.slice().reverse()));
  }
  function bodyPath(slab, inset = 0) {
    const half = slab.half - inset, radius = Math.max(slab.radius - inset, 1);
    const outline = footprint(half, radius);
    const upper = outline.map(([x, y]) => project(x, y, CX, slab.top));
    const lower = outline.map(([x, y]) => project(x, y, CX, slab.top + slab.depth));
    return toPath(hull(upper.concat(lower)));
  }
  function facePoint(slab, side, u, v) {
    const { half, top, depth } = slab;
    const [x, y] = side === 'left' ? project(-half + 2 * half * u, half, CX, top) : project(half, half - 2 * half * u, CX, top);
    return [x, y + depth * v];
  }
  const FM = { left: `${ISO_X} ${ISO_Y} 0 1`, right: `${ISO_X} ${-ISO_Y} 0 1`, top: `${ISO_X} ${ISO_Y} ${-ISO_X} ${ISO_Y}` };
  const ringPoint = (rx, deg) => {
    const a = (deg * Math.PI) / 180;
    return [round(CX + rx * Math.cos(a)), round(CY + rx * RING_FLATTEN * Math.sin(a))];
  };
  const ringPath = (rx) => {
    const ry = rx * RING_FLATTEN;
    return `M${CX + rx} ${CY}A${rx} ${ry} 0 1 1 ${CX - rx} ${CY}A${rx} ${ry} 0 1 1 ${CX + rx} ${CY}Z`;
  };

  // ---------- the mark: four leaves turned a quarter each ----------
  const LEAF = 'M11 0H19A3 3 0 0 1 22 3V11A11 11 0 0 1 11 22H3A3 3 0 0 1 0 19V11A11 11 0 0 1 11 0Z';
  const leaves = (fill, stroke) => [0, 90, 180, 270].map((t) =>
    `<path d="${LEAF}" transform="rotate(${t}) translate(-25 -25)" fill="${fill}"${stroke ? ` stroke="${stroke}" stroke-width=".6"` : ''}/>`).join('');

  // ---------- parts ----------
  const DEFS = `<defs>
    <linearGradient id="st-side" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#f6f6f8"/><stop offset=".46" stop-color="#ececf0"/><stop offset=".5" stop-color="#dddde3"/><stop offset="1" stop-color="#e7e7ec"/></linearGradient>
    <linearGradient id="st-side-shade" x1="0" y1="0" x2="0" y2="1"><stop offset=".45" stop-color="#6c6a7c" stop-opacity="0"/><stop offset="1" stop-color="#6c6a7c" stop-opacity=".16"/></linearGradient>
    <linearGradient id="st-bevel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#e5e5ea"/></linearGradient>
    <linearGradient id="st-top" x1=".1" y1="0" x2=".9" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#f2f2f5"/></linearGradient>
    <linearGradient id="st-inlay" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ececf0"/><stop offset="1" stop-color="#fff"/></linearGradient>
    <linearGradient id="st-seam" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="${HEAT[1]}"/><stop offset=".5" stop-color="${HEAT[2]}"/><stop offset="1" stop-color="${HEAT[3]}"/></linearGradient>
    <linearGradient id="st-slot" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5b5a66"/><stop offset="1" stop-color="#8f8e99"/></linearGradient>
    <linearGradient id="st-logo-top" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${HEAT[1]}"/><stop offset=".55" stop-color="${HEAT[2]}"/><stop offset="1" stop-color="${HEAT[3]}"/></linearGradient>
    <clipPath id="st-mark-clip">${leaves('#000')}</clipPath>
    <radialGradient id="st-hole" cx=".5" cy=".4" r=".7"><stop offset="0" stop-color="#d8d8de"/><stop offset="1" stop-color="#f3f3f6"/></radialGradient>
    <filter id="st-blur-sm" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="3.5"/></filter>
    <filter id="st-blur-glow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="9"/></filter>
    <filter id="st-blur-spill" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="18"/></filter>
  </defs>`;

  function faceMark(slab, side, u, v, w, h, fill, glow) {
    const [x, y] = facePoint(slab, side, u, v);
    return `<g transform="translate(${f2(x)} ${f2(y)}) matrix(${FM[side]} 0 0)">` +
      (glow ? `<rect x="${-w / 2 - 2}" y="${-h / 2 - 3}" width="${w + 4}" height="${h + 6}" rx="${h}" fill="${fill}" filter="url(#st-blur-sm)" opacity=".75"/>` : '') +
      `<rect x="${-w / 2}" y="${-h / 2}" width="${w}" height="${h}" rx="${h / 2}" fill="${fill}"/></g>`;
  }
  function panelLine(slab, side, v, from = 0.14, to = 0.86) {
    const [x1, y1] = facePoint(slab, side, from, v), [x2, y2] = facePoint(slab, side, to, v);
    return `<line x1="${f2(x1)}" y1="${f2(y1)}" x2="${f2(x2)}" y2="${f2(y2)}" stroke="#a6a5b0" stroke-width=".9" opacity=".55"/>`;
  }
  function fastener(slab, x, y) {
    const [px, py] = project(x, y, CX, slab.top);
    return `<g transform="translate(${f2(px)} ${f2(py)}) matrix(${FM.top} 0 0)"><circle r="6.5" fill="url(#st-hole)" stroke="#c9c9d1"/></g>`;
  }
  const slabBody = (s) => `<g><path d="${bodyPath(s)}" fill="url(#st-side)"/><path d="${bodyPath(s)}" fill="url(#st-side-shade)"/><path d="${bodyPath(s)}" fill="none" stroke="#cfcfd7" opacity=".85"/></g>`;
  const slabTop = (s, groove) => `<g><path d="${topPath(s)}" fill="url(#st-bevel)" stroke="#d9d9e0"/><path d="${topPath(s, 4.5)}" fill="url(#st-top)" stroke="#fff"/>` +
    (groove !== undefined ? `<path d="${topPath(s, groove)}" fill="none" stroke="#dadae1" stroke-width="1.2"/>` : '') + `</g>`;
  const inlay = (s) => `<path d="${topPath(s, 19)}" fill="url(#st-inlay)" stroke="#dcdce3" stroke-width="1.2"/><path d="${topPath(s, 20.5)}" fill="none" stroke="#fff" opacity=".9"/>`;

  const LOGO_SIDE = ['#6f55b8', '#7f5cbf', '#9564c2', '#ab6cbd', '#c174b2', '#d27ba9'];
  function coreLogo(x, y) {
    const plane = (lift) => `translate(${x} ${y - lift}) matrix(${FM.top} 0 0) scale(1.42)`;
    return `<g><g transform="${plane(0)} translate(2.4 2.4)" opacity=".22" filter="url(#st-blur-sm)">${leaves('#5e5870')}</g>` +
      LOGO_SIDE.map((c, k) => `<g transform="${plane(k)}">${leaves(c)}</g>`).join('') +
      `<g transform="${plane(LOGO_SIDE.length)}"><rect x="-27" y="-27" width="54" height="54" fill="url(#st-logo-top)" clip-path="url(#st-mark-clip)"/>${leaves('none', '#ffe1ee')}</g></g>`;
  }
  function plateNumber(slab, n, main) {
    const k = slab.half * 0.56, [x, y] = project(k, k, CX, slab.top);
    const plane = `matrix(${FM.top} 0 0) rotate(-45)`, label = String(n).padStart(2, '0');
    const t = `text-anchor="middle" dominant-baseline="central" font-size="25" font-weight="500" letter-spacing="2.5" font-family="Martian Mono, ui-monospace, monospace"`;
    return `<g transform="translate(${f2(x)} ${f2(y)})"><text ${t} transform="translate(0 .9) ${plane}" fill="#fff" opacity=".95">${label}</text>` +
      `<text ${t} transform="${plane}" fill="${main ? ACC : '#c6c5cf'}">${label}</text></g>`;
  }

  // ---------- the exploded layout ----------
  const EXPLODE = { stiffness: 105, damping: 25, mass: 1, gap: 94, lift: 12, topPlateY: 230, stagger: 0.05, scales: [0.63, 0.62, 0.7, 1.07, 1.06] };
  const PLATES = [
    Object.assign({}, BASE, { top: BASE.top + 17, depth: 17 }),
    Object.assign({}, BASE, { depth: 17 }),
    MID,
    Object.assign({}, CAP, { top: CAP.top + 7, depth: 7 }),
    Object.assign({}, CAP, { depth: 7 }),
  ];
  const ACTIVE = 0; // the details plate: data enters at the bottom of the stack
  const SKIRT = [0, 4, 0, 10, 10];
  const TARGET = PLATES.map((_, i) => EXPLODE.topPlateY + (PLATES.length - 1 - i) * EXPLODE.gap - (i === ACTIVE ? EXPLODE.lift : 0));
  const TRAVEL = PLATES.map((s, i) => TARGET[i] - s.top);
  const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
  const plateProgress = (i, p) => { const d = Math.abs(i - ACTIVE) * EXPLODE.stagger; return clamp((p - d) / (1 - d)); };
  const tipOffset = (i) => { const { half, radius } = PLATES[i]; return 0.866 * (2 * half - 0.586 * radius) * EXPLODE.scales[i]; };
  const tipX = (i, side) => CX + (side === 'right' ? 1 : -1) * tipOffset(i);
  const cornerY = (i) => TARGET[i] + PLATES[i].depth / 2;

  const CARDS = { left: { edge: -2, width: 31 }, right: { edge: 0, width: 29 }, restInset: 10, outward: 2, beadSeconds: 1.6 };
  const SIDES = ['right', 'left', 'right', 'left', 'right'];

  function coreSvg() {
    const seam = { half: MID.half - 2, radius: MID.radius - 2, top: MID.top + MID.depth - 1, depth: SEAM_GAP + 1 };
    const plate = (i, inner) => {
      const s = PLATES[i];
      return `<g data-plate="${i}">` +
        (i > 0 ? `<path data-shadow d="${topPath(s, -6, CX, s.depth + 34)}" fill="#5e5870" filter="url(#st-blur-spill)" opacity="0"/>` : '') +
        (SKIRT[i] ? `<g data-skirt opacity="0">${slabBody(Object.assign({}, s, { top: s.top + s.depth - 1, depth: SKIRT[i] + 1 }))}</g>` : '') +
        inner + `</g>`;
    };
    const emph = (s) => `<g data-emph transform="translate(${CX} ${s.top}) matrix(${FM.top} 0 0)" opacity="0">
      <circle r="104" fill="${ACC}" filter="url(#st-blur-spill)" opacity=".35"/>
      <circle r="66" fill="none" stroke="${ACC}" stroke-width="1.4" opacity=".9"/>
      <circle r="46" fill="none" stroke="${HEAT[1]}" stroke-dasharray="3 6" opacity=".7"/>
      <circle r="10" fill="${ACC}"/></g>`;
    const slots = (slab, marks) => marks.map((m) => faceMark(slab, m[0], m[1], m[2], m[3], m[4], 'url(#st-slot)')).join('');
    return `<svg class="st-svg" viewBox="0 0 1000 1000" aria-hidden="true">${DEFS}` +
      plate(0, slabBody(PLATES[0]) + slabTop(PLATES[0], 10) + plateNumber(PLATES[0], 1, true) +
        slots(BASE, [['right', .5, .62, 4.5, 4.5], ['right', .58, .62, 4.5, 4.5], ['right', .66, .62, 4.5, 4.5], ['left', .32, .62, 4.5, 4.5], ['left', .4, .62, 4.5, 4.5]]) + emph(PLATES[0])) +
      plate(1, slabBody(PLATES[1]) + slabTop(PLATES[1], 10) + plateNumber(PLATES[1], 2) + panelLine(BASE, 'left', .3) + panelLine(BASE, 'right', .3)) +
      plate(2, `<g class="st-pulse"><path d="${topPath(MID, 4, CX, BASE.top - MID.top)}" fill="${HEAT[2]}" opacity=".24" filter="url(#st-blur-spill)"/>` +
        `<path d="${bodyPath(seam)}" fill="${HEAT[2]}" opacity=".6" filter="url(#st-blur-glow)"/></g>` +
        `<path data-glow d="${bodyPath(seam)}" fill="${ACC}" filter="url(#st-blur-glow)" opacity="0"/>` +
        `<path d="${bodyPath(seam)}" fill="url(#st-seam)"/>` + slabBody(MID) + slabTop(MID, 11) + plateNumber(MID, 3) +
        panelLine(MID, 'left', .78) + panelLine(MID, 'right', .78) +
        slots(MID, [['left', .62, .42, 34, 7.5], ['left', .34, .42, 5, 5], ['left', .42, .42, 5, 5], ['right', .44, .42, 5, 5], ['right', .52, .42, 5, 5], ['right', .68, .42, 22, 4.5]]) +
        `<g class="st-pulse">${faceMark(MID, 'left', .2, .42, 7, 2.6, ACC, true)}</g>` +
        fastener(MID, 0, -MID.half + 28) + fastener(MID, -MID.half + 28, 0) + fastener(MID, MID.half - 28, 0) + fastener(MID, 0, MID.half - 28)) +
      plate(3, slabBody(PLATES[3]) + slabTop(PLATES[3]) + inlay(PLATES[3]) + plateNumber(PLATES[3], 4)) +
      plate(4, slabBody(PLATES[4]) + slabTop(PLATES[4]) + inlay(CAP) + coreLogo(CX, CAP.top)) +
      `</svg>`;
  }

  function shadowSvg() {
    const floor = Object.assign({}, BASE, { top: BASE.top + BASE.depth + 30 });
    return `<svg viewBox="0 0 1000 1000" aria-hidden="true"><defs>
      <filter id="st-blur-lg" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="26"/></filter>
      <filter id="st-blur-md" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="9"/></filter></defs>
      <path d="${topPath(floor, -14)}" fill="#7a7690" opacity=".2" filter="url(#st-blur-lg)"/>
      <path d="${topPath(floor, 6)}" fill="#55526a" opacity=".2" filter="url(#st-blur-md)"/></svg>`;
  }

  function backdropSvg() {
    const nodes = [[118, 132], [318, 82], [690, 96], [884, 188], [936, 470], [70, 512], [150, 858], [850, 846]];
    const dots = Array.from({ length: 26 }, (_, i) => {
      const a = (i * 137.5 * Math.PI) / 180, r = 160 + ((i * 53) % 340);
      return [round(CX + r * Math.cos(a) * 1.02), round(CY + r * Math.sin(a) * RING_FLATTEN * 1.6), 0.8 + (i % 3) * 0.5];
    });
    return `<svg viewBox="0 0 1000 1000" aria-hidden="true"><defs>
      <linearGradient id="st-glass" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".7"/><stop offset="1" stop-color="#fff" stop-opacity=".12"/></linearGradient>
      <linearGradient id="st-axis-v" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c9c8d2" stop-opacity="0"/><stop offset=".5" stop-color="#c9c8d2" stop-opacity=".45"/><stop offset="1" stop-color="#c9c8d2" stop-opacity="0"/></linearGradient>
      <linearGradient id="st-axis-h" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#c9c8d2" stop-opacity="0"/><stop offset=".5" stop-color="#c9c8d2" stop-opacity=".7"/><stop offset="1" stop-color="#c9c8d2" stop-opacity="0"/></linearGradient></defs>
      <g fill="url(#st-glass)" stroke="#e1e1e8" opacity=".5"><rect width="290" height="250" rx="22" transform="matrix(.866 -.5 0 1 232 300)"/><rect width="290" height="250" rx="22" transform="matrix(.866 .5 0 1 520 150)"/></g>
      <g fill="#fff" stroke="#cfcfd8">${nodes.map(([x, y], i) => `<circle cx="${x}" cy="${y}" r="${i % 3 === 0 ? 4 : 3}" opacity=".85"/>`).join('')}</g>
      <rect x="${CX - 0.5}" y="110" width="1" height="780" fill="url(#st-axis-v)"/><rect x="70" y="${CY - 0.5}" width="860" height="1" fill="url(#st-axis-h)"/>
      <g fill="#c9c8d2">${dots.map(([x, y, r], i) => `<circle cx="${x}" cy="${y}" r="${r}" opacity="${(0.35 + (i % 4) * 0.12).toFixed(2)}"/>`).join('')}</g>
      <g stroke="#c9c8d2" opacity=".5">${[[212, 292], [790, 690], [860, 430], [150, 560]].map(([x, y]) => `<path d="M${x - 5} ${y}h10M${x} ${y - 5}v10"/>`).join('')}</g></svg>`;
  }

  const BEADS = [[0, 20, 7], [0, 110, 5.5], [0, 200, 8], [0, 290, 6], [1, 60, 6], [1, 132, 8], [1, 204, 5.5], [1, 276, 7], [1, 348, 6.5]];
  const ORBIT_S = [70, 95];
  function crystal(size) {
    return `<g filter="url(#st-crystal-shadow)"><circle r="${size * 1.55}" fill="url(#st-crystal-halo)"/><circle r="${size}" fill="url(#st-crystal-body)"/>` +
      `<circle r="${size}" fill="url(#st-crystal-refract)"/><circle r="${size - 0.5}" fill="none" stroke="url(#st-crystal-rim)"/>` +
      `<ellipse cx="${-size * 0.32}" cy="${-size * 0.4}" rx="${size * 0.36}" ry="${size * 0.2}" transform="rotate(-30 ${-size * 0.32} ${-size * 0.4})" fill="#fff" opacity=".95"/>` +
      `<circle cx="${size * 0.34}" cy="${-size * 0.5}" r="${Math.max(0.7, size * 0.07)}" fill="#fff" opacity=".85"/></g>`;
  }
  function beadsSvg(half, animate) {
    const id = 'st-clip-' + half;
    const defs = half === 'back' ? `
      <linearGradient id="st-crystal-body" x1=".15" y1=".1" x2=".85" y2=".95"><stop offset="0" stop-color="#fdeaf3" stop-opacity=".92"/><stop offset=".5" stop-color="${HEAT[2]}" stop-opacity=".9"/><stop offset="1" stop-color="#c662a8" stop-opacity=".95"/></linearGradient>
      <radialGradient id="st-crystal-refract" cx=".6" cy=".78" r=".45"><stop offset="0" stop-color="#fff4f8" stop-opacity=".85"/><stop offset="1" stop-color="#fff4f8" stop-opacity="0"/></radialGradient>
      <linearGradient id="st-crystal-rim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".95"/><stop offset="1" stop-color="#9c3f86" stop-opacity=".55"/></linearGradient>
      <radialGradient id="st-crystal-halo" cx=".5" cy=".5" r=".5"><stop offset=".5" stop-color="${HEAT[2]}" stop-opacity=".14"/><stop offset="1" stop-color="${HEAT[2]}" stop-opacity="0"/></radialGradient>
      <filter id="st-crystal-shadow" x="-80%" y="-80%" width="260%" height="260%"><feDropShadow dx="0" dy="2.5" stdDeviation="2.5" flood-color="#7a3d72" flood-opacity=".25"/></filter>` : '';
    const clip = half === 'back' ? `<rect x="-200" y="-200" width="1400" height="${CY + 200}"/>` : `<rect x="-200" y="${CY}" width="1400" height="700"/>`;
    const nodes = BEADS.map(([ring, angle, size]) => {
      const rx = RING_RX[ring], [x, y] = ringPoint(rx, angle), dur = ORBIT_S[ring], rev = ring === 1;
      const start = angle / 360, begin = -dur * (rev ? 1 - start : start);
      return animate
        ? `<g>${crystal(size)}<animateMotion dur="${dur}s" begin="${begin}s" repeatCount="indefinite" path="${ringPath(rx)}"${rev ? ' keyPoints="1;0" keyTimes="0;1" calcMode="linear"' : ''}/></g>`
        : `<g transform="translate(${x} ${y})">${crystal(size)}</g>`;
    }).join('');
    return `<svg viewBox="0 0 1000 1000" aria-hidden="true"><defs>${defs}<clipPath id="${id}">${clip}</clipPath></defs><g clip-path="url(#${id})">${nodes}</g></svg>`;
  }
  const orbitSvg = () => `<svg viewBox="0 0 1000 1000" aria-hidden="true"><g fill="none" stroke="#dcdbe3">` +
    `<ellipse cx="${CX}" cy="${CY}" rx="${RING_RX[0]}" ry="${RING_RX[0] * RING_FLATTEN}" opacity=".8"/>` +
    `<ellipse cx="${CX}" cy="${CY}" rx="${RING_RX[1]}" ry="${RING_RX[1] * RING_FLATTEN}" opacity=".65"/></g></svg>`;

  // the isometric floor grid under the stack (same 30 degree axes as the core)
  const floorSvg = () => `<svg aria-hidden="true" width="100%" height="100%"><defs><pattern id="st-floor-cell" width="84" height="48" patternUnits="userSpaceOnUse">` +
    `<path d="M0 24L42 0L84 24M0 24L42 48L84 24" fill="none" stroke="#d6d5de" stroke-width=".8"/></pattern></defs><rect width="100%" height="100%" fill="url(#st-floor-cell)"/></svg>`;

  // ---------- small icons for the cards ----------
  const ICON = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICONS = [
    ICON('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
    ICON('<path d="M12 20h9"/><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
    ICON('<path d="M12 3l2.5 5.5L20 11l-5.5 2.5L12 19l-2.5-5.5L4 11l5.5-2.5Z"/>'),
    ICON('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M7 10v4a3 3 0 0 0 3 3h4"/>'),
    ICON('<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>'),
  ];

  const escape = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[m]);

  function cardBody(g, i) {
    const n = String(i + 1).padStart(2, '0');
    const items = (g.items || []).slice(0, 4).map(escape).join(' · ') + (g.items && g.items.length > 4 ? ` · +${g.items.length - 4}` : '');
    return `<div class="st-cb${g.main ? ' main' : ''}">
      <span class="st-n">${n}</span>
      <div class="st-hd"><p class="st-tl">${escape(g.title)}</p>${g.main ? '<span class="st-tag">You are here</span>' : ''}</div>
      <span class="st-ic">${ICONS[i] || ''}</span>
      <div class="st-bd"><p class="st-it">${items}</p>` +
      (g.main ? `<p class="st-live"><span data-live="company" class="ph">Company name</span><span data-live="email" class="ph">Email address</span></p>` : '') +
      `</div>` + (g.main ? '' : `<span class="st-ct"><b>${g.count}</b>${g.count === 1 ? 'Q' : 'Qs'}</span>`) + `</div>`;
  }

  // ---------- springs ----------
  function spring(k, c, m) {
    return { x: 0, v: 0, to: 0, k, c, m, step(dt) { const a = (-this.k * (this.x - this.to) - this.c * this.v) / this.m; this.v += a * dt; this.x += this.v * dt; },
      done() { return Math.abs(this.x - this.to) < 0.0005 && Math.abs(this.v) < 0.0005; } };
  }
  const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  function create(host, opts) {
    const groups = opts.groups || [];
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const pct = (n) => (n / 10) + '%';

    const cards = groups.map((g, i) => {
      const side = SIDES[i], { edge, width } = CARDS[side];
      const left = side === 'left' ? edge : 100 - edge - width;
      return `<li class="st-li" data-side="${side}" style="top:${pct(cornerY(i))};left:${left}%;width:${width}%"><div class="st-card" data-i="${i}">${cardBody(g, i)}</div></li>`;
    }).join('');
    const connectors = groups.map((g, i) => {
      const side = SIDES[i], { edge, width } = CARDS[side], dir = side === 'right' ? 1 : -1;
      const cardEdge = side === 'left' ? (edge + width) * 10 : (100 - edge - width) * 10;
      const plateX = tipX(i, side) + dir * 7, nodeX = cardEdge - dir * 10, y = cornerY(i);
      return `<g data-con="${i}" data-px="${f2(plateX)}" data-nx="${f2(nodeX)}"><line x1="${f2(plateX)}" x2="${f2(nodeX)}" y1="${y}" y2="${y}" stroke="#cfcfd7"/>` +
        `<line class="st-lit" x1="${f2(plateX)}" x2="${f2(nodeX)}" y1="${y}" y2="${y}" stroke="${ACC}" stroke-width="1.3"/>` +
        `<circle cx="${f2(plateX)}" cy="${y}" r="2.6" fill="#a9a8b3"/>` +
        `<circle class="st-halo-node${g.main ? ' main' : ''}" cx="${f2(nodeX)}" cy="${y}" r="${g.main ? 10 : 8}" fill="${ACC}"/>` +
        `<circle cx="${f2(nodeX)}" cy="${y}" r="3.6" fill="${ACC}"/><circle data-bead cx="${f2(nodeX)}" cy="${y}" r="2.6" fill="${ACC}" opacity="0"/></g>`;
    }).join('');

    host.innerHTML = `
      <div class="st-vis">
        <div class="st-floor">${floorSvg()}</div>
        <div class="st-halo" aria-hidden="true"></div>
        <div class="st-fade"><div class="st-par" data-reach="-5">${backdropSvg()}</div></div>
        <div class="st-par" data-reach="12"><div class="st-orb">${orbitSvg()}${beadsSvg('back', !reduce)}</div></div>
        <div class="st-shadow">${shadowSvg()}</div>
        <div class="st-tilt"><div class="st-tilt-in"><div class="st-float">${coreSvg()}
          <svg class="st-cons" viewBox="0 0 1000 1000" aria-hidden="true">${connectors}</svg></div></div></div>
        <div class="st-par" data-reach="12"><div class="st-orb">${beadsSvg('front', !reduce)}</div></div>
        <div class="st-tilt"><div class="st-tilt-in"><div class="st-float"><ul class="st-cards" aria-label="What the questionnaire asks">${cards}</ul></div></div></div>
        <p class="st-hint" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l7 17 2.5-7.4L21 11Z"/></svg><span class="st-hint-m">Hover to see what we'll ask</span><span class="st-hint-t">Tap to see what we'll ask</span></p>
        <button type="button" class="st-hit" aria-expanded="false" aria-label="Show what this questionnaire asks"></button>
      </div>
      <ul class="st-list" aria-label="What the questionnaire asks">${groups.map((g, i) => `<li class="st-card" data-i="${i}">${cardBody(g, i)}</li>`).join('')}</ul>`;

    const vis = host.querySelector('.st-vis');
    const svg = vis.querySelector('.st-svg');
    const plates = PLATES.map((_, i) => svg.querySelector(`[data-plate="${i}"]`));
    const shadows = plates.map((g) => g.querySelector('[data-shadow]'));
    const skirts = plates.map((g) => g.querySelector('[data-skirt]'));
    const glow = svg.querySelector('[data-glow]');
    const emph = svg.querySelector('[data-emph]');
    const tilts = Array.from(vis.querySelectorAll('.st-tilt-in'));
    const pars = Array.from(vis.querySelectorAll('.st-par'));
    const orbs = Array.from(vis.querySelectorAll('.st-orb'));
    const beadSvgs = orbs.map((o) => { const s = o.querySelectorAll('svg'); return s[s.length - 1]; });
    const shadowEl = vis.querySelector('.st-shadow');
    const hint = vis.querySelector('.st-hint');
    const hit = vis.querySelector('.st-hit');
    const cons = Array.from(vis.querySelectorAll('[data-con]')).map((g) => ({
      g, px: +g.dataset.px, nx: +g.dataset.nx, bead: g.querySelector('[data-bead]'), lit: g.querySelector('.st-lit'),
    }));
    const consSvg = vis.querySelector('.st-cons');
    const cardEls = Array.from(vis.querySelectorAll('.st-cards .st-card'));
    const cardLis = Array.from(vis.querySelectorAll('.st-li'));

    const prog = spring(EXPLODE.stiffness, EXPLODE.damping, EXPLODE.mass);
    const tx = spring(55, 18, 0.9), ty = spring(55, 18, 0.9);
    let open = false, hovering = false, held = false, lit = null, raf = 0, last = 0, openedAt = 0, onScreen = true;

    let lastP = -1;
    function paint(now) {
      const p = prog.x;
      // the plate SVG only changes while the stack is moving; once settled only the beads animate
      if (Math.abs(p - lastP) > 1e-5) { lastP = p; paintPlates(p); }
      const rx = ty.x * -3.5 + p * 4, ry = tx.x * 3.5;
      tilts.forEach((t) => { t.style.transform = `rotateX(${rx.toFixed(3)}deg) rotateY(${ry.toFixed(3)}deg)`; });
      pars.forEach((el) => { const r = +el.dataset.reach; el.style.transform = `translate3d(${(tx.x * r).toFixed(2)}px,${(ty.x * r).toFixed(2)}px,0)`; });
      paintBeads(now);
    }
    function paintPlates(p) {
      plates.forEach((g, i) => {
        const pp = plateProgress(i, p), s = PLATES[i];
        const y = TRAVEL[i] * pp, sc = 1 + (EXPLODE.scales[i] - 1) * pp, cy = s.top + s.depth / 2;
        g.setAttribute('transform', `translate(0 ${f2(y)}) translate(${CX} ${f2(cy)}) scale(${sc.toFixed(4)}) translate(${-CX} ${f2(-cy)})`);
        if (shadows[i]) shadows[i].setAttribute('opacity', (pp * 0.16).toFixed(3));
        if (skirts[i]) skirts[i].setAttribute('opacity', pp.toFixed(3));
      });
      glow.setAttribute('opacity', (clamp((p - 0.4) / 0.5) * 0.6).toFixed(3));
      emph.setAttribute('opacity', clamp((plateProgress(ACTIVE, p) - 0.5) / 0.4).toFixed(3));
      const orbitOp = 1 - (1 - 0.15) * clamp(p / 0.6);
      orbs.forEach((o) => { o.style.opacity = orbitOp.toFixed(3); });
      shadowEl.style.opacity = (1 - 0.45 * clamp(p)).toFixed(3);
      hint.style.opacity = (1 - clamp(p / 0.25)).toFixed(3);
      const cardOp = clamp((p - 0.35) / 0.55);
      cardLis.forEach((li) => {
        const dir = li.dataset.side === 'left' ? -1 : 1;
        const x = -dir * CARDS.restInset + (dir * CARDS.outward + dir * CARDS.restInset) * clamp(p);
        li.firstElementChild.style.opacity = cardOp.toFixed(3);
        li.firstElementChild.style.transform = `translateX(${x.toFixed(2)}px)`;
      });
      consSvg.style.opacity = clamp((p - 0.55) / 0.35).toFixed(3);
    }
    function paintBeads(now) {
      const show = clamp((prog.x - 0.55) / 0.35);
      const t = (now - openedAt) / 1000;
      cons.forEach((c, i) => {
        let k = -1;
        if (open && !reduce) {
          const ph = t - (0.5 + i * 0.25);
          if (ph >= 0) { const m = ph % (CARDS.beadSeconds + 1.4); if (m < CARDS.beadSeconds) k = easeInOut(m / CARDS.beadSeconds); }
        }
        if (k < 0) { c.bead.setAttribute('opacity', '0'); return; }
        c.bead.setAttribute('cx', f2(c.nx + (c.px - c.nx) * k));
        c.bead.setAttribute('opacity', (show * Math.sin(Math.PI * k)).toFixed(3));
      });
    }

    function frame(now) {
      raf = 0;
      const dt = last ? Math.min(0.05, (now - last) / 1000) : 1 / 60;
      last = now;
      if (reduce) { prog.x = prog.to; prog.v = 0; tx.x = ty.x = 0; }
      else {
        const n = Math.ceil(dt / (1 / 120)), h = dt / n;
        for (let s = 0; s < n; s++) { prog.step(h); tx.step(h); ty.step(h); }
      }
      const settled = prog.done() && tx.done() && ty.done();
      if (settled) { prog.x = prog.to; prog.v = 0; }
      paint(now);
      if (!settled || (open && !reduce && onScreen)) raf = requestAnimationFrame(frame);
      else last = 0;
    }
    const kick = () => { if (!raf) { last = 0; raf = requestAnimationFrame(frame); } };

    function pauseBeads() {
      beadSvgs.forEach((s) => { if (!s || !s.pauseAnimations) return; if (open || !onScreen) s.pauseAnimations(); else s.unpauseAnimations(); });
    }
    function setOpen(v) {
      v = !!v;
      if (v === open) return;
      open = v;
      if (open) openedAt = performance.now();
      prog.to = open ? 1 : 0;
      hit.setAttribute('aria-expanded', String(open));
      vis.classList.toggle('open', open);
      pauseBeads();
      kick();
    }
    const want = () => setOpen(hovering || held || lit !== null);
    function light(i) {
      lit = i;
      cardEls.forEach((c, k) => c.classList.toggle('lit', k === i));
      cons.forEach((c, k) => c.lit.classList.toggle('on', k === i));
    }

    vis.addEventListener('pointermove', (e) => {
      if (reduce || e.pointerType !== 'mouse') return;
      const b = vis.getBoundingClientRect();
      tx.to = ((e.clientX - b.left) / b.width - 0.5) * 2;
      ty.to = ((e.clientY - b.top) / b.height - 0.5) * 2;
      kick();
    });
    vis.addEventListener('pointerleave', () => { tx.to = 0; ty.to = 0; hovering = false; light(held ? 0 : null); want(); kick(); });
    let ptr = 'mouse';
    hit.addEventListener('pointerdown', (e) => { ptr = e.pointerType; });
    hit.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') { hovering = true; want(); } });
    hit.addEventListener('click', (e) => {
      if (e.detail === 0 || ptr !== 'mouse') { hovering = !open; if (!hovering) light(held ? 0 : null); want(); }
    });
    hit.addEventListener('blur', () => { if (ptr !== 'mouse') { hovering = false; want(); } });
    cardEls.forEach((c, i) => {
      c.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') { hovering = true; light(i); want(); } });
      c.addEventListener('pointerleave', () => light(held ? 0 : null));
      c.addEventListener('click', () => { if (open && opts.onPick) opts.onPick(i); });
    });
    host.querySelectorAll('.st-list .st-card').forEach((c, i) => c.addEventListener('click', () => opts.onPick && opts.onPick(i)));

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([en]) => { onScreen = en.isIntersecting; pauseBeads(); if (onScreen) kick(); }, { rootMargin: '40px' }).observe(vis);
    }
    paint(performance.now());

    const liveEls = Array.from(host.querySelectorAll('[data-live]'));
    return {
      setDetails({ company, email, valid }) {
        liveEls.forEach((el) => {
          const v = el.dataset.live === 'company' ? company : email;
          el.textContent = v || (el.dataset.live === 'company' ? 'Company name' : 'Email address');
          el.classList.toggle('ph', !v);
        });
        host.querySelectorAll('.st-cb.main').forEach((c) => c.classList.toggle('ok', !!valid));
      },
      hold(v) { held = !!v; light(held ? 0 : null); want(); },
      open: setOpen,
    };
  }

  window.MMGAStack = { create };
})();

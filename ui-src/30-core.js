const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ic = (n, cls = '') => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const setText = (el, s) => { if (el && el.textContent !== s) el.textContent = s; };
const setHTML = (el, s) => { if (el && el._html !== s) { el._html = s; el.innerHTML = s; } };
const setW = (el, pct) => { if (el) { const v = clamp(pct, 0, 100).toFixed(2) + '%'; if (el.style.width !== v) el.style.width = v; } };
const setCls = (el, base, extra) => { const c = extra ? base + ' ' + extra : base; if (el && el.getAttribute('class') !== c) el.setAttribute('class', c); };

/** 解析模板字符串为 DOM，收集 [data-ref] 到 el._r */
function mount(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  const el = t.content.firstElementChild;
  const r = {};
  if (el.dataset && el.dataset.ref) r[el.dataset.ref] = el;
  el.querySelectorAll('[data-ref]').forEach(n => { r[n.dataset.ref] = n; });
  el._r = r;
  return el;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem('fnm.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('fnm.' + k, JSON.stringify(v)); } catch (e) {} },
};

const mqReduce = matchMedia('(prefers-reduced-motion: reduce)');
const reduced = () => mqReduce.matches || UI.motion === false;

/* =====================================================================
   FORMAT — 合理精度：百分比取整、速率自动单位、≥100 不带小数
   ===================================================================== */
const ok = v => v != null && v !== '' && isFinite(v);
const F = {
  int: v => !ok(v) ? '--' : String(Math.round(v)),
  d1: v => !ok(v) ? '--' : (Math.abs(v) >= 100 ? String(Math.round(v)) : (+v).toFixed(1)),
  d2: v => !ok(v) ? '--' : (+v).toFixed(2),
  /** 二进制字节 → [数值, 单位] */
  bytes(b) {
    if (!ok(b)) return ['--', ''];
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0; let v = Math.abs(b);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return [i === 0 ? String(Math.round(v)) : (v >= 100 ? String(Math.round(v)) : v.toFixed(1)), u[i]];
  },
  rate(b) { const p = F.bytes(b); return [p[0], p[1] ? p[1] + '/s' : '']; },
  bytesStr(b) { const p = F.bytes(b); return p[1] ? p[0] + ' ' + p[1] : '--'; },
  rateStr(b) { const p = F.rate(b); return p[1] ? p[0] + ' ' + p[1] : '--'; },
  /** 硬盘 / 存储池容量用十进制（与厂商标称一致） */
  dsize(b) {
    if (!ok(b)) return ['--', ''];
    const T = 1e12, G = 1e9, M = 1e6;
    if (b >= T) { const v = b / T; return [v >= 100 ? String(Math.round(v)) : v >= 10 ? v.toFixed(1) : v.toFixed(2), 'TB']; }
    if (b >= G) { const v = b / G; return [v >= 100 ? String(Math.round(v)) : v.toFixed(1), 'GB']; }
    return [String(Math.round(b / M)), 'MB'];
  },
  dsizeStr(b) { const p = F.dsize(b); return p[1] ? p[0] + ' ' + p[1] : '--'; },
  dur(sec) {
    if (!ok(sec)) return '--';
    sec = Math.max(0, Math.floor(sec)); const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
  },
  hours(h) { h = +h; if (!ok(h)) return '--'; if (h >= 8760) return (h / 8760).toFixed(1) + ' 年'; if (h >= 24) return Math.round(h / 24) + ' 天'; return h + ' 小时'; },
  p2: n => String(n).padStart(2, '0'),
  hm(ts) { const d = new Date(ts * 1000); return F.p2(d.getHours()) + ':' + F.p2(d.getMinutes()); },
  hms(ts) { const d = new Date(ts * 1000); return F.hm(ts) + ':' + F.p2(d.getSeconds()); },
  md(ts) { const d = new Date(ts * 1000); return (d.getMonth() + 1) + '/' + d.getDate(); },
  mdhm(ts) { const d = new Date(ts * 1000); return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + F.hm(ts); },
  yuan: v => !ok(v) ? '--' : '¥' + (v >= 100 ? Math.round(v) : (+v).toFixed(2)),
  kwh: v => !ok(v) ? '--' : (v >= 100 ? Math.round(v) : v >= 10 ? (+v).toFixed(1) : (+v).toFixed(2)),
};
const WEEK = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

/* =====================================================================
   TWEEN — 单一 rAF 循环驱动所有数字过渡
   ===================================================================== */
const tween = (() => {
  const live = new Map(); let raf = 0;
  function frame(now) {
    raf = 0;
    live.forEach((t, el) => {
      const p = Math.min(1, (now - t.t0) / t.dur);
      const e = 1 - Math.pow(1 - p, 3);
      const v = t.from + (t.to - t.from) * e;
      el._tv = v; t.render(v);
      if (p >= 1) live.delete(el);
    });
    if (live.size) raf = requestAnimationFrame(frame);
  }
  return function (el, to, render, dur) {
    if (!el) return;
    if (to == null || !isFinite(to)) { live.delete(el); el._tv = undefined; render(null); return; }
    const from = el._tv;
    if (from == null || reduced() || document.hidden) { live.delete(el); el._tv = to; render(to); return; }
    if (from === to && !live.has(el)) return;
    live.set(el, { from, to, t0: performance.now(), dur: dur || 650, render });
    if (!raf) raf = requestAnimationFrame(frame);
  };
})();
/** 数字过渡：fmt(v) → string */
const num = (el, v, fmt = F.int) => tween(el, v, x => setText(el, x == null ? '--' : fmt(x)));
/** 数字 + 单位过渡：parts(v) → [n, u] */
const numU = (el, uel, v, parts) => tween(el, v, x => { const p = x == null ? ['--', ''] : parts(x); setText(el, p[0]); setText(uel, p[1]); });

/* =====================================================================
   DOT-MATRIX — 移植自 C「机体」：5×7 字形画成 SVG 圆点
   - 未点亮的点淡淡可见；更新时只有变化的点做淡入淡出（CSS transition）
   - 设置「点阵数字」关闭时回退为常规粗体数字
   ===================================================================== */
const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const GLYPH_SRC = {
  '0': ['.###.', '#...#', '#..##', '#.#.#', '##..#', '#...#', '.###.'],
  '1': ['..#..', '.##..', '..#..', '..#..', '..#..', '..#..', '.###.'],
  '2': ['.###.', '#...#', '....#', '...#.', '..#..', '.#...', '#####'],
  '3': ['#####', '...#.', '..#..', '...#.', '....#', '#...#', '.###.'],
  '4': ['...#.', '..##.', '.#.#.', '#..#.', '#####', '...#.', '...#.'],
  '5': ['#####', '#....', '####.', '....#', '....#', '#...#', '.###.'],
  '6': ['..##.', '.#...', '#....', '####.', '#...#', '#...#', '.###.'],
  '7': ['#####', '....#', '...#.', '..#..', '.#...', '.#...', '.#...'],
  '8': ['.###.', '#...#', '#...#', '.###.', '#...#', '#...#', '.###.'],
  '9': ['.###.', '#...#', '#...#', '.####', '....#', '...#.', '.##..'],
  '.': ['.', '.', '.', '.', '.', '.', '#'],
  ',': ['..', '..', '..', '..', '..', '.#', '#.'],
  ':': ['.', '#', '.', '.', '.', '#', '.'],
  ';': ['.', '.', '.', '.', '.', '.', '.'],            /* 冒号“熄灭”帧（闪烁） */
  '-': ['....', '....', '....', '####', '....', '....', '....'],
  ' ': ['..', '..', '..', '..', '..', '..', '..'],
  '%': ['##...', '##..#', '...#.', '..#..', '.#...', '#..##', '...##'],
  '/': ['....#', '....#', '...#.', '..#..', '.#...', '#....', '#....'],
  '°': ['.#.', '#.#', '.#.', '...', '...', '...', '...'],
  '℃': ['.#.', '#.#', '.#.', '...', '...', '...', '...'],
  '¥': ['#...#', '.#.#.', '..#..', '#####', '..#..', '#####', '..#..'],
  '↓': ['..#..', '..#..', '..#..', '#.#.#', '.###.', '..#..', '.....'],
  '↑': ['.....', '..#..', '.###.', '#.#.#', '..#..', '..#..', '..#..'],
  'A': ['.###.', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'],
  'B': ['####.', '#...#', '#...#', '####.', '#...#', '#...#', '####.'],
  'C': ['.###.', '#...#', '#....', '#....', '#....', '#...#', '.###.'],
  'D': ['###..', '#..#.', '#...#', '#...#', '#...#', '#..#.', '###..'],
  'E': ['#####', '#....', '#....', '####.', '#....', '#....', '#####'],
  'F': ['#####', '#....', '#....', '####.', '#....', '#....', '#....'],
  'G': ['.###.', '#...#', '#....', '#.###', '#...#', '#...#', '.####'],
  'H': ['#...#', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'],
  'I': ['###', '.#.', '.#.', '.#.', '.#.', '.#.', '###'],
  'K': ['#...#', '#..#.', '#.#..', '##...', '#.#..', '#..#.', '#...#'],
  'L': ['#....', '#....', '#....', '#....', '#....', '#....', '#####'],
  'M': ['#...#', '##.##', '#.#.#', '#.#.#', '#...#', '#...#', '#...#'],
  'N': ['#...#', '##..#', '#.#.#', '#..##', '#...#', '#...#', '#...#'],
  'O': ['.###.', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'],
  'P': ['####.', '#...#', '#...#', '####.', '#....', '#....', '#....'],
  'R': ['####.', '#...#', '#...#', '####.', '#.#..', '#..#.', '#...#'],
  'S': ['.####', '#....', '#....', '.###.', '....#', '....#', '####.'],
  'T': ['#####', '..#..', '..#..', '..#..', '..#..', '..#..', '..#..'],
  'U': ['#...#', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'],
  'V': ['#...#', '#...#', '#...#', '#...#', '#...#', '.#.#.', '..#..'],
  'W': ['#...#', '#...#', '#...#', '#.#.#', '#.#.#', '##.##', '#...#'],
  'X': ['#...#', '#...#', '.#.#.', '..#..', '.#.#.', '#...#', '#...#'],
  'Y': ['#...#', '#...#', '.#.#.', '..#..', '..#..', '..#..', '..#..'],
  'Z': ['#####', '....#', '...#.', '..#..', '.#...', '#....', '#####'],
  'h': ['#....', '#....', '#.##.', '##..#', '#...#', '#...#', '#...#'],
  'k': ['#....', '#....', '#..#.', '#.#..', '##...', '#.#..', '#..#.'],
  's': ['.....', '.....', '.####', '#....', '.###.', '....#', '####.'],
  'z': ['.....', '.....', '#####', '...#.', '..#..', '.#...', '#####'],
  '?': ['.###.', '#...#', '....#', '...#.', '..#..', '.....', '..#..'],
};
const GL = {};
for (const ch in GLYPH_SRC) GL[ch] = { w: GLYPH_SRC[ch][0].length, rows: GLYPH_SRC[ch] };
const DOTS = new Set();
const dmOn = () => UI.dotMatrix !== false;
class Dot {
  constructor(el) {
    this.el = el; this.str = null; this.sig = null; this.cells = null; this.mode = null;
    el.classList.add('dm'); el.setAttribute('role', 'img'); DOTS.add(this);
    if (el.dataset.dm != null) this.set(el.dataset.dm);
  }
  set(v) {
    const raw = String(v), on = dmOn();
    if (raw === this.str && this.mode === on) return;
    this.str = raw; this.mode = on;
    const label = raw.replace(/;/g, ':');
    this.el.setAttribute('aria-label', label);
    if (!on) { this.sig = null; this.el.classList.add('dm-txt'); this.el.textContent = label; return; }
    this.el.classList.remove('dm-txt');
    const gl = Array.from(raw).map(c => GL[c] || GL[c.toUpperCase()] || GL['?']);
    const sig = gl.map(g => g.w).join(',');
    if (sig !== this.sig) { this.build(gl); this.sig = sig; }
    let k = 0;
    for (const g of gl) for (let r = 0; r < 7; r++) { const row = g.rows[r]; for (let c = 0; c < g.w; c++) { const lit = row[c] === '#', ci = this.cells[k++]; if (ci._on !== lit) { ci._on = lit; ci.setAttribute('class', lit ? 'on' : ''); } } }
  }
  build(gl) {
    const U = gl.reduce((a, g) => a + g.w, 0) + Math.max(0, gl.length - 1);
    const svg = svgEl('svg', { viewBox: `0 0 ${U} 7`, 'aria-hidden': 'true' });
    svg.style.setProperty('--u', U);
    const cells = []; let x = 0;
    for (const g of gl) { for (let r = 0; r < 7; r++) for (let c = 0; c < g.w; c++) { const ci = svgEl('circle', { cx: x + c + 0.5, cy: r + 0.5, r: 0.39 }); svg.appendChild(ci); cells.push(ci); } x += g.w + 1; }
    this.el.replaceChildren(svg); this.cells = cells;
  }
  refresh() { const s = this.str; this.str = null; this.set(s == null ? '' : s); }
}
const dotMap = new WeakMap();
const dot = el => { if (!el) return null; let d = dotMap.get(el); if (!d) { d = new Dot(el); dotMap.set(el, d); } return d; };
/** 点阵数字：fmt(v) → string（不做数值补间，变化由点的淡入淡出呈现） */
const dnum = (el, v, fmt = F.int) => { if (el) dot(el).set(v == null || !isFinite(v) ? '--' : fmt(v)); };
/** 点阵数字 + 无衬线单位 */
const dnumU = (el, uel, v, parts) => { const p = v == null || !isFinite(v) ? ['--', ''] : parts(v); if (el) dot(el).set(p[0]); setText(uel, p[1]); };
/** 扫描静态 [data-dm] 节点 */
const dmScan = root => $$('[data-dm]', root).forEach(el => dot(el));
/** HTML 片段：点阵数字 + 单位 */
const dmHTML = (str, unit = '', cls = '') => `<span class="dnum ${cls}"><span class="dm" data-dm="${esc(str)}"></span>${unit ? `<span class="unit">${esc(unit)}</span>` : ''}</span>`;

/** LED 点条（C 的 dbar）：el[data-n] 个段，按比例点亮 */
function dbar(el, frac, lvl = '', ramp = null) {
  if (!el) return;
  const n = +el.dataset.n || 24;
  if (el.children.length !== n) el.innerHTML = '<i></i>'.repeat(n);
  const k = Math.round(clamp(frac || 0, 0, 1) * n), useRamp = ramp && lvl !== 'sleep';
  Array.from(el.children).forEach((c, i) => {
    const on = i < k; if (c._on !== on) { c._on = on; c.className = on ? 'on' : ''; }
    const col = on && useRamp ? rampAt((i + .5) / n * 100, ramp) : '';
    if (c._col !== col) { c._col = col; c.style.background = col; }
  });
  setCls(el, 'dbar', lvl);
}
/* =====================================================================
   负载色阶 —— 容量 / 进度类控件按数值着色（规范见 CSS「负载色阶」）
   每种指标 5 个色标：[数值, 颜色]，阈值取自设置，改阈值即改色阶
   ===================================================================== */
const RG = 'var(--r-g)', RY = 'var(--r-y)', RA = 'var(--warn-fill)', RC = 'var(--crit)';
const mono = st => { for (let i = 1; i < st.length; i++) if (st[i][0] <= st[i - 1][0]) st[i][0] = st[i - 1][0] + 0.01; return st; };
const RAMP = {
  pct:   (w, c) => mono([[0, RG], [w * .5, RG], [w * .8, RY], [w, RA], [Math.max(c, w), RC]]),
  cpu:   () => RAMP.pct(TH.cpuWarn, TH.cpuCrit),
  mem:   () => RAMP.pct(TH.memWarn, TH.memCrit),
  cap:   () => RAMP.pct(TH.capWarn, TH.capCrit),
  gen:   () => RAMP.pct(70, 90),
  temp:  () => { const w = TH.tempWarn, c = TH.tempCrit; return mono([[Math.min(30, w - 26), RG], [w - 25, RG], [w - 10, RY], [w, RA], [c, RC]]); },
  dtemp: () => { const w = TH.diskTempWarn, c = TH.diskTempCrit; return mono([[Math.min(20, w - 13), RG], [w - 12, RG], [w - 4, RY], [w, RA], [c, RC]]); },
};
const rampStops = k => Array.isArray(k) ? k : RAMP[k]();
/** 数值 v 在色阶上的颜色 */
function rampAt(v, k) {
  const st = rampStops(k);
  if (v <= st[0][0]) return st[0][1];
  for (let i = 1; i < st.length; i++) if (v <= st[i][0]) {
    const t = clamp((v - st[i - 1][0]) / ((st[i][0] - st[i - 1][0]) || 1), 0, 1);
    return t < .02 ? st[i - 1][1] : t > .98 ? st[i][1] : `color-mix(in oklch, ${st[i][1]} ${Math.round(t * 100)}%, ${st[i - 1][1]})`;
  }
  return st[st.length - 1][1];
}
/** 覆盖整条轨道的色阶渐变；pos 把数值映射到轨道百分比 */
function rampGrad(k, dir = '90deg', pos = v => v) {
  return `linear-gradient(${dir}, ${rampStops(k).map(([v, c]) => `${c} ${clamp(pos(v), 0, 100).toFixed(1)}%`).join(', ')})`;
}
/** 容量条：长度 = 数值，填充 = 色阶从 0 走到当前值的那一段（越满越“热”） */
function rampBar(el, v, k, pos = x => x, vertical = false) {
  if (!el) return;
  const len = clamp(pos(v), 0, 100);
  if (vertical) el.style.height = len.toFixed(1) + '%'; else setW(el, len);
  const g = rampGrad(k, vertical ? '0deg' : '90deg', pos), big = (100 / Math.max(len, .5) * 100).toFixed(1) + '%';
  if (el._rg !== g) { el._rg = g; el.style.backgroundImage = g; }
  const sz = vertical ? `100% ${big}` : `${big} 100%`;
  if (el.style.backgroundSize !== sz) { el.style.backgroundSize = sz; el.style.backgroundPosition = vertical ? 'bottom' : 'left'; }
}
/** 与控件联动的数字：同一色阶的文字版（浅色模式压暗以保证对比度） */
function rampInk(v, k) {
  const st = rampStops(k);
  if (v >= st[4][0]) return 'var(--crit)';
  if (v >= st[3][0]) return 'var(--warn)';
  // 黄绿 → 橙 这一段：填充色过了中点就已明显偏橙，数字在同一点切换为橙，保持同步又不经过发棕的中间色
  if (v >= (st[2][0] + st[3][0]) / 2) return 'var(--warn)';
  const t = clamp((v - st[1][0]) / ((st[2][0] - st[1][0]) || 1), 0, 1);   // 绿 → 黄绿
  return t < .02 ? 'var(--rt-g)' : t > .98 ? 'var(--rt-l)' : `color-mix(in oklch, var(--rt-l) ${Math.round(t * 100)}%, var(--rt-g))`;
}
const inkTo = (el, col) => { if (el && el._ink !== col) { el._ink = col; el.style.color = col; } };
const rampOff = el => { if (el && el._rg) { el._rg = null; el.style.backgroundImage = ''; } };

/** 点阵柱（C 的 bay gauge）：cols × rows 个点，自下而上点亮 */
function dotGauge(host, cols = 3, rows = 14) {
  const svg = svgEl('svg', { viewBox: `0 0 ${cols} ${rows}`, preserveAspectRatio: 'xMidYMid meet', 'aria-hidden': 'true' });
  const order = [];
  for (let r = rows - 1; r >= 0; r--) for (let c = 0; c < cols; c++) { const ci = svgEl('circle', { cx: c + 0.5, cy: r + 0.5, r: 0.34 }); svg.appendChild(ci); order.push(ci); }
  host.prepend(svg);
  return { set(frac, l, ramp = null) {
    const k = Math.round(clamp(frac, 0, 1) * order.length);
    order.forEach((c, i) => {
      const want = i < k; if (c._on !== want) { c._on = want; c.setAttribute('class', want ? 'on' : ''); }
      const col = want && ramp ? rampAt((Math.floor(i / cols) + 1) / rows * 100, ramp) : '';
      if (c._col !== col) { c._col = col; c.style.fill = col; }
    });
    if ((host.dataset.l || '') !== (l || '')) host.dataset.l = l || '';
  } };
}

/* =====================================================================
   SIZE OBSERVER — 图表按真实像素绘制
   ===================================================================== */
const sizeObs = new ResizeObserver(entries => {
  entries.forEach(en => { const t = en.target; const w = Math.round(en.contentRect.width), h = Math.round(en.contentRect.height); if (w !== t._w || h !== t._h) { t._w = w; t._h = h; t._onResize && t._onResize(); } });
});
const observe = (el, fn) => { el._onResize = fn; sizeObs.observe(el); };

/* =====================================================================
   SEGMENTED CONTROL（带滑动指示块）
   ===================================================================== */
function makeSeg(options, value, onChange, cls = '') {
  const el = mount(`<div class="seg ${cls}" role="radiogroup"><span class="thumb"></span>${options.map(([v, l]) => `<button type="button" role="radio" data-v="${esc(v)}" aria-checked="false">${esc(l)}</button>`).join('')}</div>`);
  const thumb = el.querySelector('.thumb');
  const place = () => {
    const b = el.querySelector('[aria-checked="true"]');
    if (!b || !b.offsetWidth) return;
    thumb.style.width = b.offsetWidth + 'px';
    thumb.style.transform = `translateX(${b.offsetLeft}px)`;
  };
  el.set = v => { el.value = v; $$('button', el).forEach(b => b.setAttribute('aria-checked', String(b.dataset.v === String(v)))); place(); };
  el.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const opt = options.find(o => String(o[0]) === b.dataset.v);
    if (String(el.value) === b.dataset.v) return;
    el.set(opt[0]); onChange && onChange(opt[0]);
  });
  el.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const i = options.findIndex(o => String(o[0]) === String(el.value));
    const n = options[clamp(i + (e.key === 'ArrowRight' ? 1 : -1), 0, options.length - 1)];
    if (n && String(n[0]) !== String(el.value)) { el.set(n[0]); onChange && onChange(n[0]); el.querySelector('[aria-checked="true"]').focus(); }
    e.preventDefault();
  });
  observe(el, place);
  el.set(value);
  return el;
}
/** 顶部导航这种静态 seg 的滑块定位 */
function placeThumb(seg) {
  const b = seg.querySelector('[aria-selected="true"]'), t = seg.querySelector('.thumb');
  if (!b || !t || !b.offsetWidth) return;
  t.style.width = b.offsetWidth + 'px'; t.style.transform = `translateX(${b.offsetLeft}px)`;
}

/* =====================================================================
   GAUGE（270° 弧形仪表）& SPARKLINE
   ===================================================================== */
function makeGauge(icon) {
  const r = 40, C = 2 * Math.PI * r, A = C * 0.75;
  const el = mount(`<svg class="m-gauge" viewBox="0 0 100 100" aria-hidden="true">
    <g transform="rotate(135 50 50)">
      <circle class="gauge-track" cx="50" cy="50" r="${r}" fill="none" stroke-width="7" stroke-linecap="round" stroke-dasharray="${A.toFixed(2)} ${C.toFixed(2)}"/>
      <circle data-ref="v" class="gauge-val" cx="50" cy="50" r="${r}" fill="none" stroke-width="7" stroke-linecap="round" stroke-dasharray="${A.toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${A.toFixed(2)}"/>
    </g>
    ${icon ? `<use href="#i-${icon}" x="38" y="38" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" style="color:var(--ink-3)"/>` : ''}
  </svg>`);
  el.set = (f, lvl) => {
    const off = (A * (1 - clamp(f || 0, 0, 1))).toFixed(2);
    if (el._r.v.style.strokeDashoffset !== off + 'px') el._r.v.style.strokeDashoffset = off + 'px';
    setCls(el._r.v, 'gauge-val', lvl);
  };
  el.tone = col => { if (el._r.v.style.stroke !== col) el._r.v.style.stroke = col; };
  return el;
}

/** 单调三次插值，曲线平滑且不过冲 */
function monoPath(xs, ys) {
  const n = xs.length; if (!n) return '';
  const f = v => Math.round(v * 10) / 10;
  if (n === 1) return `M${f(xs[0])},${f(ys[0])}`;
  const dx = [], m = [], t = new Array(n);
  for (let i = 0; i < n - 1; i++) { dx[i] = xs[i + 1] - xs[i] || 1e-6; m[i] = (ys[i + 1] - ys[i]) / dx[i]; }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : 3 * (dx[i - 1] + dx[i]) / ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
  let d = `M${f(xs[0])},${f(ys[0])}`;
  for (let i = 0; i < n - 1; i++) { const h = dx[i] / 3; d += `C${f(xs[i] + h)},${f(ys[i] + t[i] * h)} ${f(xs[i + 1] - h)},${f(ys[i + 1] - t[i + 1] * h)} ${f(xs[i + 1])},${f(ys[i + 1])}`; }
  return d;
}

function makeSpark(cls = 'spark') {
  const el = mount(`<svg class="${cls}" aria-hidden="true"><g data-ref="g"></g></svg>`);
  el._series = [];
  /** series: [{values, color?, area?}] ; opt: {min, max} */
  el.set = (series, opt = {}) => { el._series = series; el._opt = opt; draw(); };
  function draw() {
    const w = el._w || el.clientWidth, h = el._h || el.clientHeight; if (!w || !h) return;
    const ser = el._series; if (!ser.length) return;
    let lo = opt().min, hi = opt().max;
    const all = ser.flatMap(s => s.values);
    if (lo == null) lo = Math.min(...all); if (hi == null) hi = Math.max(...all);
    if (hi - lo < 1e-9) { hi = lo + Math.max(1, Math.abs(lo) * .5); }
    if (opt().max == null) hi += (hi - lo) * .18;
    const pad = 3;
    let html = '';
    ser.forEach((s, si) => {
      const n = s.values.length; if (n < 2) return;
      const xs = s.values.map((_, i) => i / (n - 1) * w);
      const ys = s.values.map(v => pad + (1 - (clamp(v, lo, hi) - lo) / (hi - lo)) * (h - pad * 2));
      const d = monoPath(xs, ys);
      if (s.area !== false && si === 0) html += `<path class="area" d="${d}L${w},${h}L0,${h}Z"/>`;
      html += `<path class="line${si ? ' l2' : ''}" d="${d}"/>`;
      if (si === 0 && opt().live !== 'none') html += opt().live === 'ink' ? `<circle class="ink" cx="${w}" cy="${ys[n - 1].toFixed(1)}" r="2"/>` : `<circle class="halo" cx="${w}" cy="${ys[n - 1].toFixed(1)}" r="5"/><circle cx="${w}" cy="${ys[n - 1].toFixed(1)}" r="2.5"/>`;
    });
    el._r.g.innerHTML = html;
  }
  const opt = () => el._opt || {};
  observe(el, draw);
  return el;
}


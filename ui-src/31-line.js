/* =====================================================================
   LINE CHART — 手绘 SVG：每条序列自带时间戳，断档不连线，十字线同步，阈值着色
   ===================================================================== */
const Bus = {
  g: {},
  join(group, c) { (this.g[group] = this.g[group] || new Set()).add(c); },
  leave(group, c) { this.g[group] && this.g[group].delete(c); },
  emit(group, ts, src) { (this.g[group] || []).forEach(c => c.showAt(ts, c === src)); },
};
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw))); const n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
const bsearch = (a, ts) => { let lo = 0, hi = a.length - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (a[m][0] < ts) lo = m; else hi = m; } return Math.abs(a[lo][0] - ts) <= Math.abs(a[hi][0] - ts) ? lo : hi; };
const medianDt = pts => { if (pts.length < 2) return 60; const d = []; for (let i = 1; i < pts.length; i++) d.push(pts[i][0] - pts[i - 1][0]); d.sort((a, b) => a - b); return d[d.length >> 1] || 60; };
/** 按断档切分：相邻点间隔超过 max(3×中位间隔, 150s) 视为停机/缺数据，不连线 */
function segments(pts) {
  if (!pts.length) return [];
  const gap = Math.max(medianDt(pts) * 3, 150), out = [[pts[0]]];
  for (let i = 1; i < pts.length; i++) { if (pts[i][0] - pts[i - 1][0] > gap) out.push([]); out[out.length - 1].push(pts[i]); }
  return out;
}
class LineChart {
  /** opt: { series:[{label,color,fmt}], yMin, yMax, yTick, peak, thresholds:()=>[{v,lv}], group, legend, height, empty:()=>string } */
  constructor(host, opt) {
    this.host = host; this.opt = opt; this.cols = []; this.hoverTs = null; this.span = 0;
    host.classList.add('chart'); if (opt.height) host.style.height = opt.height + 'px';
    host.innerHTML = `<svg role="img" aria-label="${esc(opt.series.map(s => s.label).join(' / '))} 趋势"><g class="gg"></g><g class="gs"></g><g class="gp"></g><line class="xh" y1="0"/><g class="gd"></g><rect class="hit"/></svg><div class="tip"></div><div class="chart-empty" hidden></div>`;
    this.svg = host.firstChild; this.tip = host.querySelector('.tip'); this.emptyEl = host.querySelector('.chart-empty');
    [this.gGrid, this.gSer, this.gPeak, this.xh, this.gDots, this.hit] = [...this.svg.children];
    if (opt.group) Bus.join(opt.group, this);
    if (opt.legend) this.buildLegend();
    const move = e => {
      if (!this.X) return;
      const r = this.svg.getBoundingClientRect();
      const x = clamp((e.clientX - r.left - this.L) / this.PW, 0, 1);
      const ts = this.t0 + x * (this.t1 - this.t0);
      opt.group ? Bus.emit(opt.group, ts, this) : this.showAt(ts, true);
    };
    const leave = () => { opt.group ? Bus.emit(opt.group, null, this) : this.showAt(null, true); };
    this.hit.addEventListener('pointermove', move);
    this.hit.addEventListener('pointerdown', move);
    this.hit.addEventListener('pointerleave', leave);
    this.hit.addEventListener('pointercancel', leave);
    observe(host, () => this.render());
  }
  destroy() { if (this.opt.group) Bus.leave(this.opt.group, this); sizeObs.unobserve(this.host); }
  buildLegend() {
    this.opt.legend.innerHTML = this.opt.series.map((s, i) => `<span><span class="lk"><i class="k${i ? 1 : 0}" style="--k:${s.color}"></i>${esc(s.label)}</span><span class="lv">--<small></small></span></span>`).join('');
    this.lv = $$('.lv', this.opt.legend);
  }
  /** cols: 每条序列一个 [[ts, v], …] 数组；span: 所选时间范围（秒），0 = 按数据范围 */
  setData(cols, span) {
    const clean = (cols || []).map(c => (c || []).filter(p => p && p[1] != null && isFinite(p[1])));
    const sig = clean.map(c => c.length + ':' + (c.length ? c[c.length - 1][0] + ',' + c[c.length - 1][1] + ',' + c[0][0] : '')).join('|') + '|' + span;
    if (sig === this._sig) return;
    this._sig = sig; this.cols = clean; this.span = span || 0; this.render();
  }
  render() {
    const w = this.host._w || this.host.clientWidth, h = this.host._h || this.host.clientHeight;
    const o = this.opt, all = this.cols.flat();
    const empty = !all.length;
    this.emptyEl.hidden = !empty; this.svg.style.visibility = empty ? 'hidden' : '';
    if (empty) { setText(this.emptyEl, this._sig === undefined ? '正在加载历史…' : (o.empty && o.empty()) || '暂无数据'); this.X = null; this.cols.forEach((c, i) => this.setLegend(i, null)); return; }
    if (!w || !h) return;
    const L = this.L = o.yTick ? 40 : 8, R = 4, T = 10, B = 22;
    const PW = this.PW = w - L - R, PH = this.PH = h - T - B;
    const vals = all.map(p => p[1]);
    let lo = o.yMin != null ? o.yMin : Math.min(...vals);
    let hi = o.yMax != null ? o.yMax : Math.max(...vals);
    if (o.yMin == null) lo = Math.max(o.floor0 ? 0 : -Infinity, lo - (hi - lo) * 0.15);
    let step = niceStep(((hi - lo) || Math.abs(hi) || 1) / 3);
    if (o.yMax == null) hi = Math.ceil(hi / step) * step; else step = (hi - lo) / 4;
    if (o.yMin == null) lo = Math.floor(lo / step) * step;
    if (hi <= lo) hi = lo + step;
    this.lo = lo; this.hi = hi;
    let t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
    if (this.span && t1 - t0 < this.span * 0.8) { t1 = Math.max(t1, Date.now() / 1000); t0 = t1 - this.span; }
    if (t1 - t0 < 1) t0 = t1 - 60;
    this.t0 = t0; this.t1 = t1;
    const X = this.X = ts => L + (ts - t0) / (t1 - t0) * PW;
    const Y = this.Y = v => T + (1 - (clamp(v, lo, hi) - lo) / (hi - lo)) * PH;
    let g = '';
    for (let v = lo; v <= hi + step * 0.01; v += step) {
      const y = Math.round(Y(v)) + .5;
      g += `<line class="grid-l" x1="${L}" x2="${L + PW}" y1="${y}" y2="${y}"/>`;
      if (o.yTick) g += `<text class="axis-t" x="${L - 8}" y="${y + 3.5}" text-anchor="end">${esc(o.yTick(+v.toFixed(6)))}</text>`;
    }
    const span = t1 - t0, nT = w < 420 ? 3 : 5;
    for (let i = 0; i < nT; i++) {
      const ts = t0 + span * (i + 0.5) / nT;
      g += `<text class="axis-t" x="${X(ts).toFixed(1)}" y="${h - 4}" text-anchor="middle">${span > 86400 * 1.5 ? F.md(ts) : F.hm(ts)}</text>`;
    }
    let s = '', d = '', g2 = '', p0 = '';
    this.cols.forEach((col, i) => {
      const c = (o.series[i] && o.series[i].color) || 'var(--ink-1)';
      let path = '', area = '';
      segments(col).forEach(seg => {
        const xs = seg.map(p => X(p[0])), ys = seg.map(p => Y(p[1]));
        const mp = seg.length === 1 ? `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}h1` : monoPath(xs, ys);
        path += mp;
        if (i === 0 && seg.length > 1) area += `${mp}L${xs[xs.length - 1].toFixed(1)},${T + PH}L${xs[0].toFixed(1)},${T + PH}Z`;
      });
      if (i === 0) { p0 = path; if (area) s += `<path class="ar" d="${area}" style="fill:${c};fill-opacity:.1"/>`; }
      s += `<path class="ln${i ? ' l2' : ''}" d="${path}" style="stroke:${c}"/>`;
      d += `<circle class="dot" r="3.5" style="stroke:${c}"/>`;
    });
    const c0 = this.cols[0];
    if (c0 && c0.length) { const lp = c0[c0.length - 1], lx = X(lp[0]), ly = Y(lp[1]); s += `<g class="live-pt" style="color:${o.series[0].color || 'var(--ink-1)'}"><circle class="h" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="6"/><circle class="c" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3"/></g>`; }
    const ths = (o.thresholds ? o.thresholds() : []).filter(t => t.v > lo && t.v < hi);
    if (ths.length && p0) {
      ths.forEach(t => {
        const y = Y(t.v), id = 'th' + (LineChart.uid = (LineChart.uid || 0) + 1), col = t.lv === 'crit' ? 'var(--crit)' : 'var(--warn-fill)';
        s += `<clipPath id="${id}"><rect x="${L}" y="${T - 4}" width="${PW + 8}" height="${Math.max(0, y - T + 4).toFixed(1)}"/></clipPath><path class="ln th-seg" d="${p0}" clip-path="url(#${id})" style="stroke:${col}"/>`;
        g2 += `<line class="th-l" x1="${L}" x2="${L + PW}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" style="stroke:${col}"/><text class="th-t" x="${L + PW - 2}" y="${(y - 4).toFixed(1)}" text-anchor="end" style="fill:${col}">${t.lv === 'crit' ? '严重' : '警告'} ${esc(o.yTick ? o.yTick(t.v) : t.v)}</text>`;
      });
    }
    this.gGrid.innerHTML = g + g2;
    this.gSer.innerHTML = s; this.gDots.innerHTML = d;
    let pk = '';
    if (o.peak && c0 && c0.length > 2) {
      let mi = 0; c0.forEach((p, i) => { if (p[1] > c0[mi][1]) mi = i; });
      const px = X(c0[mi][0]), py = Y(c0[mi][1]), lab = '峰值 ' + joinU(o.series[0].fmt(c0[mi][1]));
      const anchor = px > L + PW - 60 ? 'end' : px < L + 60 ? 'start' : 'middle';
      pk = `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3" style="fill:${o.series[0].color || 'var(--ink-1)'};stroke:var(--card);stroke-width:2"/><text class="peak-t" x="${px.toFixed(1)}" y="${Math.max(10, py - 9).toFixed(1)}" text-anchor="${anchor}">${esc(lab)}</text>`;
    }
    this.gPeak.innerHTML = pk;
    this.xh.setAttribute('y1', T); this.xh.setAttribute('y2', T + PH);
    this.hit.setAttribute('x', L); this.hit.setAttribute('y', 0); this.hit.setAttribute('width', Math.max(0, PW)); this.hit.setAttribute('height', h);
    this.showAt(this.hoverTs, this._src);
  }
  /** 某序列在 ts 附近的值（超出 2 个采样间隔视为无数据） */
  valAt(col, ts) {
    if (!col.length) return null;
    const i = bsearch(col, ts), p = col[i];
    return Math.abs(p[0] - ts) <= Math.max(medianDt(col) * 2, 90) ? p : null;
  }
  showAt(ts, isSrc) {
    this.hoverTs = ts; this._src = isSrc;
    const o = this.opt;
    if (ts == null || !this.X || ts < this.t0 - 1 || ts > this.t1 + 1) {
      this.host.classList.remove('hovering'); this.tip.classList.remove('show');
      this.cols.forEach((c, i) => this.setLegend(i, c.length ? c[c.length - 1][1] : null));
      return;
    }
    const ref = this.cols.find(c => c.length); if (!ref) return;
    const rp = ref[bsearch(ref, ts)], x = this.X(rp[0]);
    this.host.classList.add('hovering');
    this.xh.setAttribute('x1', x); this.xh.setAttribute('x2', x);
    const dots = this.gDots.children, vals = this.cols.map(c => this.valAt(c, rp[0]));
    vals.forEach((p, k) => {
      if (dots[k]) { if (p) { dots[k].setAttribute('cx', this.X(p[0])); dots[k].setAttribute('cy', this.Y(p[1])); dots[k].style.display = ''; } else dots[k].style.display = 'none'; }
      this.setLegend(k, p ? p[1] : null);
    });
    if (isSrc) {
      const span = this.t1 - this.t0;
      this.tip.innerHTML = `<div class="tt">${span > 86400 * 1.5 ? F.mdhm(rp[0]) : F.hms(rp[0])}</div>` +
        vals.map((p, k) => `<div class="tr"><span><i class="k${k ? 1 : 0}" style="--k:${o.series[k].color}"></i>${esc(o.series[k].label)}</span><b>${esc(p ? joinU(o.series[k].fmt(p[1])) : '--')}</b></div>`).join('');
      const w = this.host._w || this.host.clientWidth, tw = this.tip.offsetWidth || 140;
      this.tip.style.left = (x + 14 + tw > w ? x - 14 - tw : x + 14) + 'px'; this.tip.style.top = '4px';
      this.tip.classList.add('show');
    } else this.tip.classList.remove('show');
  }
  setLegend(i, v) {
    if (!this.lv || !this.lv[i]) return;
    const p = v == null ? ['--', ''] : this.opt.series[i].fmt(v);
    const el = this.lv[i]; const k = p.join('|'); if (el._k === k) return; el._k = k;
    el.innerHTML = `${esc(p[0])}<small>${esc(p[1] || '')}</small>`;
  }
}


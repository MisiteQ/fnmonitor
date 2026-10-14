/* =====================================================================
   BAR CHART — 堆叠柱，2px 间隔，逐柱悬浮
   ===================================================================== */
class BarChart {
  /** opt: { series:[{key,label,color}], fmt:v=>string, title:i=>string, xLabel:i=>string|null, height } */
  constructor(host, opt) {
    this.host = host; this.opt = opt; this.rows = [];
    host.classList.add('bars'); if (opt.height) host.style.height = opt.height + 'px';
    host.innerHTML = `<svg role="img"></svg><div class="tip"></div>`;
    this.svg = host.firstChild; this.tip = host.lastChild;
    this.svg.addEventListener('pointermove', e => {
      const r = e.target.closest('[data-i]'); if (!r) return this.hover(-1);
      this.hover(+r.dataset.i);
    });
    this.svg.addEventListener('pointerleave', () => this.hover(-1));
    observe(host, () => this.render());
  }
  setData(rows) { this.rows = rows; this.render(); }
  render() {
    const w = this.host._w || this.host.clientWidth, h = this.host._h || this.host.clientHeight;
    if (!w || !h) return;
    if (!this.rows.length) { this.svg.innerHTML = ''; return; }
    const o = this.opt, L = 0, T = 8, B = 22, PH = h - T - B, n = this.rows.length;
    const tot = this.rows.map(r => o.series.reduce((s, se) => s + r[se.key], 0));
    const max = Math.max(...tot, 1e-9) * 1.08;
    const slot = w / n, bw = Math.max(3, Math.min(22, slot - 3));
    let g = '';
    [0.5, 1].forEach(f => { const y = Math.round(T + PH * (1 - f / 1.08)) + .5; g += `<line class="grid-l" x1="0" x2="${w}" y1="${y}" y2="${y}" style="stroke:var(--line)"/>`; });
    this.rows.forEach((r, i) => {
      const x = L + slot * i + (slot - bw) / 2; let y = T + PH;
      g += `<g data-i="${i}"><rect x="${(slot * i).toFixed(1)}" y="0" width="${slot.toFixed(1)}" height="${h}" fill="transparent"/>`;
      o.series.forEach((se, k) => {
        const hh = r[se.key] / max * PH; if (hh <= 0) return;
        const top = k === o.series.length - 1 || o.series.slice(k + 1).every(s2 => !r[s2.key]);
        const gap = k > 0 ? 2 : 0;
        y -= hh;
        g += `<rect class="b" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, hh - gap).toFixed(1)}" rx="${top ? Math.min(3, bw / 2) : 1}" style="fill:${se.color}${r.dim ? ';fill-opacity:.42' : ''}"/>`;
      });
      if (!r.dim && this.rows.some(z => z.dim)) g += `<circle class="live-pt" cx="${(x + bw / 2).toFixed(1)}" cy="${(y - 6).toFixed(1)}" r="2.5"/>`;
      g += '</g>';
      const lab = o.xLabel && o.xLabel(i);
      if (lab) g += `<text class="axis-t" x="${(x + bw / 2).toFixed(1)}" y="${h - 4}" text-anchor="middle" style="fill:var(--ink-3);font-size:10.5px">${esc(lab)}</text>`;
    });
    this.svg.innerHTML = g;
  }
  hover(i) {
    if (i === this._hi || (i >= 0 && !this.rows[i])) return; this._hi = i;
    this.host.classList.toggle('hovering', i >= 0);
    $$('g[data-i]', this.svg).forEach(g => $$('rect.b', g).forEach(r => r.classList.toggle('on', +g.dataset.i === i)));
    if (i < 0) return this.tip.classList.remove('show');
    const o = this.opt, r = this.rows[i];
    this.tip.innerHTML = `<div class="tt">${esc(o.title(i))}</div>` + o.series.map((se, k) => `<div class="tr"><span><i class="kb${k ? 1 : 0}" style="--k:${se.color}"></i>${esc(se.label)}</span><b>${esc(o.fmt(r[se.key]))}</b></div>`).join('');
    const w = this.host._w, slot = w / this.rows.length, x = slot * (i + .5), tw = this.tip.offsetWidth || 140;
    this.tip.style.left = clamp(x - tw / 2, 0, w - tw) + 'px'; this.tip.style.top = '-8px'; this.tip.style.transform = 'translateY(-100%)';
    this.tip.classList.add('show');
  }
}

/* 读取 CSS token（图表颜色需要真实色值写入 SVG 属性时使用 var() 即可） */
const joinU = p => p[1] && /^[A-Za-z]/.test(p[1]) ? p[0] + ' ' + p[1] : p[0] + (p[1] || '');
/* 分析层数据实体色（见 CSS 色彩规范 v5） */
const M = { cpu: 'var(--e-blue)', p2: 'var(--e-teal)', mem: 'var(--e-teal)', io: 'var(--e-blue)', io2: 'var(--e-coral)', temp: 'var(--e-coral)', temp2: 'var(--e-violet)', pwr: 'var(--e-teal)', fan: 'var(--e-violet)' };
const C = { c1: 'var(--c1)', c2: 'var(--c2)', c3: 'var(--c3)', c4: 'var(--c4)', ink: 'var(--ink-1)', ink3: 'var(--ink-3)', ok: 'var(--ok)', warn: 'var(--warn)', crit: 'var(--crit)', sleep: 'var(--sleep)' };

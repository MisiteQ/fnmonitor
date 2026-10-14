/* ---------------------------------------------------------------------
   03 TRENDS — 历史（/api/history）+ 实时拼接，同步十字线，阈值线
   --------------------------------------------------------------------- */
const SERIES_SRC = { 'net.rx': ['net', 'rx'], 'net.tx': ['net', 'tx'], 'diskio.r': ['diskio', 'r'], 'diskio.w': ['diskio', 'w'], 'net_bytes.rx': ['net_bytes', 'rx'], 'net_bytes.tx': ['net_bytes', 'tx'] };
const LIVE_OF = { cpu: 'cpu', mem: 'mem', 'net.rx': 'rx', 'net.tx': 'tx', 'diskio.r': 'dr', 'diskio.w': 'dw', temp: 'temp', temp_mb: 'tempMb', power: 'power', fan_avg: 'fan', disk_total: 'disk', load1: 'load' };
const histIv = () => (S.cfg && S.cfg.history_interval) || 60;
async function fetchSeries(keys, range) {
  const bases = [...new Set(keys.map(k => (SERIES_SRC[k] || [k])[0]))], res = {};
  await Promise.all(bases.map(b => Hist.get(b, range).then(d => { res[b] = d; }, () => { res[b] = null; })));
  return res;
}
function seriesOf(res, key, range) {
  const [b, f] = SERIES_SRC[key] || [key, 'points'], d = res && res[b], pts = (d && d[f]) || [];
  if (!['10m', '1h', '6h'].includes(range) || !LIVE_OF[key]) return pts;
  const cut = Date.now() / 1000 - RANGE_SEC[range];
  return mergeLive(pts, S.live[LIVE_OF[key]]).filter(p => p[0] >= cut);
}
const emptyFor = key => () => {
  const o = S.o || {};
  if (key === 'temp' && cpuTemp(o) == null && mbTemp(o) == null) return '未检测到 CPU / 主板温度传感器';
  if (key === 'fan_avg' && !((o.sensors || {}).fans || []).length) return '未检测到可读取的风扇转速传感器（已适配 hwmon / IPMI / ThinkPad / EC，多为固件未开放）';
  if (key === 'power' && !pwrOk(o)) return '功耗不可用';
  return `暂无数据 · 后台每 ${histIv()} 秒记录一条历史，稍后自动出现`;
};
const TREND_DEFS = [
  { id: 'cpumem', title: 'CPU 与内存', sheet: 'cpu', yMin: 0, yMax: 100, yTick: v => v + '%', peak: true, thresholds: () => [{ v: TH.cpuWarn, lv: 'warn' }, { v: TH.cpuCrit, lv: 'crit' }], series: [{ m: 'cpu', label: 'CPU', color: M.cpu, fmt: fPct }, { m: 'mem', label: '内存', color: M.mem, fmt: fPct }] },
  { id: 'net', title: '网络', sheet: 'net', yMin: 0, yTick: shortBytes, series: [{ m: 'net.rx', label: '下行', color: M.io, fmt: F.rate }, { m: 'net.tx', label: '上行', color: M.io2, fmt: F.rate }] },
  { id: 'io', title: '磁盘读写', sheet: 'storage', yMin: 0, yTick: v => v + 'M', series: [{ m: 'diskio.r', label: '读取', color: M.io, fmt: fMBs }, { m: 'diskio.w', label: '写入', color: M.io2, fmt: fMBs }] },
  { id: 'temp', title: '温度', sheet: 'temp', yTick: v => v + '°', thresholds: () => [{ v: TH.tempWarn, lv: 'warn' }, { v: TH.tempCrit, lv: 'crit' }], series: [{ m: 'temp', label: 'CPU', color: M.temp, fmt: fDeg }, { m: 'temp_mb', label: '主板', color: M.temp2, fmt: fDeg }] },
  { id: 'power', title: '整机功耗', sheet: 'power', yMin: 0, yTick: v => v + 'W', peak: true, series: [{ m: 'power', label: '功耗', color: M.pwr, fmt: fW }] },
  { id: 'fan', title: '风扇转速', sheet: 'temp', yMin: 0, yTick: v => String(v), series: [{ m: 'fan_avg', label: '平均转速', color: M.fan, fmt: fRpm }] },
];
const chartOpt = (d, extra) => Object.assign({ series: d.series, yMin: d.yMin, yMax: d.yMax, yTick: d.yTick, peak: d.peak, thresholds: d.thresholds, empty: emptyFor(d.series[0].m) }, extra);
defineModule({
  id: 'trends', page: 'overview', kicker: 'TRENDS', title: '趋势', dek: '历史记录与实时数据，指针悬停可对齐所有图表',
  actions: m => (m.seg = makeSeg(RANGES, UI.range, v => { UI.range = v; saveUI(); m.load(); }, 'sm')),
  build(body, m) {
    body.innerHTML = `<div class="grid g-2">${TREND_DEFS.map(d => `<div class="card chart-card"><div class="chart-head"><button class="chart-title link-like" data-sheet="${d.sheet}" type="button">${d.title}</button><div class="chart-legend" data-ref="leg-${d.id}"></div></div><div data-ref="ch-${d.id}"></div></div>`).join('')}</div><p class="hist-status faint" data-ref="hs"></p>`;
    const x = collect(body); m.x = x;
    m.charts = TREND_DEFS.map(d => ({ d, c: new LineChart(x['ch-' + d.id], chartOpt(d, { group: 'trend', legend: x['leg-' + d.id] })) }));
    m.keys = [...new Set(TREND_DEFS.flatMap(d => d.series.map(s => s.m)))];
    m.load = async () => {
      const range = UI.range;
      const res = await fetchSeries(m.keys, range);
      if (range !== UI.range) return;
      m.res = res; m.range = range; m.drawAt = 0; m.draw(true); m.insight();
    };
    m.draw = force => {
      if (!m.res) return;
      const now = Date.now();
      if (!force && m.range === '6h' && now - m.drawAt < 30000) return;
      if (!force && !['10m', '1h', '6h'].includes(m.range)) return;
      m.drawAt = now;
      m.charts.forEach(({ d, c }) => c.setData(d.series.map(se => seriesOf(m.res, se.m, m.range)), RANGE_SEC[m.range]));
    };
    m.insight = () => {
      const h = seriesOf(m.res, 'cpu', m.range);
      const n = m.keys.reduce((a, k) => a + seriesOf(m.res, k, m.range).length, 0);
      const last = h.length ? h[h.length - 1][0] : 0;
      if (h.length > 1) {
        let mi = 0, sum = 0; h.forEach((p, i) => { sum += p[1]; if (p[1] > h[mi][1]) mi = i; });
        const span = h[h.length - 1][0] - h[0][0];
        setHTML(m.r.dek, `${RANGE_TXT[m.range]} CPU 平均 <b>${F.int(sum / h.length)}%</b>，峰值 <b>${F.int(h[mi][1])}%</b> 出现在 ${span > 86400 ? F.mdhm(h[mi][0]) : F.hm(h[mi][0])} 前后。将指针移到任一图表上，所有图表会对齐到同一时刻。`);
      } else setHTML(m.r.dek, '历史数据还在积累中，后台会定时写入一条记录。');
      if (n) { setText(x.hs, `历史 ${n} 点 · 最近写入 ${last ? F.hm(last) : '--'} · 每 ${histIv()} 秒一条`); return; }
      setText(x.hs, '历史库为空 · 采集中…');
      API.get('/api/history/diag').then(dg => {
        if (!dg) return;
        setText(x.hs, dg.db_error ? '历史库异常：' + dg.db_error : !dg.db_exists ? '历史库未创建 · 数据目录不可写？' : !dg.rows ? `历史库为空 · 每 ${histIv()} 秒记录一条，稍后自动出现` : `历史库 ${dg.rows} 行 · 最近写入 ${dg.last_ts ? F.mdhm(dg.last_ts) : '--'}`);
        x.hs.title = '数据目录：' + (dg.data_dir || '--') + ' · 库文件：' + (dg.db_file || '--');
      }).catch(() => {});
    };
  },
  update(o, m) { if (!m.res && !m.loading) { m.loading = true; m.load().finally(() => { m.loading = false; }); return; } m.draw(false); },
});

/* 硬件实体绘制：核心点阵 + 内存条 */
const CORE_DOTS = (() => { let h = ''; for (let y = 0; y < 5; y++) for (let x = 0; x < 5; x++) h += `<circle cx="${x + 0.5}" cy="${y + 0.5}" r=".36"/>`; return h; })();
const DIMM = { x0: 12, p: 47.5, w: 42, y: 12, h: 48 };
const dimmX = f => { f = clamp(f, 0, 1); const k = Math.min(7, Math.floor(f * 8)); return DIMM.x0 + k * DIMM.p + (f * 8 - k) * DIMM.w; };
function dimmSVG() {
  let h = `<defs><pattern id="dimmHatch" width="3.2" height="3.2" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.3" height="3.2" style="fill:var(--e-teal)"/></pattern><clipPath id="dimmChips">`;
  for (let i = 0; i < 8; i++) h += `<rect x="${DIMM.x0 + i * DIMM.p}" y="${DIMM.y}" width="${DIMM.w}" height="${DIMM.h}" rx="3"/>`;
  h += `</clipPath></defs><path class="pcb" d="M7 0H393A7 7 0 0 1 400 7V100H241V92H229V100H0V7A7 7 0 0 1 7 0Z"/>`;
  for (let i = 0; i < 8; i++) h += `<rect class="chip-bg" x="${DIMM.x0 + i * DIMM.p}" y="${DIMM.y}" width="${DIMM.w}" height="${DIMM.h}" rx="3"/>`;
  h += `<g clip-path="url(#dimmChips)">${['app', 'cache', 'buf'].map(k => `<rect class="seg seg-${k}" y="${DIMM.y}" height="${DIMM.h}" x="${DIMM.x0}" width="0"/>`).join('')}</g>`;
  h += `<text x="${DIMM.x0}" y="74" data-ref="dimmT"></text><rect x="362" y="66" width="22" height="9" rx="1.5" class="chip-bg"/>`;
  for (let x = 8; x < 394; x += 5) if (x < 225 || x > 242) h += `<rect class="finger" x="${x}" y="82" width="3" height="14" rx=".8"/>`;
  return h;
}
const memItems = o => { const m = o && o.memory; return Array.isArray(m) ? m : (m && m.items) || []; };
const dimmLabel = (o) => {
  const it = memItems(o).filter(i => i.present), mem = (o && o.mem) || {};
  if (!it.length) return `${gbT(mem.total)} GB 物理内存 · 未读取到插槽信息`;
  const a = it[0];
  return [[a.type, a.speed].filter(Boolean).join('-'), a.size, [a.manufacturer, a.part].filter(Boolean).join(' '), it.length > 1 ? `× ${it.length}` : ''].filter(Boolean).join(' · ').toUpperCase();
};
const MEM_LEG = mp => [['应用', mp.app, 'var(--e-teal)'], ['缓存', mp.cache, 'hatch'], ['缓冲', mp.buf, 'var(--ink-3)'], ['空闲', mp.free, 'var(--fill-3)']];
const memLegHTML = (mp, first) => MEM_LEG(mp).map(([l, v, c], i) => `<span><i ${c === 'hatch' ? 'class="hatch"' : `style="background:${i === 0 && first ? first : c}"`}></i>${l} <b>${gb(v)} GB</b></span>`).join('');
/* 每核点阵：cores 个 5×5 点阵，自下而上点亮 */
function coreTiles(host, n) {
  host.style.setProperty('--cols', n <= 4 ? n : n <= 8 ? 4 : n <= 18 ? 6 : 8);
  host.innerHTML = Array.from({ length: n }, (_, i) => `<div class="hw-core" data-ref="core${i}"><svg viewBox="0 0 5 5" aria-hidden="true">${CORE_DOTS}</svg><div class="hw-core-f"><span>C${i}</span><b data-ref="cv${i}">--</b></div></div>`).join('');
  const r = collect(host);
  return Array.from({ length: n }, (_, i) => { const cs = $$('circle', r['core' + i]), order = []; for (let y = 4; y >= 0; y--) for (let c = 0; c < 5; c++) order.push(cs[y * 5 + c]); return { el: r['core' + i], v: r['cv' + i], order }; });
}
function setCores(tiles, pc) {
  pc.forEach((v, i) => {
    const t = tiles[i]; if (!t) return;
    const k = Math.round(clamp(v, 0, 100) / 100 * 25);
    t.order.forEach((c, j) => { const on = j < k; if (c._on !== on) { c._on = on; c.setAttribute('class', on ? 'on' : ''); } const col = on ? rampAt((Math.floor(j / 5) + 1) * 20, 'cpu') : ''; if (c._col !== col) { c._col = col; c.style.fill = col; } });
    const l = LV.cpu(v); if ((t.el.dataset.l || '') !== l) t.el.dataset.l = l;
    setText(t.v, F.int(v)); inkTo(t.v, rampInk(v, 'cpu'));
  });
}
const GPU_TAG = { igpu: '核显', nvidia: 'NVIDIA', amd: 'AMD' };
/* Intel 核显引擎（i915 PMU，原理同 intel_gpu_top）：后端只下发实际存在的引擎 */
const GPU_ENG = [['render', '渲染'], ['video', '视频'], ['video_enhance', '视频增强'], ['blitter', '复制'], ['compute', '计算']];
const GPU_DIAG = '/api/gpu?diag=1';
const gpuIntel = g => g && g.vendor === '8086';
const gpuEngs = g => GPU_ENG.filter(([k]) => g.engines && ok(g.engines[k]));
/** 核显读数状态：pmu（引擎实测）/ rc6（活跃度估算）/ warm（首轮采样）/ none（读不到） */
const gpuMode = g => !gpuIntel(g) ? 'drv' : g.warming ? 'warm' : g.util_source === 'pmu' && gpuEngs(g).length ? 'pmu' : g.util_source === 'rc6' && ok(g.util) ? 'rc6' : 'none';
const diagLink = () => `<a class="link-btn diag-link" href="${GPU_DIAG}" target="_blank" rel="noopener">${ic('info', 'i-sm')}诊断信息（反馈问题时附上）</a>`;
const GPU_NOTE = {
  pmu: 'i915 PMU 实测（与 intel_gpu_top 同源），约 10 秒均值。',
  rc6: '按 GPU 省电态（RC6）驻留时间估算整体活跃度，不区分引擎；读取各引擎占用需要 i915 PMU。',
  warm: '正在采样，约 10 秒后显示。',
  none: '未能读取核显占用（i915 PMU 与 RC6 均不可用）。',
};

/* ---------------------------------------------------------------------
   04 COMPUTE — 每核 / 内存构成 / 显卡
   --------------------------------------------------------------------- */
defineModule({
  id: 'compute', page: 'overview', kicker: 'COMPUTE', title: '处理器 · 内存 · 显卡', dek: '每核使用率、内存构成与显卡状态',
  actions: () => detailBtn('cpu'),
  build(body, m) {
    body.innerHTML = `<div class="grid g-compute">
      <div class="card pad tap" data-sheet="cpu" data-dc="cpu" tabindex="0" role="button">
        <div class="label">每核使用率<span class="faint lbl-r" data-ref="model"></span></div>
        <div class="split" style="margin-top:14px">
          <div><span class="eyebrow">整体</span><span class="big-stat dnum" style="margin-top:6px"><span class="dm" data-ref="avg"></span><span class="unit">%</span></span></div>
          <div><span class="eyebrow">频率</span><b data-ref="freq"></b></div>
          <div><span class="eyebrow">负载 1 / 5 / 15 分钟</span><b data-ref="load"></b></div>
        </div>
        <div class="hw-cores" data-ref="cores"></div>
      </div>
      <div class="card pad tap" data-sheet="mem" data-dc="mem" tabindex="0" role="button">
        <div class="label">内存构成</div>
        <div class="big-stat dnum" style="margin-top:14px"><span class="dm" data-ref="mUsed"></span><span class="unit" data-ref="mTot"></span></div>
        <svg class="hw-dimm" viewBox="0 0 400 100" data-ref="dimm" role="img" aria-label="内存条占用示意">${dimmSVG()}</svg>
        <div class="legend" data-ref="mLeg"></div>
        <hr class="hair">
        <div class="label" style="justify-content:space-between"><span>交换分区</span><span class="tnum muted" data-ref="swap"></span></div>
        <div class="bar thin" style="margin-top:10px" data-ref="swapW"><i data-ref="swapBar"></i></div>
      </div>
      <div class="card pad" data-dc="cpu" data-ref="gpuCard"></div>
    </div>`;
    m.x = collect(body); m.tiles = [];
    [m.sApp, m.sCache, m.sBuf] = $$('.seg', m.x.dimm);
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, cpu = o.cpu || {}, pc = cpu.per_core || [], hwc = ((o.hardware || {}).cpu) || {};
    setText(x.model, hwc.model || cpu.model || (o.system || {}).cpu_model || [hwc.arch, cpu.cores ? cpu.cores + ' 核' : ''].filter(Boolean).join(' · '));
    dnum(x.avg, cpu.percent); inkTo(x.avg, ok(cpu.percent) ? rampInk(cpu.percent, 'cpu') : '');
    setText(x.freq, ok(cpu.frequency_mhz) && cpu.frequency_mhz > 0 ? (cpu.frequency_mhz / 1000).toFixed(2) + ' GHz' : '--');
    setText(x.load, (cpu.load || []).map(v => F.d2(v)).join(' / ') || '--');
    if (m.tiles.length !== pc.length) { m.tiles = pc.length ? coreTiles(x.cores, pc.length) : []; if (!pc.length) x.cores.innerHTML = emptyHTML('每核数据不可用'); }
    setCores(m.tiles, pc);
    let mx = 0; pc.forEach((v, i) => { if (v > pc[mx]) mx = i; });
    const mem = o.mem || {}, mp = memParts(mem), T = +mem.total || 0;
    dnum(x.mUsed, T ? mem.used / GiB : null, v => v.toFixed(1)); setText(x.mTot, T ? `/ ${gbT(T)} GB 已用` : '');
    inkTo(x.mUsed, ok(mem.percent) ? rampInk(mem.percent, 'mem') : '');
    setText(x.dimmT || (x.dimmT = x.dimm.querySelector('[data-ref="dimmT"]')), dimmLabel(o));
    if (T) {
      const a = mp.app / T, c = mp.cache / T, b = mp.buf / T;
      const seg = (el, f0, f1) => { const x0 = dimmX(f0), x1 = dimmX(f1); el.setAttribute('x', x0.toFixed(2)); el.setAttribute('width', Math.max(0, x1 - x0).toFixed(2)); };
      seg(m.sApp, 0, a); seg(m.sCache, a, a + c); seg(m.sBuf, a + c, a + c + b);
      const mc = rampAt(mem.percent, 'mem'); if (m.sApp.style.fill !== mc) m.sApp.style.fill = mc;
      const lm = LV.mem(mem.percent); if ((x.dimm.dataset.l || '') !== lm) x.dimm.dataset.l = lm;
      setHTML(x.mLeg, memLegHTML(mp, mc));
    } else setHTML(x.mLeg, '<span>内存数据不可用</span>');
    if (+mem.swap_total > 0) { setText(x.swap, `${gb(mem.swap_used)} / ${gb(mem.swap_total)} GB`); x.swapW.hidden = false; rampBar(x.swapBar, mem.swap_used / mem.swap_total * 100, 'gen'); }
    else { setText(x.swap, '未启用'); x.swapW.hidden = true; }
    // 显卡：只展示后端真实读到的字段
    const gpus = o.gpu || [], g = gpus[0], names = ((o.hardware || {}).gpu) || [];
    const mode = g ? gpuMode(g) : '', engs = g ? gpuEngs(g) : [];
    const gsig = g ? [g.name, g.type, mode, engs.map(e => e[0]).join(), +g.mem_total > 0].join('|') : 'none' + names.join();
    if (m.gsig !== gsig) {
      m.gsig = gsig;
      const row = (k, l, cls = '') => `<div class="engine ${cls}"><span>${l}</span><div class="bar thin"><i data-ref="gb_${k}"></i></div><span class="v" data-ref="gv_${k}">--</span></div>`;
      const rows = !g ? '' : mode === 'pmu' ? engs.map(([k, l]) => row(k, l)).join('') : mode === 'rc6' ? row('util', '活跃度（RC6 估算）', 'wide')
        : mode === 'drv' ? row('util', '利用率') + (+g.mem_total > 0 ? row('mem', '显存') : '') : '';
      x.gpuCard.innerHTML = g ? `<div class="label">${GPU_TAG[g.type] || '显卡'}<span class="faint lbl-r">${esc(String(g.name || '').replace(/ \(.*\)/, ''))}</span></div>
        <div class="big-stat dnum" style="margin-top:14px"><span class="dm" data-ref="gU"></span><span class="unit">%</span><span class="sub mono" style="margin-left:8px" data-ref="gF"></span></div>
        <div style="margin-top:18px">${rows}</div><p class="sub" style="margin:14px 0 0" data-ref="gNote"></p>${mode === 'rc6' || mode === 'none' ? diagLink() : ''}`
        : `<div class="label">显卡</div>${emptyHTML('未检测到可监控的显卡', names.length ? esc(names.join('、')) + ' · 无法读取实时状态' : '支持 Intel 核显 / NVIDIA（nvidia-smi）/ AMD（amdgpu）', 'gpu')}`;
      Object.assign(x, collect(x.gpuCard));
    }
    if (g) {
      const has = g.util_avail && ok(g.util);
      dnum(x.gU, has ? g.util : null); inkTo(x.gU, has ? rampInk(g.util, 'gen') : '');
      setText(x.gF, [ok(g.freq_mhz) ? F.int(g.freq_mhz) + ' MHz' : '', ok(g.temp) ? F.int(g.temp) + '℃' : ''].filter(Boolean).join(' · '));
      const bar = (k, v) => { rampBar(x['gb_' + k], ok(v) ? clamp(+v, 0, 100) : 0, 'gen'); setText(x['gv_' + k], ok(v) ? F.int(v) + '%' : '--'); };
      if (mode === 'pmu') engs.forEach(([k]) => bar(k, g.engines[k]));
      else if (mode === 'rc6' || mode === 'drv') { bar('util', has ? g.util : null); if (+g.mem_total > 0) bar('mem', g.mem_pct); }
      setText(x.gNote, mode !== 'drv' ? GPU_NOTE[mode] : !g.util_avail ? '该显卡驱动未开放利用率读数。' : +g.mem_total > 0 ? `显存 ${F.bytesStr(g.mem_used)} / ${F.bytesStr(g.mem_total)}` : (gpus.length > 1 ? `另有 ${gpus.length - 1} 块显卡，详见处理器详情。` : ''));
    }
    setHTML(m.r.dek, `${pc.length ? `${pc.length} 个核心整体 <b>${F.int(cpu.percent)}%</b>，最忙的是 C${mx}（${F.int(pc[mx])}%）。` : ''}${T ? `内存实际占用 <b>${gb(mp.app)} GB</b>，另有 ${gb(mp.cache + mp.buf)} GB 缓存可随时释放。` : ''}`);
  },
});

/* ---------------------------------------------------------------------
   05 THERMAL — 温度 / 风扇 / 电压
   --------------------------------------------------------------------- */
const T_MIN = 20, T_MAX = 100, tPos = t => clamp((t - T_MIN) / (T_MAX - T_MIN) * 100, 0, 100);
function thermoList(o, all) {
  const L = [], ct = cpuTemp(o), mt = mbTemp(o);
  if (ct != null) L.push({ k: 'cpu', label: 'CPU', v: ct, kind: 'cpu' });
  if (mt != null) L.push({ k: 'mb', label: '主板', v: mt, kind: 'other' });
  ((o.sensors || {}).temps || []).forEach((t, i) => { if (t.type === 'disk' || !ok(t.value)) return; L.push({ k: 's' + i + t.name, label: t.name, v: +t.value, kind: t.type === 'cpu' ? 'cpu' : 'other', max: t.max, crit: t.crit }); });
  return all ? L : L.slice(0, 6);
}
const diskThermo = o => disksOf(o).filter(d => ok(d.temp)).map(d => ({ k: 'd-' + d.name, label: d.name + (isSleep(d) ? ' · 休眠' : ''), v: +d.temp, kind: 'disk', sleep: isSleep(d) }));
const tMarks = t => t.kind === 'cpu' ? [TH.tempWarn, TH.tempCrit] : t.kind === 'disk' ? [TH.diskTempWarn, TH.diskTempCrit] : [t.max, t.crit].filter(ok);
const tLv = t => t.kind === 'cpu' ? LV.temp(t.v) : t.kind === 'disk' ? LV.dtemp(t.v) : (ok(t.crit) && t.v >= t.crit ? 'crit' : ok(t.max) && t.v >= t.max ? 'warn' : '');
const tRamp = t => t.kind === 'disk' ? 'dtemp' : 'temp';
function thermoRows(list) {
  return list.map(t => `<div class="thermo-row" data-t="${esc(t.k)}"><span class="${t.sleep ? 'faint' : ''} t-l" title="${esc(t.label)}">${esc(t.label)}</span><div class="thermo">${tMarks(t).map(v => `<span class="mk" style="left:${tPos(v)}%"></span>`).join('')}<i></i></div><span class="v"></span></div>`).join('');
}
function updateThermo(root, list) {
  list.forEach(t => {
    const row = root.querySelector(`[data-t="${CSS.escape(t.k)}"]`); if (!row) return;
    const bar = row.querySelector('i'), v = row.querySelector('.v'), lv = t.sleep ? 'sleep' : tLv(t);
    setCls(bar, '', lv);
    if (t.sleep) { bar.style.width = tPos(t.v).toFixed(1) + '%'; rampOff(bar); }
    else rampBar(bar, t.v, tRamp(t), tPos);
    setHTML(v, t.sleep ? `<span class="faint" title="休眠前读数">${F.int(t.v)}℃</span>` : `${F.int(t.v)}℃`);
    inkTo(v, t.sleep ? '' : rampInk(t.v, tRamp(t)));
  });
}
const scaleHTML = `<div class="thermo-scale"><span></span><div>${[20, 40, 60, 80, 100].map(v => `<span style="left:${tPos(v)}%">${v}°</span>`).join('')}</div><span></span></div>`;
const fanName = f => String(f.name || '风扇');
const NO_FAN = '已适配标准 hwmon、IPMI/BMC、ThinkPad ACPI 与 0xA20 硬件监控邮箱（只读）；仍无数值通常表示该机型固件未向系统开放风扇数据。带 BMC 的主机可安装 ipmitool 后自动识别。';
defineModule({
  id: 'thermal', page: 'overview', dc: 'temp', kicker: 'THERMAL', title: '温度与散热', dek: '温度测点、风扇与电压',
  actions: () => detailBtn('temp'),
  build(body, m) {
    body.innerHTML = `<div class="grid g-thermal">
      <div class="card pad tap" data-sheet="temp" tabindex="0" role="button">
        <div class="label" style="margin-bottom:6px">处理器与主板<span class="faint lbl-r">竖线为警告 / 严重阈值</span></div>
        <div data-ref="tl"></div>
        <div class="label" style="margin:18px 0 6px">硬盘</div>
        <div data-ref="td"></div>
        ${scaleHTML}
      </div>
      <div class="card pad">
        <div class="label">风扇</div>
        <div data-ref="fans" style="margin-top:6px"></div>
        <div data-ref="vw"><div class="label" style="margin:18px 0 12px">电压</div><div class="volts" data-ref="volts"></div></div>
      </div>
    </div>`;
    m.x = collect(body);
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, tl = thermoList(o), td = diskThermo(o), sen = o.sensors || {}, fans = sen.fans || [], volts = sen.volts || [];
    const s1 = tl.map(t => t.k).join(), s2 = td.map(t => t.k).join();
    if (m.s1 !== s1) { m.s1 = s1; x.tl.innerHTML = tl.length ? thermoRows(tl) : emptyHTML('未读取到 CPU / 主板温度', 'fnOS 默认未安装 lm-sensors，已直读 thermal zone 与 hwmon；虚拟机或容器内通常无法访问硬件传感器。'); }
    if (m.s2 !== s2) { m.s2 = s2; x.td.innerHTML = td.length ? thermoRows(td) : emptyHTML('没有可用的硬盘温度', disksOf(o).length ? '硬盘未提供温度（需要 drivetemp 或 smartctl）' : '未检测到物理硬盘'); }
    updateThermo(x.tl, tl); updateThermo(x.td, td);
    const fsig = fans.map(fanName).join();
    if (m.fsig !== fsig) { m.fsig = fsig; x.fans.innerHTML = fans.length ? fans.map((f, i) => `<div class="fan"><span class="fan-ico"><svg class="i" data-ref="fi${i}"><use href="#i-fan"/></svg></span><div style="flex:1;min-width:0"><div class="sub">${esc(fanName(f))}</div><div class="num"><span data-ref="fv${i}">--</span><span class="unit" style="font-size:12px">RPM</span></div></div></div>`).join('') : emptyHTML('未检测到可读取的风扇传感器', esc(NO_FAN), 'fan'); Object.assign(x, collect(x.fans)); }
    fans.forEach((f, i) => { num(x['fv' + i], f.rpm); if (x['fi' + i]) { x['fi' + i].style.setProperty('--spin', (2400 / Math.max(300, +f.rpm || 300)).toFixed(2) + 's'); x['fi' + i].style.animationPlayState = +f.rpm > 0 ? '' : 'paused'; } });
    x.vw.hidden = !volts.length;
    const vsig = volts.map(v => v.name).join();
    if (m.vsig !== vsig) { m.vsig = vsig; x.volts.innerHTML = volts.slice(0, 8).map((v, i) => `<div class="volt"><div class="eyebrow">${esc(v.name)}</div><div class="num"><span data-ref="vv${i}">--</span><span class="unit" style="font-size:12px">V</span></div></div>`).join(''); Object.assign(x, collect(x.volts)); }
    volts.slice(0, 8).forEach((v, i) => num(x['vv' + i], v.value, z => (+z).toFixed(2)));
    const ct = cpuTemp(o), lt = LV.temp(ct), hot = td.filter(t => !t.sleep).sort((a, b) => b.v - a.v)[0];
    const fanAvg = fans.filter(f => +f.rpm > 0); const fa = fanAvg.length ? fanAvg.reduce((a, f) => a + +f.rpm, 0) / fanAvg.length : null;
    setHTML(m.r.dek, lt ? `<span class="warn">CPU ${F.int(ct)}℃，温度${lt === 'crit' ? '过高' : '偏高'}</span>，建议检查风道与灰尘。`
      : !tl.length && !td.length ? '未读取到任何温度传感器。'
        : [ct != null ? `CPU <b>${F.int(ct)}℃</b>` : '', hot ? `硬盘最高 ${F.int(hot.v)}℃（${esc(hot.label)}）` : '', fa != null ? `风扇平均 ${F.int(fa)} RPM` : ''].filter(Boolean).join('，') + '。');
  },
});


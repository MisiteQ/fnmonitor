/* ---------------------------------------------------------------------
   06 ENERGY — /api/power（实时）+ /api/power_stats（kWh / 电费）+ 功率历史积分
   --------------------------------------------------------------------- */
const DAY_BARS = 30;
/** 每日柱：以真实历史积分；只显示有数据覆盖的天数（最多 30 天） */
function dailyRows(daysMap, keys, firstTs) {
  const firstDay = firstTs ? new Date(firstTs * 1000) : new Date();
  const n = clamp(Math.floor((Date.now() - new Date(firstDay.getFullYear(), firstDay.getMonth(), firstDay.getDate()).getTime()) / 864e5) + 1, 1, DAY_BARS);
  const ds = lastDays(n);
  return { days: ds, rows: ds.map((d, i) => Object.assign({ dim: i !== ds.length - 1 }, ...keys.map(k => ({ [k]: (daysMap[k][dayKey(d)] || 0) })))) };
}
/** /api/daily：后端按本地自然日汇总（不受历史保留天数限制，今天含实时尾段）。
    旧后端没有该接口时返回 null，各图回退为前端按历史积分 */
const Daily = {
  at: 0, data: null, p: null,
  get(maxAge = 300000) {
    if (Date.now() - this.at < maxAge) return Promise.resolve(this.data);
    if (this.p) return this.p;
    this.p = API.get('/api/daily?days=' + DAY_BARS).then(j => (j && Array.isArray(j.days) && j.days.length ? j : null)).catch(() => null)
      .then(j => { this.data = j; this.at = Date.now(); this.p = null; return j; });
    return this.p;
  },
};
/** 每日汇总 → 柱图行：从第一天有数据起到今天；中间没数据的日子保留空位（null，不画柱） */
function dailyFromApi(j, pick) {
  const ds = j.days, first = ds.findIndex(d => d.has_data && pick(d));
  if (first < 0) return null;
  const list = ds.slice(first), days = list.map(d => { const [y, mo, da] = d.day.split('-').map(Number); return new Date(y, mo - 1, da); });
  return { list, days, rows: list.map((d, i) => Object.assign({ dim: i !== list.length - 1 }, pick(d) || {})) };
}
const nul = (v, f) => v == null ? '无数据' : f(v);
/** 平均功率（本月预计 / 全年估算共用）：优先每日汇总的覆盖时长加权均值，其次功率历史均值，再次当前功率 */
function avgW(o) {
  const ds = ((Daily.data || {}).days || []).filter(d => ok(d.kwh) && +d.power_h > 0);
  const h = ds.reduce((a, d) => a + +d.power_h, 0);
  if (h >= 1) return { w: ds.reduce((a, d) => a + +d.kwh, 0) * 1000 / h, basis: `近 ${ds.length} 天平均`, n: ds.length };
  const hp = Hist.peek('power', 'all'), pts = (hp && hp.points) || [];
  if (pts.length > 5) return { w: pts.reduce((a, p) => a + (+p[1] || 0), 0) / pts.length, basis: '功率历史平均' };
  return pwrOk(o) ? { w: +o.power.total, basis: '当前功率' } : null;
}
const barOpts = (days, series, fmt, h) => ({
  height: h, series, fmt,
  title: i => days[i] ? (days[i].getMonth() + 1) + '/' + days[i].getDate() + (i === days.length - 1 ? ' · 今天' : '') : '',
  xLabel: i => { const n = days.length, step = n > 14 ? 7 : n > 7 ? 3 : 1; return i === n - 1 ? '今天' : ((n - 1 - i) % step === 0 && days[i] ? (days[i].getMonth() + 1) + '/' + days[i].getDate() : null); },
});
function powerParts(p) {
  // iDRAC 给的是整机电源输出，拆不出 CPU/内存/硬盘，直接一条整机实测
  if (p.source === 'idrac') return [['整机实测', +p.total || 0, 'var(--d-p)']];
  const parts = [];
  if (ok(p.package)) parts.push(['CPU 封装', +p.package, 'var(--d-p)']);
  if (ok(p.dram)) parts.push(['内存', +p.dram, 'color-mix(in srgb, var(--d-p) 55%, var(--card))']);
  if (ok(p.disks_w)) parts.push([`硬盘${ok(p.disks_active) ? `（${p.disks_active} 块在转）` : ''}`, +p.disks_w, 'hatch']);
  if (ok(p.base)) parts.push(['主板与其他', +p.base, 'color-mix(in srgb, var(--d-p) 25%, var(--card))']);
  const rest = (+p.total || 0) - parts.reduce((a, x) => a + x[1], 0);
  if (rest > 0.05) parts.push([p.source === 'estimated' ? 'CPU 与网卡等（估算）' : '其他', rest, 'var(--fill-3)']);
  return parts;
}
const ppI = c => c === 'hatch' ? '<i class="hatch"></i>' : `<i style="background:${c}"></i>`;
const SRC_TXT = { idrac: 'iDRAC 实测', rapl: '硬件 RAPL', estimated: '模型估算' };
/** 本月预计：已用电量 + 剩余时长 × 平均功率 */
const monthProj = (mk, o) => { const a = avgW(o); if (!ok(mk) || !a) return null; const d = new Date(), end = new Date(d.getFullYear(), d.getMonth() + 1, 1); return { kwh: mk + a.w * (end - d) / 3.6e9, w: a.w, basis: a.basis }; };
/** 全年估算：平均功率 × 8760 小时（只是估算，口径写在副标题里） */
const yearProj = o => { const a = avgW(o); return a ? { kwh: a.w * 8.76, w: a.w, basis: a.basis } : null; };
defineModule({
  id: 'energy', page: 'overview', dc: 'pwr', kicker: 'ENERGY', title: '能耗', dek: '实时功耗、用电量与电费',
  actions: () => detailBtn('power'),
  build(body, m) {
    body.innerHTML = `<div class="energy">
      <div class="card pad energy-live tap" data-sheet="power" tabindex="0" role="button">
        <div class="label">实时功耗<span class="pill" style="margin-left:auto" data-ref="src"></span></div>
        <div class="dnum huge" style="margin-top:18px"><span class="dm" data-ref="w"></span><span class="unit">W</span></div>
        <div class="sub" style="margin-top:6px" data-ref="wSub"></div>
        <div class="stack" style="margin-top:22px;height:12px" data-ref="stack"></div>
        <div class="legend" data-ref="leg"></div>
      </div>
      <div class="stat-cells">
        <div class="stat-cell"><div class="eyebrow">今日用电</div><div class="dnum"><span class="dm" data-ref="tk"></span><span class="unit">kWh</span></div><div class="sub" data-ref="tc"></div></div>
        <div class="stat-cell"><div class="eyebrow">本月用电</div><div class="dnum"><span class="dm" data-ref="mk"></span><span class="unit">kWh</span></div><div class="sub" data-ref="mc"></div></div>
        <div class="stat-cell"><div class="eyebrow">本月预计电费</div><div class="dnum"><span class="dm" data-ref="pc"></span></div><div class="sub" data-ref="pk"></div></div>
        <div class="stat-cell"><div class="eyebrow">全年估算<span class="pill est-pill">估算</span></div><div class="dnum"><span class="dm" data-ref="yc"></span></div><div class="sub" data-ref="yk"></div></div>
      </div>
    </div>
    <div class="card chart-card" style="margin-top:16px">
      <div class="chart-head"><div class="chart-title" data-ref="barT">每日用电</div><div class="chart-legend"><span><span class="lk"><i class="kb0" style="--k:var(--e-teal)"></i><span data-ref="barL">每日 kWh · 按功率历史积分</span></span></span></div></div>
      <div data-ref="bars"></div><div class="chart-empty-s" data-ref="barE" hidden></div>
    </div>`;
    m.x = collect(body);
    const SER = [{ key: 'kwh', label: '用电', color: 'var(--e-teal)' }], fmt = v => nul(v, x => F.kwh(x) + ' kWh');
    const show = (days, rows, title, legend, extra) => {
      if (!m.bars) m.bars = new BarChart(m.x.bars, barOpts(days, SER, fmt, 132));
      Object.assign(m.bars.opt, barOpts(days, SER, fmt, 132));
      if (extra) { const t0 = m.bars.opt.title; m.bars.opt.title = i => t0(i) + extra(i); }
      const any = !!rows.length; m.x.barE.hidden = any; m.x.bars.hidden = !any;
      setText(m.x.barT, any ? title(days.length) : '近 30 天用电'); setText(m.x.barL, legend);
      if (any) m.bars.setData(rows);
    };
    m.loadBars = async () => {
      const j = await Daily.get(), r = j && dailyFromApi(j, d => ok(d.kwh) ? { kwh: +d.kwh } : null);
      setText(m.x.barE, '暂无功率数据：采集满一段时间后这里会显示每天的用电量');
      if (j) {
        if (!r) return show([], [], () => '', '每日 kWh');
        // 提示里带上电费与数据覆盖时长（不足一整天的柱子是「部分日」，不是用电少）
        return show(r.days, r.rows, n => `近 ${n} 天用电`, '每日 kWh · 功率历史积分', i => { const d = r.list[i]; if (!d) return ''; const part = ok(d.power_h) && d.power_h < 23.5 ? ` · 数据 ${F.d1(d.power_h)} 小时` : ''; return (ok(d.cost) ? ' · ' + F.yuan(d.cost) : '') + part; });
      }
      // 旧后端：前端按功率历史积分（只覆盖历史保留天数）
      const d = await Hist.get('power', 'all', 600000).catch(() => null), pts = (d && d.points) || [];
      const map = { kwh: dailyIntegral(pts, 1 / 3.6e6) }, { days, rows } = dailyRows(map, ['kwh'], pts.length ? pts[0][0] : 0);
      show(pts.length > 1 ? days : [], pts.length > 1 ? rows : [], n => `近 ${n} 天用电`, '每日 kWh · 按功率历史积分');
    };
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, p = o.power || {}, has = pwrOk(o), ps = S.pstats, rate = ps && ps.cost ? ps.cost.rate_yuan_per_kwh : (S.cfg && S.cfg.power_rate_yuan);
    dnum(x.w, has ? +p.total : null, F.d1);
    setText(x.src, has ? (SRC_TXT[p.source] || '来源未知') : '不可用');
    setText(x.wSub, !has ? '无法读取 RAPL，也无法估算整机功耗'
      : p.source === 'idrac' ? `iDRAC 实测整机输出${ok(p.capacity) ? ` · 电源容量 ${F.int(p.capacity)} W` : ''}${ok(p.min) && ok(p.max) ? ` · 近 1 分钟 ${F.int(p.min)}–${F.int(p.max)} W` : ''}`
        : p.source === 'rapl' ? '整机 = CPU 封装（RAPL 实测）+ 内存 + 主板底座 + 活动硬盘' : '无 RAPL：按 TDP × CPU 占用 + 硬盘 + 网卡 + 主板底座估算');
    const parts = has ? powerParts(p) : [], sig = parts.map(q => q[0]).join();
    if (m.psig !== sig) { m.psig = sig; x.stack.innerHTML = parts.map(q => ppI(q[2])).join(''); }
    parts.forEach((q, i) => { if (x.stack.children[i]) x.stack.children[i].style.flexGrow = q[1].toFixed(2); });
    setHTML(x.leg, parts.map(([l, v, c]) => `<span>${ppI(c)}${esc(l)} <b>${F.d1(v)} W</b></span>`).join(''));
    const en = ps && ps.energy, co = ps && ps.cost;
    dnum(x.tk, en ? en.today_kwh : null, F.kwh); setText(x.tc, co ? '电费 ' + F.yuan(co.today_yuan) : '--');
    dnum(x.mk, en ? en.month_kwh : null, F.kwh); setText(x.mc, co ? '电费 ' + F.yuan(co.month_yuan) : '--');
    const pj = en ? monthProj(en.month_kwh, o) : null, pk = pj ? pj.kwh : null;
    if (x.pc) dot(x.pc).set(pk != null && ok(rate) ? F.yuan(pk * rate) : '--'); setText(x.pk, pk != null ? `约 ${F.kwh(pk)} kWh · 剩余天数按${pj.basis} ${F.d1(pj.w)} W` : '本月数据不足');
    const yp = has || Daily.data ? yearProj(o) : null;
    if (x.yc) dot(x.yc).set(yp && ok(rate) ? F.yuan(yp.kwh * rate) : '--');
    setText(x.yk, yp ? `约 ${F.int(yp.kwh)} kWh · ${yp.basis} ${F.d1(yp.w)} W × 8760 小时` : '暂无可用的功率数据');
    const sl = disksOf(o).filter(isSleep).length, dw = S.cfg && S.cfg.power_disk_typical_w;
    setHTML(m.r.dek, !has ? '功耗不可用。' : `此刻 <b>${F.d1(p.total)} W</b>${ok(rate) ? `，按电价 ${esc(rate)} 元/kWh` : ''}${pk != null && ok(rate) ? `，本月电费预计 <b>${F.yuan(pk * rate)}</b>` : ''}。${sl ? `${sl} 块硬盘休眠中${ok(dw) ? `，每块约省 ${esc(dw)} W` : ''}。` : ''}`);
    if (!m.barsAt || Date.now() - m.barsAt > 600000) { m.barsAt = Date.now(); m.loadBars(); }
  },
});

/* ---------------------------------------------------------------------
   07 NETWORK — 网口实时速率 + /api/traffic 统计 + 速率历史积分
   --------------------------------------------------------------------- */
const ipOf = (o, iface) => { const r = ((o.system || {}).ips || []).find(p => p.iface === iface); return r ? (r.ipv4 || [])[0] || (r.ipv6 || [])[0] || '' : ''; };
const speedOf = (o, iface) => { const r = (((o.hardware || {}).net) || []).find(p => p.name === iface); return r && +r.speed_mbps > 0 ? +r.speed_mbps : 0; };
const speedTxt = mb => mb >= 1000 ? (mb / 1000 >= 10 ? Math.round(mb / 1000) : +(mb / 1000).toFixed(1)) + ' GbE' : mb + ' Mb/s';
function rankHTML(cs, lim) {
  const top = cs.slice(0, lim), max = Math.max(1, ...top.map(c => +c.total_bytes || 0));
  return top.map(c => `<div class="rank-row"><span class="n" title="${esc(c.name)}">${esc(c.name)}</span><div class="stack" style="width:${Math.max(2, (+c.total_bytes || 0) / max * 100).toFixed(1)}%"><i style="flex-grow:${+c.in_bytes || 0};background:var(--e-blue)"></i><i style="flex-grow:${+c.out_bytes || 0};background:var(--e-coral)"></i></div><span class="v">${F.bytesStr(c.total_bytes)}</span></div>`).join('');
}
defineModule({
  id: 'network', page: 'overview', dc: 'io', kicker: 'NETWORK', title: '网络与流量', dek: '网口速率、流量统计与容器流量',
  actions: () => detailBtn('net'),
  build(body, m) {
    body.innerHTML = `<div class="grid g-2">
      <div class="card pad tap" data-sheet="net" tabindex="0" role="button">
        <div data-ref="ifs"></div>
        <div class="split" style="margin-top:6px;border-top:1px solid var(--line);padding-top:16px">
          <div><span class="eyebrow">今日</span><b data-ref="today">--</b></div>
          <div><span class="eyebrow">本月</span><b data-ref="month">--</b></div>
          <div><span class="eyebrow">开机以来</span><b data-ref="boot">--</b></div>
        </div>
      </div>
      <div class="card pad">
        <div class="label" style="margin-bottom:8px">容器流量排行<span class="faint lbl-r">累计 · 自容器启动</span></div>
        <div data-ref="rank"></div>
        <div class="legend"><span><i style="background:var(--e-blue)"></i>流入</span><span><i style="background:var(--e-coral)"></i>流出</span></div>
      </div>
    </div>
    <div class="card chart-card" style="margin-top:16px">
      <div class="chart-head"><div class="chart-title" data-ref="barT">每日流量</div><div class="chart-legend"><span><span class="lk"><i class="kb0" style="--k:var(--e-blue)"></i>下行</span></span><span><span class="lk"><i class="kb1" style="--k:var(--e-coral)"></i>上行</span></span></div></div>
      <div data-ref="bars"></div><div class="chart-empty-s" data-ref="barE" hidden></div>
    </div>`;
    m.x = collect(body);
    const SER = [{ key: 'rx', label: '下行', color: 'var(--e-blue)' }, { key: 'tx', label: '上行', color: 'var(--e-coral)' }], fmt = v => nul(v, F.bytesStr);
    const show = (days, rows, title) => {
      m.daily = rows;
      if (!m.bars) m.bars = new BarChart(m.x.bars, barOpts(days, SER, fmt, 140));
      Object.assign(m.bars.opt, barOpts(days, SER, fmt, 140));
      const any = !!rows.length; m.x.barE.hidden = any; m.x.bars.hidden = !any;
      setText(m.x.barT, any ? title : '近 30 天流量');
      if (any) m.bars.setData(rows);
    };
    m.loadBars = async () => {
      const j = await Daily.get(), r = j && dailyFromApi(j, d => ok(d.rx) ? { rx: +d.rx, tx: +d.tx || 0 } : null);
      setText(m.x.barE, '暂无流量数据：采集满一段时间后这里会显示每天的上下行流量');
      if (j) return r ? show(r.days, r.rows, `近 ${r.days.length} 天流量 · ${j.exclude_bridge ? '物理网口' : '全部网口（含 Docker 网桥）'}，按网口计数器`) : show([], [], '');
      // 旧后端：前端按速率历史积分（只覆盖历史保留天数）
      const d = await Hist.get('net', 'all', 600000).catch(() => null), rx = (d && d.rx) || [], tx = (d && d.tx) || [];
      const map = { rx: dailyIntegral(rx, 1), tx: dailyIntegral(tx, 1) }, { days, rows } = dailyRows(map, ['rx', 'tx'], rx.length ? rx[0][0] : 0);
      show(rx.length > 1 ? days : [], rx.length > 1 ? rows : [], `近 ${days.length} 天流量 · 物理网口，按速率历史积分`);
    };
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, ni = nets(o), sig = ni.map(n => n.iface).join();
    if (m.sig !== sig) {
      m.sig = sig;
      x.ifs.innerHTML = ni.length ? ni.map((n, i) => `<div class="row-i if-row" style="grid-template-columns:1fr auto;padding:${i ? '16px' : '0'} 0 16px">
          <div style="min-width:0"><div class="label" style="color:var(--ink-1)">${esc(n.iface)}<span class="pill" data-ref="sp${i}" hidden></span><span class="faint tnum" style="font-weight:400">${esc(ipOf(o, n.iface))}</span></div>
            <div class="bar thin" style="margin-top:12px;width:100%" data-ref="uw${i}"><i data-ref="u${i}"></i></div></div>
          <div style="text-align:right;min-width:150px" class="tnum"><div>↓ <b data-ref="rx${i}"></b></div><div class="muted"><span class="faint">↑</span> <span data-ref="tx${i}"></span></div></div>
        </div>`).join('') : emptyHTML('没有检测到物理网口');
      Object.assign(x, collect(x.ifs));
    }
    ni.forEach((n, i) => {
      tween(x['rx' + i], +n.rx_rate || 0, v => setText(x['rx' + i], v == null ? '--' : F.rateStr(v)));
      tween(x['tx' + i], +n.tx_rate || 0, v => setText(x['tx' + i], v == null ? '--' : F.rateStr(v)));
      const sp = speedOf(o, n.iface);
      x['sp' + i].hidden = !sp; setText(x['sp' + i], sp ? speedTxt(sp) : '');
      x['uw' + i].hidden = !sp; if (sp) rampBar(x['u' + i], Math.max(+n.rx_rate || 0, +n.tx_rate || 0) / (sp * 125000) * 100, 'gen');
    });
    const t = S.traffic, tt = t && t.totals;
    setText(x.today, tt ? `↓ ${F.bytesStr(tt.today_rx)}  ↑ ${F.bytesStr(tt.today_tx)}` : '--');
    setText(x.month, tt ? `↓ ${F.bytesStr(tt.month_rx)}  ↑ ${F.bytesStr(tt.month_tx)}` : '--');
    setText(x.boot, tt ? `↓ ${F.bytesStr(tt.boot_rx)}  ↑ ${F.bytesStr(tt.boot_tx)}` : '--');
    const cs = (t && t.containers) || [], dk = S.docker || o.docker;
    setHTML(x.rank, cs.length ? rankHTML(cs, 6) : emptyHTML(dockerOk(dk) ? '没有运行中的容器' : 'Docker 不可用', dockerOk(dk) ? '' : esc((dk && dk.error) || ''), 'box'));
    if (!m.barsAt || Date.now() - m.barsAt > 600000) { m.barsAt = Date.now(); m.loadBars(); }
    let dek = tt ? `今天下载 <b>${F.bytesStr(tt.today_rx)}</b>、上传 ${F.bytesStr(tt.today_tx)}` : '流量统计加载中';
    const prev = (m.daily || []).slice(0, -1).filter(r => +r.rx > 0);
    if (tt && prev.length >= 2) { const avg = prev.reduce((a, r) => a + r.rx, 0) / prev.length, diff = avg ? (tt.today_rx - avg) / avg * 100 : 0; if (avg) dek += `，${diff >= 0 ? '高于' : '低于'} ${prev.length} 天日均 ${F.int(Math.abs(diff))}%`; }
    dek += '。';
    if (cs.length) { const total = cs.reduce((a, c) => a + (+c.total_bytes || 0), 0); if (total) dek += `${esc(cs[0].name)} 是累计流量最大的容器，占 ${F.int(cs[0].total_bytes / total * 100)}%。`; }
    setHTML(m.r.dek, dek);
  },
});

/* ---------------------------------------------------------------------
   08 PROCESSES — /api/processes（CPU 占用前 20）
   --------------------------------------------------------------------- */
defineModule({
  id: 'procs', page: 'overview', dc: 'cpu', kicker: 'PROCESSES', title: '进程', dek: '按 CPU 占用排序的进程',
  actions: m => { const b = mount(`<button class="link-btn" type="button">展开全部${ic('chev', 'i-sm')}</button>`); b.onclick = () => { m.all = !m.all; b.firstChild.textContent = m.all ? '收起' : '展开全部'; runUpdate(m); }; return b; },
  build(body, m) {
    body.innerHTML = `<div class="card pad" style="overflow-x:auto"><table class="tbl"><thead><tr><th>进程</th><th class="hide-m">PID</th><th class="hide-m">用户</th><th class="r">CPU</th><th class="r">内存</th><th class="r hide-m">常驻</th></tr></thead><tbody data-ref="tb">${Array.from({ length: 5 }, (_, i) => `<tr><td colspan="6"><div class="skel" style="height:14px;width:${[62, 48, 70, 40, 56][i]}%"></div></td></tr>`).join('')}</tbody></table><div data-ref="empty"></div></div>`;
    m.x = collect(body); m.rows = {};
    setText(m.r.dek, '正在读取进程列表…');
  },
  update(o, m) {
    const p = S.procs; if (!p) return;
    const list = (p.processes || []).slice(0, m.all ? 20 : 10), tb = m.x.tb;
    if (!m.cleared) { m.cleared = true; tb.innerHTML = ''; }
    setHTML(m.x.empty, list.length ? '' : emptyHTML('暂无进程数据', '需要系统 ps 命令（procps）'));
    list.forEach(pr => {
      const key = pr.pid + ':' + pr.comm; let row = m.rows[key];
      if (!row) {
        row = mount(`<table><tr><td class="proc-n"><b>${esc(pr.comm)}</b><div class="cmd" title="${esc(pr.args)}">${esc(pr.args)}</div></td><td class="hide-m faint">${esc(pr.pid)}</td><td class="hide-m muted">${esc(pr.user)}</td><td class="r"><div class="cell-bar"><div class="bar"><i data-ref="b"></i></div><b data-ref="c" style="min-width:52px;text-align:right"></b></div></td><td class="r" data-ref="m"></td><td class="r hide-m muted" data-ref="rss"></td></tr></table>`).querySelector('tr');
        row._r = collect(row); m.rows[key] = row;
      }
      tb.append(row);
      setText(row._r.c, F.d1(pr.cpu) + '%'); inkTo(row._r.c, rampInk(+pr.cpu || 0, 'cpu'));
      rampBar(row._r.b, clamp(+pr.cpu || 0, 0, 100), 'cpu');
      setText(row._r.m, F.d1(pr.mem) + '%'); setText(row._r.rss, F.bytesStr(pr.rss));
    });
    const keep = new Set(list.map(pr => pr.pid + ':' + pr.comm));
    Object.keys(m.rows).forEach(k => { if (!keep.has(k)) { m.rows[k].remove(); delete m.rows[k]; } });
    const [a, b] = list;
    setHTML(m.r.dek, !a ? '没有读取到进程。' : a.cpu < 10 ? '系统很安静，没有进程占用超过 10% 的 CPU。按 CPU 占用排序，每 15 秒刷新。'
      : `<b>${esc(a.comm)}</b>${b ? ` 与 <b>${esc(b.comm)}</b>` : ''} 最活跃。按 CPU 占用排序，每 15 秒刷新。`);
  },
});

/* ---------------------------------------------------------------------
   09 MONTHLY — /api/monthly_summary（设置中开启后显示）
   --------------------------------------------------------------------- */
const CN_M = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二'];
defineModule({
  id: 'monthly', page: 'overview', kicker: 'LAST MONTH', title: '上月汇总', dek: '上个月的运行回顾',
  build(body, m) { m.x = { body }; },
  update(o, m) {
    const j = S.monthly; if (!j) return;
    const sig = JSON.stringify(j); if (m.sig === sig) return; m.sig = sig;
    const body = m.x.body;
    if (!j.available) { body.innerHTML = `<div class="card pad">${emptyHTML('暂无汇总数据', esc(j.note || '等待后台采集归档（每小时清理历史时自动归档）'), 'calendar')}</div>`; setText(m.r.dek, '每月 1 日起自动汇总上个月的运行数据。'); return; }
    const cell = (l, a, b) => `<div class="stat-cell"><div class="eyebrow">${l}</div>${a}<div class="sub">${b}</div></div>`, cells = [];
    if (j.cpu) cells.push(cell('CPU 平均', dmHTML(F.int(j.cpu.avg), '%'), '峰值 ' + F.int(j.cpu.max) + '%'));
    if (j.mem) cells.push(cell('内存平均', dmHTML(F.int(j.mem.avg), '%'), '峰值 ' + F.int(j.mem.max) + '%'));
    if (j.disk) cells.push(cell('存储平均', dmHTML(F.int(j.disk.avg), '%'), '峰值 ' + F.int(j.disk.max) + '%'));
    if (j.temp) cells.push(cell('CPU 温度', dmHTML(F.int(j.temp.avg), '℃'), '峰值 ' + F.int(j.temp.max) + '℃'));
    if (j.net_rx != null || j.net_tx != null) { const b = F.bytes(j.net_rx || 0); cells.push(cell('下行', dmHTML(b[0], b[1]), '上行 ' + F.bytesStr(j.net_tx || 0))); }
    if (j.power) cells.push(cell('用电', dmHTML(F.kwh(j.power.kwh), 'kWh'), `电费 ${F.yuan(j.power.yuan)} · 均 ${F.d1(j.power.avg_w)} W`));
    if (j.load1) cells.push(cell('负载均值', dmHTML(F.d2(j.load1.avg)), '峰值 ' + F.d2(j.load1.max)));
    if (j.fan) cells.push(cell('风扇均速', dmHTML(F.int(j.fan.avg), 'RPM'), '月平均'));
    const mm = +j.month, cn = CN_M[mm - 1] || mm;
    const lead = [j.cpu ? `CPU 平均 ${F.int(j.cpu.avg)}%、峰值 ${F.int(j.cpu.max)}%` : '', j.power ? `整月用电 ${F.kwh(j.power.kwh)} kWh（约 ${F.yuan(j.power.yuan)}）` : '', (j.net_rx || j.net_tx) ? `共收发 ${F.bytesStr((j.net_rx || 0) + (j.net_tx || 0))}` : ''].filter(Boolean);
    body.innerHTML = `<div class="grid g-monthly">
      <div class="card pad" style="display:flex;flex-direction:column;justify-content:space-between;gap:24px">
        <div><div class="eyebrow">${esc(j.year)} 年 ${esc(mm)} 月 · 回顾</div>
        <p class="monthly-lead">${esc(cn)}月${lead.length ? '，' + esc(lead.join('，')) : '的汇总数据'}。</p></div>
        <p class="sub" style="margin:0">${esc(j.note || '数据来自月度归档，与历史保留天数无关。')}</p>
      </div>
      <div class="stat-cells" style="grid-template-columns:repeat(${cells.length > 4 ? 3 : 2},1fr)">${cells.join('') || cell('—', '', '暂无可用指标')}</div>
    </div>`;
    dmScan(body);
    setHTML(m.r.dek, `${esc(j.year)} 年 ${esc(mm)} 月的运行回顾。每月自动生成，可在设置中关闭。`);
  },
});

/* ---------------------------------------------------------------------
   10 HOST — 系统 / 硬件 / 网口 / 内存插槽
   --------------------------------------------------------------------- */
const kvHTML = rows => `<div class="card pad kv-card"><dl class="kv">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl></div>`;
const kvBare = rows => `<dl class="kv">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>`;
const dash = v => v == null || v === '' ? '<span class="faint">—</span>' : esc(v);
function hostRows(o) {
  const sy = o.system || {}, hw = o.hardware || {}, hc = hw.cpu || {}, b = hw.board || {}, cpu = o.cpu || {};
  const sys = [['主机名', dash(sy.hostname)], ...(sy.fnos_version ? [['系统', esc('fnOS ' + sy.fnos_version)]] : []), ['内核', `<span class="mono">${dash(sy.kernel)}</span>`], ['架构', dash(sy.arch || hc.arch)], ['已运行', `<span data-ref="up">${esc(F.dur(o.uptime))}</span>`]];
  const hwr = [['处理器', dash(hc.model || cpu.model || sy.cpu_model)], ['核心 / 线程', `${dash(hc.cores || cpu.cores)} / ${dash(hc.threads || cpu.threads)}`],
    ['主板', dash([b.sys_vendor || b.board_vendor, b.board_name || b.product_name].filter(Boolean).join(' ') || null)], ['BIOS', dash([b.bios_vendor, b.bios_version, b.bios_date].filter(Boolean).join(' ') || null)],
    ['显卡', dash((hw.gpu || []).join('、') || null)]];
  return { sys, hwr };
}
defineModule({
  id: 'host', page: 'overview', kicker: 'HOST', title: '主机', dek: '系统、硬件与网络信息',
  build(body, m) { body.innerHTML = `<div class="grid g-2"><div data-ref="a"></div><div data-ref="b"></div></div><div data-ref="c" style="margin-top:16px"></div>`; m.x = collect(body); },
  update(o, m) {
    if (!o) return;
    const { sys, hwr } = hostRows(o), sy = o.system || {};
    const ips = (sy.ips || []).map(p => [esc(p.iface), `<span class="mono">${esc((p.ipv4 || []).concat(p.ipv6 || []).join(' · ') || '—')}</span>${speedOf(o, p.iface) ? ' · ' + speedTxt(speedOf(o, p.iface)) : ''}`]);
    const items = memItems(o), mi = (o.memory && !Array.isArray(o.memory)) ? o.memory : {};
    const sigA = JSON.stringify([sys.slice(0, -1), hwr, ips, items]);   // 末行「已运行」每秒变化，单独刷新
    if (m.sigA !== sigA) {
      m.sigA = sigA;
      m.x.a.innerHTML = kvHTML(sys); m.x.b.innerHTML = kvHTML(hwr.concat(ips.length ? ips : [['网络', '<span class="faint">未读取到 IP</span>']]));
      m.x.c.innerHTML = items.length ? `<div class="card pad"><div class="label">内存插槽<span class="pill">${mi.dual ? '双通道' : '单通道 / 未知'}</span></div><div class="dimm-grid">${items.map(it => `<div class="dimm ${it.present ? 'fill' : 'empty'}"><div class="d-loc">${esc(it.locator || '插槽')}</div><div class="d-size">${it.present ? esc(it.size || '--') : '空'}</div>${it.present && it.manufacturer ? `<div class="d-mfr">${esc(it.manufacturer)}</div>` : ''}${it.present && (it.speed || it.type) ? `<div class="d-spec">${esc([it.type, it.speed].filter(Boolean).join(' · '))}</div>` : ''}${it.present && it.part ? `<div class="d-spec mono">${esc(it.part)}</div>` : ''}</div>`).join('')}</div></div>` : '';
      m.up = m.x.a.querySelector('[data-ref="up"]');
    }
    setText(m.up, F.dur(o.uptime));
    setHTML(m.r.dek, `${sy.fnos_version ? esc('fnOS ' + sy.fnos_version) + ' · ' : ''}内核 ${esc(sy.kernel || '--')}`);
  },
});


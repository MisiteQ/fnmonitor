
/* =====================================================================
   SHEETS — 渐进披露：深度信息都在右侧抽屉（移动端全屏），URL hash 路由
   ===================================================================== */
const sec = (t, extra = '') => `<div class="sheet-sec"><span>${t}</span>${extra}</div>`;
const rowsCard = inner => `<div class="card pad" style="padding-top:6px;padding-bottom:6px"><div class="rows">${inner}</div></div>`;
/** 抽屉内图表：一个范围切换控制该抽屉内全部图表，图表间十字线同步 */
function sheetCharts(ctx, body, defs) {
  const wrap = mount(`<div><div class="sheet-seg" data-ref="cseg"></div><div data-ref="clist" style="display:grid;gap:12px"></div></div>`);
  body.append(wrap);
  const charts = defs.map(d => {
    const card = mount(`<div class="card chart-card"><div class="chart-head"><div class="chart-title">${d.title}</div><div class="chart-legend" data-ref="cleg"></div></div><div data-ref="cch"></div></div>`);
    wrap._r.clist.append(card);
    const c = new LineChart(card._r.cch, chartOpt(d, { legend: card._r.cleg, group: 'sheet', height: 180 }));
    ctx.charts.push(c); return { d, c };
  });
  const keys = [...new Set(defs.flatMap(d => d.series.map(s => s.m)))];
  const draw = () => charts.forEach(({ d, c }) => c.setData(d.series.map(se => { const pts = seriesOf(ctx.res, se.m, ctx.range); return d.tf ? d.tf(pts) : pts; }), RANGE_SEC[ctx.range]));
  const load = async r => { ctx.range = r; const res = await fetchSeries(keys, r); if (ctx.range !== r || sheetCtx !== ctx) return; ctx.res = res; draw(); };
  wrap._r.cseg.append(makeSeg(RANGES, UI.range, load, 'sm'));
  ctx.chartTick = () => { if (ctx.res && ['10m', '1h'].includes(ctx.range)) draw(); };
  ctx.chartReload = () => load(ctx.range);
  ctx.range = UI.range; load(UI.range);
}
/** 功率序列 → 累计 kWh（跳过停机断档） */
function cumKwh(pts) {
  const out = []; if (!pts || pts.length < 2) return out;
  const cap = medianDt(pts) * 3; let cum = 0;
  for (let i = 1; i < pts.length; i++) { const dt = pts[i][0] - pts[i - 1][0]; if (dt > 0 && dt <= cap) cum += pts[i - 1][1] * dt / 3.6e6; out.push([pts[i][0], cum]); }
  return out;
}
const procRows = (list, key) => list.length ? list.map(p => `<div class="row-i" style="grid-template-columns:1fr auto"><div style="min-width:0"><b style="font-weight:600">${esc(p.comm)}</b> <span class="faint tnum">${esc(p.pid)}</span><div class="cmd-line">${esc(p.args)}</div></div><b class="tnum">${key === 'cpu' ? F.d1(p.cpu) + '%' : F.bytesStr(p.rss)}</b></div>`).join('') : `<div class="row-i">${emptyHTML('暂无进程数据', '需要系统 ps 命令')}</div>`;
const pillFor = (lv, text) => `<span class="pill ${lv === 'none' ? '' : lv || 'ok'}">${lv === 'sleep' ? ic('moon') : lv === 'warn' || lv === 'crit' ? ic('alert') : lv === 'none' ? ic('info') : '<span class="dot"></span>'}${esc(text)}</span>`;

const SHEETS = {
  health: {
    icon: 'shield', title: () => '健康检查', sub: () => '每次刷新自动复核 · 阈值可在设置中调整',
    build(b) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">健康分</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">分</span></div></div><div data-ref="lvl"></div></div>
        <div class="sheet-stack" data-ref="probs" style="display:contents"></div>
        ${sec('逐项检查')}${rowsCard('<div data-ref="list"></div>')}
        <p class="sub">健康分从 100 起算：被忽略的提醒不计分；每项需要留意 −7，严重 −22，提示 −1。判定使用「设置 → 提醒阈值」，与健康报告口径一致。</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" type="button" data-dl="/api/report?format=html">${ic('download')}生成健康报告（HTML）</button><button class="btn ghost" type="button" data-goset="thresholds">${ic('alert')}调整提醒阈值</button></div>`;
      const x = collect(b);
      b.addEventListener('click', e => {
        const t = e.target.closest('[data-dl]'); if (t) doDownload(t.dataset.dl);
        const g = e.target.closest('[data-goset]'); if (g) { closeSheet(); focusGroup(g.dataset.goset); }
        const ig = e.target.closest('[data-ig]'); if (ig) { e.stopPropagation(); const it = (b._issues || []).find(i => i.key === ig.dataset.ig); if (it) Ignore.add(it); }
        const un = e.target.closest('[data-unig]'); if (un) { e.stopPropagation(); Ignore.remove(un.dataset.unig); }
      });
      return o => {
        const h = computeHealth(o); dnum(x.v, h.score); b._issues = h.issues;
        const lvIc = lv => `<span class="${lv === 'info' ? 'faint' : 'st-' + lv}">${ic(lv === 'info' ? 'info' : 'alert')}</span>`;
        let ph = '';
        if (h.issues.length) ph += sec('需要处理', `<span class="faint">${h.issues.length} 项</span>`) + rowsCard(h.issues.map(it => `<div class="row-i ig-row ${it.sheet ? 'tap-row' : ''}" ${it.sheet ? `data-sheet="${esc(it.sheet)}"` : ''}>${lvIc(it.level)}<div style="min-width:0"><b style="font-weight:600">${esc(it.text)}</b>${it.changed ? '<div class="ig-meta">忽略后状态发生变化，已重新提醒</div>' : ''}</div><button class="btn sm ghost" type="button" data-ig="${esc(it.key)}" title="状态变化时会重新提醒">${ic('eye-off')}忽略</button></div>`).join(''));
        if (h.ignored.length) ph += sec('已忽略', '<span class="faint">不计入健康分 · 状态变化时重新提醒</span>') + rowsCard(h.ignored.map(it => `<div class="row-i ig-row">${lvIc(it.level)}<div style="min-width:0"><b style="font-weight:600;color:var(--ink-2)">${esc(it.text)}</b></div><button class="btn sm" type="button" data-unig="${esc(it.key)}">${ic('eye')}恢复提醒</button></div>`).join(''));
        setHTML(x.probs, ph);
        setHTML(x.lvl, `<span class="pill ${h.level}">${h.level === 'ok' ? '<span class="dot"></span>良好' : h.level === 'warn' ? ic('alert') + '需要留意' : ic('alert') + '严重'}</span>`);
        const row = (st, t, d, sheet) => `<div class="row-i ${sheet ? 'tap-row' : ''}" style="grid-template-columns:auto 1fr auto" ${sheet ? `data-sheet="${esc(sheet)}"` : ''}><span class="${st === true ? 'st-ok' : st === 'info' || st === 'none' ? 'faint' : 'st-' + st}">${ic(st === true ? 'check-c' : st === 'info' || st === 'none' ? 'info' : st === 'sleep' ? 'moon' : 'alert')}</span><div><b style="font-weight:600">${t}</b><div class="sub">${d}</div></div>${sheet ? ic('chev', 'i-sm faint') : ''}</div>`;
        const cpu = o.cpu || {}, mem = o.mem || {}, ct = cpuTemp(o);
        let html = row(LV.cpu(cpu.percent) || true, 'CPU 占用', `当前 ${F.int(cpu.percent)}% · 阈值 ${TH.cpuWarn}% / ${TH.cpuCrit}%`, 'cpu')
          + row(ct == null ? 'none' : LV.temp(ct) || true, 'CPU 温度', ct == null ? '未读取到温度传感器' : `${F.int(ct)}℃ · 阈值 ${TH.tempWarn}℃ / ${TH.tempCrit}℃`, 'temp')
          + row(LV.mem(mem.percent) || true, '内存', `已用 ${F.int(mem.percent)}% · 阈值 ${TH.memWarn}% / ${TH.memCrit}%${+mem.swap_total ? ` · 交换分区 ${F.int(mem.swap_percent)}%` : ''}`, 'mem')
          + row(+o.disk_size > 0 ? LV.cap(o.disk_total) || true : 'none', '存储池', +o.disk_size > 0 ? `已用 ${F.int(o.disk_total)}% · 阈值 ${TH.capWarn}% / ${TH.capCrit}%` : '未读取到存储池用量', 'storage');
        disksOf(o).forEach(d => {
          const sm = smartState(d), dt = isSleep(d) ? '' : LV.dtemp(d.temp);
          const st = sm.lv === 'crit' || dt === 'crit' ? 'crit' : sm.lv === 'warn' || dt === 'warn' ? 'warn' : sm.lv === 'sleep' ? 'sleep' : sm.lv === 'none' ? 'none' : true;
          html += row(st, `硬盘 ${esc(d.name)}`, isSleep(d) ? '休眠中，未唤醒检测' : `${esc(sm.text)}${ok(d.temp) ? ` · ${F.int(d.temp)}℃` : ''}${+d.total > 0 ? ` · 已用 ${F.int(d.percent)}%` : ''}`, 'disk/' + encodeURIComponent(d.name));
        });
        const dk = S.docker || o.docker;
        if (dockerOk(dk)) { const stopped = (dk.containers || []).filter(c => !ctrRun(c)); html += row(stopped.length ? 'info' : true, '容器', stopped.length ? `${stopped.slice(0, 4).map(c => esc(c.name)).join('、')}${stopped.length > 4 ? ' 等' : ''} 未运行` : '全部运行中'); }
        else html += row('none', '容器', esc((dk && dk.error) || 'Docker 不可用'));
        h.issues.filter(i => /^(raid|vd|pd):/.test(i.key)).forEach(i => { html += row(i.level, '阵列', esc(i.text), 'storage'); });
        setHTML(x.list, html);
      };
    },
  },
  cpu: {
    icon: 'cpu', title: () => '处理器', sub: o => { const c = o.cpu || {}, hc = (o.hardware || {}).cpu || {}; return [hc.model || c.model, c.cores ? `${c.cores} 核 ${c.threads || c.cores} 线程` : ''].filter(Boolean).join(' · '); },
    build(b, arg, ctx) {
      const o = S.o, n = ((o.cpu || {}).per_core || []).length;
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">CPU 使用率</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">%</span></div></div><div style="text-align:right"><div class="eyebrow">负载 1 / 5 / 15</div><b class="tnum" data-ref="load"></b></div></div>
        ${sec('每核使用率')}<div class="card pad">${n ? `<div class="dcores" style="--cols:${Math.min(8, n)}">${Array.from({ length: n }, (_, i) => `<div class="dcore" data-ref="dc${i}"><div class="dcore-f"><span>C${i}</span><b data-ref="cv${i}"></b></div></div>`).join('')}</div>` : emptyHTML('每核数据不可用')}</div>`;
      sheetCharts(ctx, b, [TREND_DEFS[0].series && { title: 'CPU 使用率', yMin: 0, yMax: 100, yTick: v => v + '%', peak: true, thresholds: TREND_DEFS[0].thresholds, series: [TREND_DEFS[0].series[0]] }, { title: '系统负载（1 分钟）', yMin: 0, yTick: v => String(v), series: [{ m: 'load1', label: '负载', color: M.cpu, fmt: fLoad }] }]);
      b.insertAdjacentHTML('beforeend', `${sec('占用最高的进程', '<span class="faint">每 15 秒刷新</span>')}${rowsCard('<div data-ref="procs"></div>')}${sec('规格')}<div data-ref="spec"></div>${sec('显卡')}<div class="sheet-stack" data-ref="gpu"></div>`);
      const x = collect(b), cg = Array.from({ length: n }, (_, i) => dotGauge(x['dc' + i], 3, n > 8 ? 8 : 14));
      return o => {
        const c = o.cpu || {}, hc = (o.hardware || {}).cpu || {};
        dnum(x.v, c.percent); inkTo(x.v, ok(c.percent) ? rampInk(c.percent, 'cpu') : ''); setText(x.load, (c.load || []).map(v => F.d2(v)).join(' / ') || '--');
        (c.per_core || []).forEach((v, i) => { if (!cg[i]) return; cg[i].set(v / 100, LV.cpu(v), 'cpu'); setText(x['cv' + i], F.int(v)); inkTo(x['cv' + i], rampInk(v, 'cpu')); });
        setHTML(x.procs, procRows(((S.procs || {}).processes || []).slice(0, 8), 'cpu'));
        setHTML(x.spec, kvHTML([['型号', dash(hc.model || c.model)], ['核心 / 线程', `${dash(c.cores)} / ${dash(c.threads)}`], ['插槽', dash(c.sockets)], ['当前频率', ok(c.frequency_mhz) && c.frequency_mhz > 0 ? (c.frequency_mhz / 1000).toFixed(2) + ' GHz' : dash(null)], ['架构', dash(hc.arch || (o.system || {}).arch)]]));
        const gs = o.gpu || [];
        setHTML(x.gpu, gs.length ? gs.map(g => { const md = gpuMode(g), iu = gpuIntel(g);
          const util = md === 'pmu' ? `${F.d1(g.util)}%<div class="sub">${gpuEngs(g).map(([k, l]) => `${l} ${F.int(g.engines[k])}%`).join(' · ')}</div>` : md === 'rc6' ? `${F.d1(g.util)}%（RC6 活跃度估算）` : md === 'warm' ? '<span class="faint">采样中…</span>' : g.util_avail && ok(g.util) ? F.d1(g.util) + '%' : iu ? `<span class="faint">未能读取</span> ${diagLink()}` : dash(null);
          return kvHTML([['型号', esc(g.name) + (GPU_TAG[g.type] ? ` <span class="pill">${GPU_TAG[g.type]}</span>` : '')], ['利用率', util], ['温度', ok(g.temp) ? F.d1(g.temp) + ' ℃' : iu ? '<span class="faint">核显无独立温度读数（见 CPU 温度）</span>' : dash(null)], ['频率', ok(g.freq_mhz) ? F.int(g.freq_mhz) + ' MHz' + (ok(g.freq_req_mhz) ? `<div class="sub">请求 ${F.int(g.freq_req_mhz)} MHz</div>` : '') : dash(null)], ['显存', +g.mem_total > 0 ? `${F.bytesStr(g.mem_used)} / ${F.bytesStr(g.mem_total)}（${F.d1(g.mem_pct)}%）` : g.shared_memory ? '<span class="faint">与系统共享内存</span>' : dash(null)]]); }).join('') : `<div class="card pad">${emptyHTML('未检测到可监控的显卡', esc(((o.hardware || {}).gpu || []).join('、')))}</div>`);
      };
    },
  },
  mem: {
    icon: 'mem', title: () => '内存', sub: o => { const it = memItems(o).filter(i => i.present); return `${gbT((o.mem || {}).total)} GB${it.length ? ` · ${it.length} 根 ${esc(it[0].type || '')}` : ''}`; },
    build(b, arg, ctx) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">已用</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">%</span></div></div><div style="text-align:right"><div class="eyebrow">可用</div><b class="tnum" data-ref="avail"></b></div></div>
        <div class="card pad" data-dc="mem"><div class="stack" data-ref="st"><i></i><i class="hatch"></i><i style="background:var(--ink-3)"></i><i style="background:var(--fill-2)"></i></div><div class="legend" data-ref="leg"></div></div>
        <div class="note info">${ic('info')}<span>“已用”为扣除可回收缓存后的实际占用；“缓存”是 Linux 用空闲内存加速读盘的部分，应用需要时会立即让出。</span></div>`;
      sheetCharts(ctx, b, [{ title: '内存使用率', yMin: 0, yMax: 100, yTick: v => v + '%', series: [{ m: 'mem', label: '内存', color: M.mem, fmt: fPct }] }]);
      b.insertAdjacentHTML('beforeend', `${sec('内存占用较多的进程', '<span class="faint">取自 CPU 前 20 的进程</span>')}${rowsCard('<div data-ref="procs"></div>')}${sec('交换分区')}<div data-ref="swap"></div>${sec('内存插槽')}<div data-ref="slots"></div>`);
      const x = collect(b);
      return o => {
        const m = o.mem || {}; dnum(x.v, m.percent); inkTo(x.v, ok(m.percent) ? rampInk(m.percent, 'mem') : ''); setText(x.avail, gb(m.available) + ' GB');
        const mp = memParts(m), mc = ok(m.percent) ? rampAt(m.percent, 'mem') : 'var(--e-teal)';
        [mp.app, mp.cache, mp.buf, mp.free].forEach((v, i) => { x.st.children[i].style.flexGrow = Math.max(0, v).toFixed(0); }); x.st.children[0].style.background = mc;
        setHTML(x.leg, memLegHTML(mp, mc));
        setHTML(x.procs, procRows(((S.procs || {}).processes || []).slice().sort((a, c) => c.rss - a.rss).slice(0, 6), 'rss'));
        setHTML(x.swap, +m.swap_total > 0 ? kvHTML([['已用', `${gb(m.swap_used)} GB（${F.d1(m.swap_percent)}%）`], ['总量', gb(m.swap_total) + ' GB']]) : kvHTML([['状态', '未启用交换分区']]));
        const it = memItems(o);
        setHTML(x.slots, it.length ? kvHTML(it.map(i => [esc(i.locator || '插槽'), i.present ? esc([i.manufacturer, i.size, [i.type, i.speed].filter(Boolean).join('-'), i.part].filter(Boolean).join(' · ')) : '<span class="faint">空</span>'])) : `<div class="card pad">${emptyHTML('未读取到内存插槽信息', '需要 root 权限运行 dmidecode')}</div>`);
      };
    },
  },
  storage: {
    icon: 'storage', title: () => '存储', sub: o => `${disksOf(o).length} 块硬盘${o.disk_pools ? ` · ${o.disk_pools} 个存储池` : ''}${+o.disk_size > 0 ? ' · ' + F.dsizeStr(o.disk_size) : ''}`,
    build(b, arg, ctx) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">存储池已用</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">%</span></div></div><div style="text-align:right"><div class="eyebrow">可用</div><b class="tnum" data-ref="free"></b></div></div>
        ${rowsCard('<div data-ref="disks"></div>')}`;
      sheetCharts(ctx, b, [TREND_DEFS[2], { title: '存储池使用率', yMin: 0, yMax: 100, yTick: v => v + '%', thresholds: () => [{ v: TH.capWarn, lv: 'warn' }, { v: TH.capCrit, lv: 'crit' }], series: [{ m: 'disk_total', label: '已用', color: M.io, fmt: fPct }] }]);
      b.insertAdjacentHTML('beforeend', `${sec('分区与挂载')}<div class="card pad tbl-wrap"><table class="tbl"><thead><tr><th>挂载点</th><th class="hide-m">设备</th><th class="r">读</th><th class="r">写</th><th class="r">使用率</th><th class="r hide-m">已用 / 总量</th></tr></thead><tbody data-ref="parts"></tbody></table></div><div class="sheet-stack" data-ref="raid"></div>${sec('硬盘休眠状态', '<span data-ref="prot"></span>')}<div data-ref="sb"></div>`);
      const x = collect(b);
      return o => {
        const dd = disksOf(o), has = +o.disk_size > 0;
        dnum(x.v, has ? o.disk_total : null); inkTo(x.v, has ? rampInk(o.disk_total, 'cap') : ''); setText(x.free, has ? F.dsizeStr(o.disk_size - o.disk_used) : '--');
        const sig = dd.map(d => d.name).join();
        if (x._sig !== sig) {
          x._sig = sig;
          x.disks.innerHTML = dd.length ? dd.map(d => `<div class="row-i tap-row" data-sheet="disk/${esc(encodeURIComponent(d.name))}" style="grid-template-columns:auto minmax(0,1fr) 120px auto"><span class="drive-ico" style="width:36px;height:36px">${ic(diskIcon(d))}</span><div style="min-width:0"><b style="font-weight:600">${esc(d.name)}</b> <span class="faint">${esc(vendorModel(d))}</span><div class="sub" data-io="${esc(d.name)}"></div></div><div><div class="dbar" data-n="20" data-b="${esc(d.name)}"></div><div class="sub tnum" style="text-align:right;margin-top:4px" data-p="${esc(d.name)}"></div></div>${ic('chev', 'i-sm faint')}</div>`).join('') : `<div class="row-i">${emptyHTML('未检测到物理硬盘', '需要 lsblk 支持')}</div>`;
        }
        dd.forEach(d => {
          const q = s => x.disks.querySelector(`[${s}="${CSS.escape(d.name)}"]`), io = ioOf(o, d.name), sl = isSleep(d), sm = smartState(d), hc = +d.total > 0;
          dbar(q('data-b'), hc ? d.percent / 100 : 0, sl ? 'sleep' : LV.cap(d.percent), 'cap');
          const p = q('data-p'); setText(p, hc ? `${F.int(d.percent)}% · ${F.dsizeStr(d.total)}` : `${F.dsizeStr(d.size)} · 未挂载`); inkTo(p, sl || !hc ? '' : rampInk(d.percent, 'cap'));
          setHTML(q('data-io'), sl ? `<span style="color:var(--ink-2)">${ic('moon', 'i-sm')} 休眠中</span>` : (sm.lv === 'warn' || sm.lv === 'crit' ? `<span class="st-${sm.lv}">${esc(sm.text)}</span> · ` : '') + (io ? `读 ${F.rateStr(io.read_rate)} · 写 ${F.rateStr(io.write_rate)}` : '无 IO 数据'));
        });
        const io = dev => { dev = String(dev || '').split('/').pop(); const map = {}; (o.diskio || []).forEach(z => { map[z.name] = z; }); for (let i = dev.length; i >= 1; i--) if (map[dev.slice(0, i)]) return map[dev.slice(0, i)]; return map[dev.replace(/\d+$/, '')] || null; };
        setHTML(x.parts, (o.disks || []).length ? o.disks.map(p => { const z = io(p.device); return `<tr><td class="mono">${esc(p.mount)}${p.standby ? ` ${ic('moon', 'i-sm')}` : ''}</td><td class="hide-m mono faint">${esc(p.device)}</td><td class="r">${z ? F.rateStr(z.read_rate) : '--'}</td><td class="r">${z ? F.rateStr(z.write_rate) : '--'}</td><td class="r" style="color:${rampInk(+p.percent || 0, 'cap')}">${F.d1(p.percent)}%</td><td class="r hide-m muted">${F.dsizeStr(p.used)} / ${F.dsizeStr(p.total)}</td></tr>`; }).join('') : '<tr><td colspan="6" class="faint">没有可用的分区数据</td></tr>');
        const raid = o.raid || [], rc = o.raidcard || {};
        let rh = '';
        if (raid.length || (rc.type && rc.type !== 'none')) {
          rh += sec('阵列') + kvHTML(raid.map(r => [esc(r.device), `${esc(r.status)} · <span class="mono">${esc((r.members || []).join(' '))}</span>`]).concat(rc.type && rc.type !== 'none' ? [['阵列卡', esc(rc.label || rc.type)]] : []).concat(!raid.length ? [['mdadm', '无软阵列']] : []));
          (rc.controllers || []).forEach(c => {
            rh += kvHTML([[esc(c.model || 'Controller ' + c.id), esc([c.status && '状态 ' + c.status, c.roc_temp != null && '芯片 ' + c.roc_temp + '℃', c.memory].filter(Boolean).join(' · ') || '--')]].concat((c.vd || []).map(v => [`VD${esc(v.vd)} ${esc(v.name || '')}`, `${pillFor(v.badge === 'off' ? 'crit' : v.badge === 'on' ? '' : 'none', v.state_label || v.state || '--')} ${esc(v.type || '')} · ${esc(v.size || '')}`])));
            if ((c.pds || []).length) rh += `<div class="card pad tbl-wrap"><table class="tbl"><thead><tr><th>盘位</th><th>型号</th><th class="hide-m">介质</th><th class="r">容量</th><th class="r">温度</th><th class="r">健康</th></tr></thead><tbody>${c.pds.map(p => `<tr><td class="mono">${esc(p.loc)}</td><td>${esc(p.model || '--')}<div class="mono faint">${esc(p.serial || '')}</div></td><td class="hide-m">${esc(((p.intf || '') + ' ' + (p.media || '')).trim() || '--')}</td><td class="r">${esc(p.size || '--')}</td><td class="r">${p.temp != null ? esc(p.temp) + '℃' : '--'}</td><td class="r">${pillFor(p.health === 'ok' ? '' : p.health === 'warn' ? 'warn' : 'crit', p.health_label || p.health || '--')}${+p.media_errors > 0 ? ` <span class="st-warn">介质错误 ${esc(p.media_errors)}</span>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
            else rh += `<div class="note info">${ic('info')}<span>未读取到阵列卡物理盘（storcli 需 root 权限，或版本过旧）。</span></div>`;
          });
          if (rc.type === 'megaraid' && !(rc.controllers || []).length) rh += `<div class="note info">${ic('info')}<span>${esc(rc.error === 'storcli not found' ? '未找到 storcli / perccli：下载 storcli 解压后把 storcli64 放入 /usr/local/bin/，稍候自动识别' : rc.error || rc.detail || 'storcli 读取失败')}</span></div>`;
          else if (rc.type && rc.type !== 'none' && rc.type !== 'megaraid' && rc.detail) rh += `<div class="note info">${ic('info')}<span>${esc(rc.detail)}</span></div>`;
        }
        setHTML(x.raid, rh);
        const ps = S.pstats, sb = (ps && ps.disks) || [];
        setHTML(x.prot, ps ? (ps.standby_protect === false ? '<span class="pill">保护已关闭</span>' : '<span class="pill ok"><span class="dot"></span>保护中 · 不唤醒休眠硬盘</span>') : '');
        setHTML(x.sb, sb.length ? kvHTML(sb.map(z => [esc(z.name), z.state === 'standby' ? `${ic('moon', 'i-sm')} 休眠` : z.state === 'active/idle' ? '活动' : '<span class="faint">未知</span>'])) : `<div class="card pad">${emptyHTML('无硬盘休眠数据')}</div>`);
      };
    },
  },
  disk: {
    icon: a => { const d = disksOf(S.o).find(z => z.name === a); return d ? diskIcon(d) : 'hdd'; },
    title: a => '硬盘 ' + a, sub: (o, a) => { const d = disksOf(o).find(z => z.name === a); return d ? vendorModel(d) : ''; },
    build(b, a) {
      const d0 = disksOf(S.o).find(z => z.name === a);
      if (!d0) { b.innerHTML = `<div class="empty"><b>未找到硬盘 ${esc(a)}</b><span>它可能已被移除，或尚未被识别。</span></div>`; return; }
      const sm0 = smartState(d0), sl = isSleep(d0);
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">已用</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">%</span></div></div><div data-ref="pill"></div></div>
        <div class="card pad"><div class="dbar" data-n="40" data-ref="bar"></div><div class="legend" data-ref="leg"></div></div>
        <div class="sheet-stack" data-ref="notes"></div>
        ${sl ? '' : `${sec('实时读写', '<span class="faint">近 5 分钟</span>')}<div class="card chart-card"><div class="chart-head"><div class="chart-title">读写速率</div><div class="chart-legend"><span><span class="lk"><i class="k0" style="--k:var(--e-blue)"></i>读取</span><span class="lv" data-ref="rv">--</span></span><span><span class="lk"><i class="k1" style="--k:var(--e-coral)"></i>写入</span><span class="lv" data-ref="wv">--</span></span></div></div><div style="height:64px" data-ref="sp"></div></div>`}
        ${sec('SMART')}<div class="card pad" style="padding-top:14px;padding-bottom:6px"><table class="tbl"><thead><tr><th>属性</th><th class="r">值</th><th class="r">状态</th></tr></thead><tbody data-ref="smart"></tbody></table></div>
        ${sec('信息')}<div data-ref="info"></div>`;
      const x = collect(b);
      if (x.sp) x.sp.append(makeSpark());
      void sm0;
      /* 单盘 SMART 明细按需读取：只有打开这个面板才会请求，不参与 60 秒循环。
         后端另有一层 5 分钟缓存，重复点开不会反复跑 smartctl。 */
      const paintSmart = () => {
        const dd = disksOf(S.o).find(z => z.name === a); if (!dd) return;
        const slp = isSleep(dd), sd = smartDetailOf(a), sas = !!(sd && sd.scsi);
        const val = k => (sd && sd[k] != null ? sd[k] : dd[k]);
        const temp = (sd && sd.temp_c != null) ? sd.temp_c : dd.temp;
        const num = v => ok(v) ? esc(Number(v).toLocaleString()) : '<span class="faint">—</span>';
        const flag = v => (+v > 0 ? 'warn' : true);
        let rows = [['整体评估', dd.health ? esc(/PASS/i.test(dd.health) ? '通过' : dd.health) : '<span class="faint">未读取到</span>',
          /FAIL/i.test(dd.health || '') ? 'crit' : /PASS|OK/i.test(dd.health || '') ? true : null]];
        if (sas) {
          // SAS/SCSI 没有 ATA 属性表：看增长缺陷表与错误计数器，ECC 计数会回绕只能参考
          rows = rows.concat([
            ['增长缺陷表', num(sd.grown_defects), sd.grown_defects != null ? flag(sd.grown_defects) : null],
            ['待定缺陷', num(sd.scsi_pending_defects), sd.scsi_pending_defects != null ? flag(sd.scsi_pending_defects) : null],
            ['不可纠正错误', num(sd.scsi_uncorrected), flag(sd.scsi_uncorrected || 0)],
            ['ECC 纠正次数', num(sd.scsi_ecc) + ' <span class="faint">计数器会回绕</span>', null],
            ['重读 / 重写', num(sd.scsi_rereads), null],
            ['已处理数据', ok(sd.scsi_gb) ? esc(F.dsizeStr(sd.scsi_gb * 1e9)) : '<span class="faint">—</span>', null],
          ]);
        } else {
          const cnt = SMART_KEYS.filter(([k]) => val(k) != null && val(k) !== '');
          rows = rows.concat(cnt.map(([k, l]) => [l, esc(val(k)), flag(val(k))]));
        }
        rows = rows.concat([
          ['通电时间', val('power_on_hours') ? `${esc(Number(val('power_on_hours')).toLocaleString())} 小时（${F.hours(val('power_on_hours'))}）` : '<span class="faint">—</span>', null],
          ['温度', ok(temp) ? `${F.int(temp)}℃` : '<span class="faint">—</span>', ok(temp) && !slp ? (LV.dtemp(temp) || true) : null],
          ['转速', val('rpm') ? esc(val('rpm')) : '<span class="faint">—</span>', null]]);
        let hint = '';
        if (sas) hint = slp ? '休眠中，明细待硬盘唤醒后读取' : '';
        else if (!rows.some(r => r[2] === 'warn' || r[2] === 'crit') && !SMART_KEYS.some(([k]) => val(k) != null && val(k) !== '')) {
          hint = slp ? '休眠中，扇区计数待硬盘唤醒后读取' : (sd && sd.error ? `SMART 明细读取失败：${esc(sd.error)}` : '该硬盘未提供重映射 / 待映射扇区等计数（NVMe 或 smartctl 不可用）');
        }
        setHTML(x.smart, rows.map(([k, v, s]) => `<tr><td>${k}</td><td class="r tnum ${s === 'warn' || s === 'crit' ? 'st-' + s + ' smart-flag' : ''}">${v}</td><td class="r">${slp && k !== '整体评估' ? '<span class="faint">休眠前</span>' : s === true ? `<span class="st-ok">${ic('check', 'i-sm')}</span>` : s ? `<span class="pill ${s}">${s === 'crit' ? '严重' : '留意'}</span>` : '<span class="faint">—</span>'}</td></tr>`).join('')
          + (hint ? `<tr><td colspan="3" class="faint" style="font-size:12px">${hint}</td></tr>` : ''));
        const extra = [];
        if (sas && sd.manufactured) extra.push(['生产周期', esc(sd.manufactured)]);
        /* 实际硬盘位 = iDRAC 物理盘列表里的「插槽编号」，靠序列号对齐到 /dev/sdX。
           槽位不会变，后端跟着 10 分钟才刷一次的明细一起读，不参与实时轮询。
           对不上（iDRAC 没配 / 盘不在阵列卡下 / 序列号读不到）就显示 —，不猜一个
           出来，免得照错的位置去拔盘。 */
        const slotTip = '机箱里的物理槽位，取 iDRAC 物理盘列表的「插槽编号」（从 0 起）';
        const slot = dd.slot == null
          ? '<span class="faint" title="没读到槽位：iDRAC 未启用，或这块盘不在阵列卡下、序列号对不上">—</span>'
          : `<b class="mono" title="${slotTip}，对照 iDRAC 物理盘列表第 ${F.int(dd.slot)} 项">插槽 ${F.int(dd.slot)}</b> <span class="faint">iDRAC</span>`;
        setHTML(x.info, kvHTML([['型号', dash(vendorModel(dd))], ['序列号', `<span class="mono">${dash(dd.serial)}</span>`],
          ['类型', KIND_TXT[diskKind(dd)]], ['硬盘容量', F.dsizeStr(dd.size)],
          ['挂载点', `<span class="mono">${dash((dd.mounts || []).join(', '))}</span>`],
          ['实际硬盘位', slot]].concat(extra)));
      };
      /* 休眠盘不发请求：smartctl -n standby 不会唤醒它，但省掉一次无意义的 IO */
      if (!sl) loadSmartDetail(a).then(() => paintSmart());
      return o => {
        const d = disksOf(o).find(z => z.name === a); if (!d) return;
        const sm = smartState(d), sl2 = isSleep(d), hc = +d.total > 0;
        dnum(x.v, hc ? d.percent : null); dbar(x.bar, hc ? d.percent / 100 : 0, sl2 ? 'sleep' : LV.cap(d.percent), 'cap'); inkTo(x.v, sl2 ? 'var(--ink-3)' : hc ? rampInk(d.percent, 'cap') : '');
        setHTML(x.pill, pillFor(sm.lv, sm.lv === '' ? '健康' : sm.lv === 'none' ? 'SMART 未知' : sm.lv === 'sleep' ? '休眠中' : '需要留意'));
        setHTML(x.leg, hc ? `<span>已用 <b>${F.dsizeStr(d.used)}</b></span><span>可用 <b>${F.dsizeStr(d.total - d.used)}</b></span><span>文件系统容量 <b>${F.dsizeStr(d.total)}</b></span>` : `<span>未挂载或无用量数据 · 硬盘容量 <b>${F.dsizeStr(d.size)}</b></span>`);
        setHTML(x.notes, (sl2 ? `<div class="note sleep">${ic('moon')}<span>这块硬盘已停转休眠。飞牛监控不会主动唤醒它：容量与温度为休眠前的最后读数，SMART 会在它下次被系统唤醒时更新。</span></div>` : '')
          + (sm.lv === 'warn' || sm.lv === 'crit' ? `<div class="note warn">${ic('alert')}<span><b>${esc(sm.text)}。</b>建议确认 ${esc((d.mounts || []).join('、') || '该盘上的数据')} 有备份，并留意数值是否继续增长。</span></div>` : '')
          + (!sl2 && LV.dtemp(d.temp) ? `<div class="note warn">${ic('temp')}<span>温度 ${F.int(d.temp)}℃，已超过${LV.dtemp(d.temp) === 'crit' ? '严重' : '警告'}阈值（${LV.dtemp(d.temp) === 'crit' ? TH.diskTempCrit : TH.diskTempWarn}℃）。</span></div>` : ''));
        paintSmart();
        if (x.sp) { const bb = S.diskBuf[d.name]; if (bb && bb.r.length > 1) x.sp.firstChild.set([{ values: bb.r }, { values: bb.w }], { min: 0 }); const io = ioOf(o, d.name); setText(x.rv, io ? F.rateStr(io.read_rate) : '--'); setText(x.wv, io ? F.rateStr(io.write_rate) : '--'); }
      };
    },
  },
  net: {
    icon: 'net', title: () => '网络', sub: o => nets(o).map(n => n.iface + (speedOf(o, n.iface) ? ' ' + speedTxt(speedOf(o, n.iface)) : '')).join(' · ') || '未检测到物理网口',
    build(b, arg, ctx) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">↓ 下行</div><div class="dnum"><span class="dm" data-ref="rx"></span><span class="unit" data-ref="rxU"></span></div></div><div style="text-align:right"><div class="eyebrow">↑ 上行</div><div class="dnum sec" style="color:var(--ink-2)"><span class="dm" data-ref="tx"></span><span class="unit" data-ref="txU"></span></div></div></div>`;
      sheetCharts(ctx, b, [TREND_DEFS[1], { title: '累计流量（全部网口计数器）', yMin: 0, yTick: shortBytes, series: [{ m: 'net_bytes.rx', label: '下行累计', color: M.io, fmt: F.bytes }, { m: 'net_bytes.tx', label: '上行累计', color: M.io2, fmt: F.bytes }] }]);
      b.insertAdjacentHTML('beforeend', `${sec('网口')}<div data-ref="ifs"></div>${sec('流量统计')}<div data-ref="tot"></div>${sec('容器流量', '<span class="faint">累计 · 自容器启动</span>')}<div class="card pad" data-ref="ctr"></div>`);
      const x = collect(b);
      return o => {
        dnumU(x.rx, x.rxU, sumRx(o), F.rate); dnumU(x.tx, x.txU, sumTx(o), F.rate);
        const t = S.traffic || {}, ifs = t.ifaces || o.net || [];
        setHTML(x.ifs, ifs.length ? kvHTML(ifs.filter(n => n.iface !== 'lo').map(n => [esc(n.iface) + (VIRT.test(n.iface) ? ' <span class="faint">虚拟</span>' : ''), `↓ ${F.rateStr(n.rx_rate)} · ↑ ${F.rateStr(n.tx_rate)}<div class="sub">累计 ↓ ${F.bytesStr(n.rx_bytes)} ↑ ${F.bytesStr(n.tx_bytes)}${+n.rx_drop || +n.tx_drop ? ` · <span class="st-warn">丢包 ${esc((+n.rx_drop || 0) + (+n.tx_drop || 0))}</span>` : ''}${+n.rx_errs || +n.tx_errs ? ` · <span class="st-warn">错包 ${esc((+n.rx_errs || 0) + (+n.tx_errs || 0))}</span>` : ''}</div>`])) : `<div class="card pad">${emptyHTML('没有网口数据')}</div>`);
        const tt = t.totals;
        setHTML(x.tot, tt ? kvHTML([['今日', `↓ ${F.bytesStr(tt.today_rx)} · ↑ ${F.bytesStr(tt.today_tx)}`], ['本月', `↓ ${F.bytesStr(tt.month_rx)} · ↑ ${F.bytesStr(tt.month_tx)}`], ['开机以来', `↓ ${F.bytesStr(tt.boot_rx)} · ↑ ${F.bytesStr(tt.boot_tx)}`], ['Docker 网桥流量', (t.config || {}).exclude_bridge ? '已排除' : '已计入']]) : `<div class="card pad">${emptyHTML('流量统计加载中…')}</div>`);
        const cs = t.containers || [];
        setHTML(x.ctr, cs.length ? rankHTML(cs, 20) : emptyHTML(dockerOk(S.docker || o.docker) ? '没有运行中的容器' : 'Docker 不可用', '', 'box'));
      };
    },
  },
  temp: {
    icon: 'temp', title: () => '温度与散热', sub: o => { const s = o.sensors || {}; return `${(s.temps || []).length + diskThermo(o).length + (cpuTemp(o) != null ? 1 : 0)} 个温度测点 · ${(s.fans || []).length} 个风扇`; },
    build(b, arg, ctx) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">CPU</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">℃</span></div></div><div style="text-align:right"><div class="eyebrow">阈值</div><b class="tnum" data-ref="thr"></b></div></div>
        <div class="stat-cells temp-cells"><div class="stat-cell"><div class="eyebrow">CPU</div><b class="tnum" data-ref="c1"></b></div><div class="stat-cell"><div class="eyebrow">主板</div><b class="tnum" data-ref="c2"></b></div><div class="stat-cell"><div class="eyebrow">显卡</div><b class="tnum" data-ref="c3"></b></div><div class="stat-cell"><div class="eyebrow">全部测点最高</div><b class="tnum" data-ref="c4"></b></div></div>
        <div class="card pad" data-ref="th"></div>`;
      sheetCharts(ctx, b, [Object.assign({}, TREND_DEFS[3], { peak: true }), TREND_DEFS[5]]);
      b.insertAdjacentHTML('beforeend', `${sec('风扇')}<div data-ref="fans"></div>${sec('电压')}<div data-ref="volts"></div>${sec('全部传感器')}<div data-ref="all"></div>`);
      const x = collect(b);
      return o => {
        const ct = cpuTemp(o); dnum(x.v, ct); inkTo(x.v, ct != null ? rampInk(ct, 'temp') : '');
        setText(x.thr, `CPU ${TH.tempWarn} / ${TH.tempCrit}℃ · 硬盘 ${TH.diskTempWarn} / ${TH.diskTempCrit}℃`);
        const s = o.sensors || {}, temps = s.temps || [], tv = t => ok(t) ? F.d1(t) + ' ℃' : '—';
        const gT = (o.gpu || []).map(g => g.temp).filter(ok).concat(temps.filter(t => t.type === 'gpu').map(t => t.value)), allT = temps.map(t => t.value).concat([ct, mbTemp(o)], diskTemps(o).map(d => d.temp)).filter(ok).map(Number);
        setText(x.c1, tv(ct)); setText(x.c2, tv(mbTemp(o))); setText(x.c3, gT.length ? tv(Math.max(...gT)) : '—'); setText(x.c4, allT.length ? tv(Math.max(...allT)) : '—');
        const list = thermoList(o, true).concat(diskThermo(o)), sig = list.map(t => t.k).join();
        if (x._sig !== sig) { x._sig = sig; x.th.innerHTML = list.length ? thermoRows(list) + scaleHTML : emptyHTML('未读取到任何温度传感器', 'fnOS 默认未安装 lm-sensors，已直读 thermal zone 与 hwmon；虚拟机或容器内通常无法访问硬件传感器。'); }
        updateThermo(x.th, list);
        setHTML(x.fans, (s.fans || []).length ? kvHTML(s.fans.map(f => [esc(fanName(f)), `${F.int(f.rpm)} RPM`]).concat(s.fans_source ? [['数据来源', `<span class="mono">${esc(s.fans_source)}</span>`]] : [])) : `<div class="card pad">${emptyHTML('未检测到可读取的风扇传感器', esc(NO_FAN))}</div>`);
        setHTML(x.volts, (s.volts || []).length ? kvHTML(s.volts.map(v => [esc(v.name), `${F.d2(v.value)} V`])) : `<div class="card pad">${emptyHTML('未读取到电压数据')}</div>`);
        const TN = { cpu: 'CPU', pch: '芯片组', board: '主板', acpi: 'ACPI', disk: '硬盘', gpu: 'GPU' };
        setHTML(x.all, temps.length ? kvHTML(temps.map(t => [`${esc(t.name)} <span class="pill">${esc(TN[t.type] || t.type)}</span>`, `${tv(t.value)}${ok(t.max) ? `<div class="sub">上限 ${F.d1(t.max)} ℃</div>` : ''}`])) : `<div class="card pad">${emptyHTML(s.available ? 'sensors 未返回温度' : '未安装 lm-sensors（sensors 命令不可用）', '已使用 thermal zone / hwmon 直读作为替代')}</div>`);
      };
    },
  },
  power: {
    icon: 'bolt', title: () => '能耗', sub: o => pwrOk(o) ? '来源：' + (SRC_TXT[o.power.source] || '未知') + (o.power.source === 'rapl' ? ' + 部件估算' : '') : '功耗不可用',
    build(b, arg, ctx) {
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">实时功耗</div><div class="dnum"><span class="dm" data-ref="v"></span><span class="unit">W</span></div></div><div style="text-align:right"><div class="eyebrow">口径</div><b class="tnum" data-ref="src"></b></div></div>
        <div class="card pad" data-dc="pwr"><div class="stack" style="height:12px" data-ref="st"></div><div class="legend" data-ref="leg"></div></div><div data-ref="parts"></div>`;
      sheetCharts(ctx, b, [TREND_DEFS[4], { title: '累计用电（所选范围）', yMin: 0, yTick: v => v >= 10 ? F.kwh(v) : v >= 0.1 ? v.toFixed(2) : v.toFixed(3), series: [{ m: 'power', label: '累计', color: M.pwr, fmt: v => [F.kwh(v), 'kWh'] }], tf: cumKwh }]);
      b.insertAdjacentHTML('beforeend', `${sec('用电与电费', '<span class="faint" data-ref="rate"></span>')}<div data-ref="cost"></div>${sec('估算参数', '<button class="link-btn" type="button" data-goset="power">去设置</button>')}<div data-ref="est"></div>
        <div class="note info">${ic('info')}<span>接了 iDRAC 时直接用电源实测的整机输出，这是最准的口径（含主板、内存、网卡与全部硬盘）。没有 iDRAC 才会退回 RAPL：它只测得 CPU 封装（与内存）功耗，整机值再叠加主板底座与活动硬盘的典型功耗；无 RAPL 时按 TDP × CPU 占用估算。休眠硬盘不计入。</span></div>`);
      const x = collect(b);
      b.addEventListener('click', e => { const g = e.target.closest('[data-goset]'); if (g) { closeSheet(); focusGroup(g.dataset.goset); } });
      return o => {
        const p = o.power || {}, has = pwrOk(o), ps = S.pstats || {};
        dnum(x.v, has ? p.total : null, F.d1); setText(x.src, has ? (p.source === 'idrac' ? 'iDRAC 整机实测' : p.source === 'rapl' ? 'RAPL 实测 + 底座 / 硬盘估算' : '模型估算（无 RAPL）') : '不可用');
        const parts = has ? powerParts(p) : [];
        setHTML(x.st, parts.map(q => ppI(q[2])).join('')); parts.forEach((q, i) => { x.st.children[i].style.flexGrow = q[1].toFixed(2); });
        setHTML(x.leg, parts.map(([l, v, c]) => `<span>${ppI(c)}${esc(l)} <b>${F.d1(v)} W</b></span>`).join('') || '<span>无功耗数据</span>');
        const w = v => ok(v) ? F.d1(v) + ' W' : '<span class="faint">—</span>';
        // iDRAC 给的是整机电源输出，没有 CPU/内存/硬盘的分解，改列电源侧读数
        const psuRows = (p.psu || []).map(u => [`电源 ${esc(u.name)}`,
          `${ok(u.volts) ? esc(String(u.volts)) + ' V' : '—'} · ${esc(u.health || '')}${u.model ? ' · ' + esc(u.model) : ''}`]);
        setHTML(x.parts, has ? (p.source === 'idrac'
          ? kvHTML([['整机功耗', w(p.total)], ['电源容量', w(p.capacity)], ['最低（近 1 分钟）', w(p.min)], ['最高（近 1 分钟）', w(p.max)], ['平均（近 1 分钟）', w(p.avg)]].concat(psuRows))
          : kvHTML([['CPU 封装 Package', w(p.package)], ['　核心 Core', w(p.core)], ['　非核心 Uncore', w(p.uncore)], ['内存 DRAM', w(p.dram)], ['主板 / 其他底座', w(p.base)], [`活动硬盘${ok(p.disks_active) ? `（${p.disks_active} 块）` : ''}`, w(p.disks_w)]])) : '');
        const en = ps.energy || {}, co = ps.cost || {}, ep = ps.estimation_params;
        setText(x.rate, ok(co.rate_yuan_per_kwh) ? `电价 ${co.rate_yuan_per_kwh} 元/kWh` : '');
        setHTML(x.cost, ps.energy ? kvHTML([['今日', `${F.kwh(en.today_kwh)} kWh · ${F.yuan(co.today_yuan)}`], ['本月', `${F.kwh(en.month_kwh)} kWh · ${F.yuan(co.month_yuan)}`], ['开机以来（按当前功率估）', `${F.kwh(en.boot_kwh)} kWh`]]) : `<div class="card pad">${emptyHTML('功耗统计加载中…')}</div>`);
        setHTML(x.est, ep ? kvHTML([['CPU TDP（无 RAPL 时）', ep.tdp_w + ' W'], ['单盘典型功耗', ep.disk_typical_w + ' W'], ['网卡固定功耗', ep.nic_fixed_w + ' W'], ['主板 / 其他底座', ep.base_w + ' W']]) : '');
      };
    },
  },
  ctr: {
    icon: () => 'box', title: a => a, sub: (o, a) => { const c = Docker.find(a); return c ? c.image : ''; },
    build(b, a) {
      const c0 = Docker.find(a);
      if (!c0) { b.innerHTML = `<div class="empty"><b>未找到容器 ${esc(a)}</b><span>它可能已被删除，或 Docker 当前不可用。</span></div>`; return; }
      b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">CPU</div><div class="dnum"><span class="dm" data-ref="cpu"></span><span class="unit">%</span></div></div><div style="text-align:right"><div class="eyebrow">内存</div><div class="dnum sec"><span class="dm" data-ref="mem"></span><span class="unit" data-ref="memU"></span></div></div></div>
        <div class="ctr-acts" data-ref="acts"></div>
        <div class="card chart-card"><div class="chart-head"><div class="chart-title">CPU · 近几分钟</div><span data-ref="state"></span></div><div style="height:64px" data-ref="sp"></div></div>
        ${sec('实时网速', '<span class="faint">30 秒均值</span>')}<div class="stat-cells ctr-rates"><div class="stat-cell"><div class="eyebrow"><i class="ar dn">↓</i>下行 · 接收</div><div class="dnum"><span class="dm" data-ref="rx"></span><span class="unit" data-ref="rxU"></span></div></div><div class="stat-cell"><div class="eyebrow"><i class="ar up">↑</i>上行 · 发送</div><div class="dnum"><span class="dm" data-ref="tx"></span><span class="unit" data-ref="txU"></span></div></div></div><p class="sub ctr-net-note" data-ref="netN"></p>
        ${sec('详情')}<div data-ref="kv"></div>`;
      const x = collect(b), sp = makeSpark(); x.sp.append(sp);
      x.acts.addEventListener('click', e => { const btn = e.target.closest('[data-act]'); if (btn) Docker.act(btn.dataset.id, btn.dataset.name, btn.dataset.act, btn); });
      return () => {
        const c = Docker.find(a); if (!c) return;
        const pend = Docker.pending[c.id], run = ctrRun(c), cpu = ctrCpu(c), hp = hostPort(c.ports);
        dnum(x.cpu, run ? cpu : null, F.d1); dnumU(x.mem, x.memU, run ? ctrMem(c) : null, F.bytes);
        setHTML(x.state, pend ? `<span class="pill">${Docker.PEND[pend]}</span>` : run ? '<span class="pill ok"><span class="dot"></span>运行中</span>' : `<span class="pill">${esc(c.state || '已停止')}</span>`);
        const ids = `data-id="${esc(c.id)}" data-name="${esc(c.name)}"`;
        setHTML(x.acts, pend ? `<span class="spin"></span><span class="pending-t">${Docker.PEND[pend]}</span>` : (run
          ? `<button class="btn" type="button" data-act="restart" ${ids}>${ic('restart')}重启</button><button class="btn danger" type="button" data-act="stop" ${ids}>${ic('stop')}停止</button>`
          : `<button class="btn primary" type="button" data-act="start" ${ids}>${ic('play')}启动</button>`) + (run && hp ? `<a class="btn ghost" href="${esc(location.protocol + '//' + location.hostname + ':' + hp + '/')}" target="_blank" rel="noopener noreferrer">${ic('ext')}打开 :${esc(hp)}</a>` : ''));
        const rOk = ctrRateOk(c);
        dnumU(x.rx, x.rxU, rOk ? +c.rx_rate : null, F.rate); dnumU(x.tx, x.txU, rOk ? +c.tx_rate : null, F.rate);
        setText(x.netN, !run ? '容器未运行' : c.net_host ? 'host 网络模式：与宿主机共用网卡，Docker 不单独统计' : !rOk ? '采样中，约 30 秒后显示' : c.net_src === 'stats' ? '按 docker stats 的累计读数差分估算（只有 3 位有效数字，低速时偏粗）' : '容器网络命名空间计数器差分，精确到字节');
        const bb = S.ctrBuf[c.id]; sp.set([{ values: run && bb && bb.length > 1 ? bb : [0, 0] }], { min: 0, max: Math.max(5, ...(bb || [0])), live: run ? 'ink' : 'none' });
        setHTML(x.kv, kvHTML([['镜像', `<span class="mono">${esc(c.image)}</span>`], ['容器 ID', `<span class="mono">${esc(c.id)}</span>`], ['状态', esc(c.status_zh || zhStatus(c.status) || c.state || '--')], ['端口', ctrPorts(c.ports).length ? ctrPorts(c.ports).map(p => `<span class="port">${esc(p)}</span>`).join(' ') : '<span class="faint">无映射</span>'], ['内存', run && c.mem_usage ? `${esc(c.mem_usage)}${c.mem_percent ? `（${esc(c.mem_percent)}%）` : ''}` : '<span class="faint">—</span>'], ['累计流量', run && !c.net_host && (ok(c.net_in_bytes) || c.net) ? (ok(c.net_in_bytes) && ok(c.net_out_bytes) ? `↓ ${F.bytesStr(c.net_in_bytes)} · ↑ ${F.bytesStr(c.net_out_bytes)}<div class="sub">自容器启动</div>` : esc(c.net)) : '<span class="faint">—</span>'], ['创建时间', dash(c.created)]]));
      };
    },
  },
  keys: {
    icon: () => 'key', title: () => '键盘快捷键', sub: () => '在任何页面都可使用',
    build(b) {
      const np = navPages(), k = [[np.map((p, i) => i + 1).join(' / '), '切换到 ' + np.map(p => PAGE_TITLE[p]).join(' / ')], ['/', '搜索硬盘、容器、虚拟机、设置'], ['E', '编辑布局（隐藏、拖动排序）'], ['T', '切换浅色 / 深色'], ['R', '循环切换趋势时间范围'], ['F11', '全屏'], ['Esc', '关闭抽屉 / 搜索 / 退出编辑'], ['?', '显示本面板']];
      b.innerHTML = `<div class="card"><div class="keys">${k.map(([a, d]) => `<span>${d}</span><span>${a.split(' / ').map(z => `<span class="kbd">${z}</span>`).join(' ')}</span>`).join('')}</div></div>`;
    },
  },
};
const ICO_OF = (type, arg) => { const d = SHEETS[type]; return typeof d.icon === 'function' ? d.icon(arg) : d.icon; };
const SHEET_DC = { cpu: 'util', mem: 'util', storage: 'util', disk: 'util', net: 'io', temp: 'temp', power: 'pwr' };

let sheetCtx = null, lastFocus = null;
function openSheet(key, fromRoute) {
  const [type, ...rest] = String(key).split('/'); let arg = rest.join('/'); try { arg = decodeURIComponent(arg); } catch (e) { /* keep raw */ }
  const def = SHEETS[type]; if (!def || S.editing) return;
  if (!S.o) { S.pendingSheet = key; return; }
  if (sheetCtx && sheetCtx.key === key) return;
  const wasOpen = !!sheetCtx;
  if (!wasOpen) lastFocus = document.activeElement;
  teardownSheet();
  const body = $('#sheetBody'); body.innerHTML = ''; body.scrollTop = 0;
  const ctx = { key, type, charts: [] };
  const sheetEl = $('#sheet'); if (SHEET_DC[type]) sheetEl.dataset.dc = SHEET_DC[type]; else delete sheetEl.dataset.dc;
  sheetCtx = ctx;
  setHTML($('#sheetIco'), ic(ICO_OF(type, arg)));
  setText($('#sheetTitle'), def.title(arg)); setText($('#sheetSub'), def.sub ? def.sub(S.o, arg) : '');
  try { ctx.update = def.build(body, arg, ctx) || null; dmScan(body); ctx.update && ctx.update(S.o); } catch (e) { console.error('[fnmonitor] 抽屉渲染失败', key, e); }
  S.sheet = key;
  $('#sheet').classList.add('on'); $('#scrim').classList.add('on');
  document.documentElement.style.overflow = 'hidden';
  if (!fromRoute) { if (!wasOpen) S.pushed = true; setHash(!wasOpen); }
  if (type === 'cpu' || type === 'mem') Poll.run('procs');
  setTimeout(() => $('#sheet').focus({ preventScroll: true }), 60);
}
function teardownSheet() { if (sheetCtx) { sheetCtx.charts.forEach(c => c.destroy()); sheetCtx = null; } }
function closeSheet(fromRoute) {
  if (!S.sheet) return;
  teardownSheet(); S.sheet = null;
  $('#sheet').classList.remove('on'); $('#scrim').classList.remove('on');
  document.documentElement.style.overflow = '';
  if (!fromRoute) { if (S.pushed) history.back(); else setHash(false); }
  S.pushed = false;
  if (lastFocus && lastFocus.focus && document.contains(lastFocus)) lastFocus.focus({ preventScroll: true });
}
$('#sheetClose').addEventListener('click', () => closeSheet());
$('#scrim').addEventListener('click', () => closeSheet());


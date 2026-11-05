
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
          html += row(st, `硬盘 ${esc(diskLabel(d))}`, isSleep(d) ? '休眠中，未唤醒检测' : `${esc(sm.text)}${ok(d.temp) ? ` · ${F.int(d.temp)}℃` : ''}${+d.total > 0 ? ` · 已用 ${F.int(d.percent)}%` : ''}`, 'disk/' + encodeURIComponent(d.name));
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
          x.disks.innerHTML = dd.length ? dd.map(d => `<div class="row-i tap-row" data-sheet="disk/${esc(encodeURIComponent(d.name))}" style="grid-template-columns:auto minmax(0,1fr) 120px auto"><span class="drive-ico" style="width:36px;height:36px">${ic(diskIcon(d))}</span><div style="min-width:0"><b style="font-weight:600">${esc(diskLabel(d))}</b> ${diskSlot(d) != null ? `<span class="faint mono">${esc(d.name)}</span> ` : ''}<span class="faint">${esc(vendorModel(d))}</span><div class="sub" data-io="${esc(d.name)}"></div></div><div><div class="dbar" data-n="20" data-b="${esc(d.name)}"></div><div class="sub tnum" style="text-align:right;margin-top:4px" data-p="${esc(d.name)}"></div></div>${ic('chev', 'i-sm faint')}</div>`).join('') : `<div class="row-i">${emptyHTML('未检测到物理硬盘', '需要 lsblk 支持')}</div>`;
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
        setHTML(x.sb, sb.length ? kvHTML(sb.map(z => [esc(diskLabelByName(o, z.name)), z.state === 'standby' ? `${ic('moon', 'i-sm')} 休眠` : z.state === 'active/idle' ? '活动' : '<span class="faint">未知</span>'])) : `<div class="card pad">${emptyHTML('无硬盘休眠数据')}</div>`);
      };
    },
  },
  disk: {
    icon: a => { const d = disksOf(S.o).find(z => z.name === a); return d ? diskIcon(d) : 'hdd'; },
    /* 标题显示物理位置（插槽 6），副标题补设备名与型号：拔盘时按槽位找，不用再去
       系统里查 sdk 是第几个。拿不到槽位时标题退回设备名，副标题只留型号。 */
    title: a => '硬盘 ' + diskLabelByName(S.o, a),
    sub: (o, a) => { const d = disksOf(o).find(z => z.name === a); return d ? [diskSlot(d) != null ? d.name : '', vendorModel(d)].filter(Boolean).join(' · ') : ''; },
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
        const slotState = (S.o && S.o.idrac_slot_state) || 'off';
        const slot = dd.slot != null
          ? `<b class="mono" title="${slotTip}，对照 iDRAC 物理盘列表第 ${F.int(dd.slot)} 项">插槽 ${F.int(dd.slot)}</b> <span class="faint">iDRAC</span>`
          : slotState === 'warming'
            ? '<span class="faint" title="iDRAC 明细正在后台读取，首次约 40~60 秒；读到后这里会自动出现槽位号，不用刷新">读取中…</span>'
            : slotState === 'ready'
              ? '<span class="faint" title="iDRAC 明细里没有这块盘：可能不在阵列卡下，或序列号对不上">—</span>'
              : '<span class="faint" title="iDRAC 未启用，读不到机箱槽位（设置 → iDRAC 里填地址和账号即可）">—</span>';
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
  /* 巡检流水：每次巡检留一行。有变化记变化内容，没变化记「未见异常」。
     「未见异常」7 天后自动清；有变化的永久保留，只能手动清。
     数据来自 /api/scan_log（只读本机 sqlite，不触发任何 smartctl）。 */
  scanlog: {
    icon: () => 'list', title: () => '巡检历史', sub: () => '每次巡检一行 · 有变化记详情，没变化记未见异常',
    build(b) {
      b.innerHTML = `<div class="card pad scanlog-bar"><span class="faint" data-ref="sum" style="flex:1;min-width:140px"></span><button class="btn sm ghost" type="button" data-clear="ok">${ic('eye-off')}清理未见异常</button><button class="btn sm ghost" type="button" data-clear="all">${ic('x')}清空全部</button></div>
        <div data-ref="list" style="margin-top:10px"></div>
        <p class="sub">每次巡检固定留一条记录：分级或计数（重映射扇区 / 待映射扇区 / 不可纠正错误）有变化就写清哪块盘、从多少变到多少；没有变化就记「未见异常」。温度、通电时长这类天天在动的量不算变化。未见异常的记录 7 天后自动清理，有变化的记录不会自动清理，只有点上面的按钮才删。</p>`;
      const x = collect(b), st = { rows: null, err: '', loading: false };
      const ago = ts => {
        const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
        if (s < 90) return '刚刚';
        if (s < 3600) return Math.round(s / 60) + ' 分钟前';
        if (s < 86400) return Math.round(s / 3600) + ' 小时前';
        return Math.round(s / 86400) + ' 天前';
      };
      const one = r => {
        const ch = !!r.changed, txt = r.summary || (ch ? '有变化' : '未见异常');
        return `<div class="row-i" style="grid-template-columns:auto 1fr auto">
          <span class="${ch ? 'st-warn' : 'st-ok'}">${ic(ch ? 'alert' : 'check-c')}</span>
          <div style="min-width:0"><b style="font-weight:600;${ch ? '' : 'color:var(--ink-2)'}">${esc(txt)}</b>
            <div class="sub">${esc(F.mdhm(r.ts))} · ${esc(ago(r.ts))}${+r.total ? ` · ${+r.total} 块盘` : ''}</div></div>
          <span class="pill ${ch ? 'warn' : 'ok'}">${ch ? '有变化' : '未见异常'}</span></div>`;
      };
      const draw = () => {
        if (!st.rows && st.loading) setHTML(x.list, `<div class="card pad">${emptyHTML('正在读取巡检流水…', '', 'clock')}</div>`);
        else if (st.err) setHTML(x.list, `<div class="card pad">${emptyHTML('读取失败', esc(st.err))}</div>`);
        else if (!st.rows || !st.rows.length) setHTML(x.list, `<div class="card pad">${emptyHTML('还没有巡检记录', '下一次巡检完成后这里会出现第一条记录', 'list')}</div>`);
        else setHTML(x.list, rowsCard(st.rows.map(one).join('')));
        const n = (st.rows || []).length, chg = (st.rows || []).filter(r => r.changed).length;
        setText(x.sum, st.rows ? `${n} 条记录${chg ? ` · ${chg} 条有变化` : ''}` : st.loading ? '读取中…' : '');
      };
      const load = async () => {
        st.loading = true; draw();
        try { const j = await API.get('/api/scan_log?limit=200'); st.rows = (j && j.items) || []; st.err = ''; }
        catch (e) { st.err = String((e && e.message) || e); }
        st.loading = false; draw();
      };
      b.addEventListener('click', async e => {
        const t = e.target.closest('[data-clear]'); if (!t) return;
        const onlyOk = t.dataset.clear === 'ok';
        if (!confirm(onlyOk ? '清理所有「未见异常」记录？有变化的记录会保留。' : '清空全部巡检记录？有变化的记录也会一起删掉，无法恢复。')) return;
        t.disabled = true;
        try {
          const j = await API.post('/api/scan_log/clear', onlyOk ? { only_ok: true } : {});
          toast((j && j.message) || '已清理', { key: 'scanlog', icon: 'check' });
          await load();
        } catch (err) { toast('清理失败 · ' + String((err && err.message) || err).slice(0, 60), { icon: 'alert', ms: 4000 }); }
        t.disabled = false;
      });
      load();
      // 巡检刚跑完（last_ts 变了）就悄悄刷新一次，不用手动关掉重开
      let seen = (S.diskHealth && S.diskHealth.last_ts) || 0;
      return () => {
        const lt = (S.diskHealth && S.diskHealth.last_ts) || 0;
        if (lt && lt !== seen) { seen = lt; load(); }
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
  notifyguide: {
    icon: () => 'bell', title: () => '推送渠道设置指南', sub: () => '9 种渠道的配置方法与消息模板',
    build(b) {
      const chs = [
        { name: '邮件（SMTP）', icon: 'mail', key: ['SMTP 服务器', '发件账号', '授权码（非登录密码）', '收件人'], get: '邮箱网页端 → 设置 → 开启 SMTP 服务 → 生成授权码' },
        { name: 'Server酱', icon: 'bell', key: ['SendKey（SCT 开头）'], get: 'sct.ftqq.com 注册 → 关注「方糖」公众号 → 复制 SendKey' },
        { name: '企业微信群机器人', icon: 'chat', key: ['Webhook 地址'], get: '群设置 → 群机器人 → 添加 → 自定义 → 复制 Webhook' },
        { name: '钉钉群机器人', icon: 'chat', key: ['Webhook 地址', '加签密钥 SEC…（选加签时）'], get: '群设置 → 智能群助手 → 添加机器人 → 自定义 → 选加签安全设置 → 复制 Webhook 和密钥' },
        { name: '飞书群机器人', icon: 'chat', key: ['Webhook 地址'], get: '群设置 → 群机器人 → 添加机器人 → 自定义 → 复制 Webhook' },
        { name: 'Bark', icon: 'bell', key: ['推送地址'], get: 'iOS 安装 Bark App → 首页复制服务器地址（api.day.app/xxx）' },
        { name: 'PushPlus', icon: 'send', key: ['Token'], get: 'pushplus.plus 微信扫码登录 → 发送消息页复制 Token' },
        { name: '通用 Webhook', icon: 'link', key: ['URL', '可选 method/headers/body 模板'], get: '自行搭建的接收端；默认 POST JSON {"title","content"}' },
        { name: '企业微信应用', icon: 'chat', key: ['CorpID', 'Secret', 'AgentId', '接收人（默认 @all）'], get: 'work.weixin.qq.com → 应用管理 → 自建应用 → 获取 CorpID/Secret/AgentId' },
      ];
      const rows = chs.map(c => `<div class="card" style="margin-bottom:10px"><div style="font-weight:600;margin-bottom:6px">${c.name}</div><div style="font-size:12px;color:var(--ink-2);line-height:1.7"><b>需要填写：</b>${c.key.join('、')}<br><b>获取方式：</b>${c.get}</div></div>`).join('');
      b.innerHTML = `<div class="card" style="margin-bottom:12px"><div style="font-weight:600;margin-bottom:6px">消息模板占位符</div><div style="font-size:12px;color:var(--ink-2);line-height:1.8"><code style="background:var(--bg-2);padding:1px 5px;border-radius:3px">{title}</code> 告警标题　<code style="background:var(--bg-2);padding:1px 5px;border-radius:3px">{content}</code> 告警正文　<code style="background:var(--bg-2);padding:1px 5px;border-radius:3px">{text}</code> 正文别名　<code style="background:var(--bg-2);padding:1px 5px;border-radius:3px">{time}</code> 当前时间</div></div>
      <div class="card" style="margin-bottom:12px"><div style="font-weight:600;margin-bottom:6px">常用模板示例</div><pre style="font-size:12px;color:var(--ink-2);white-space:pre-wrap;margin:0;line-height:1.7">{title}

{content}

告警时间：{time}</pre></div>
      ${rows}
      <div class="card"><div style="font-size:12px;color:var(--ink-3);line-height:1.7">提示：每个渠道卡片都有「测试」按钮，保存后可立即验证配置是否正确。全局设置（标题前缀 / 极简推送 / 勿扰模式）对所有渠道生效。</div></div>`;
    },
  },

  monitor: {
    icon: () => 'eye', title: () => '事件监控', sub: () => 'fnOS 系统事件实时推送 · 数据源路径自动探测',
    build(body, arg, ctx) {
    body.innerHTML = `
    <style>
    .mm-cats { display:flex; flex-direction:column; gap:8px; max-height:min(52vh,480px); overflow-y:auto; }
    .mm-cat { flex:0 0 auto; border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden; }
    .mm-cat-head { display:flex; align-items:center; gap:8px; padding:9px 12px; cursor:pointer; background:var(--fill-1); user-select:none; }
    .mm-cat-name { font-weight:550; font-size:12.5px; color:var(--ink-1); }
    .mm-cat-num { font-size:11px; color:var(--ink-3); }
    .mm-cat-all { margin-left:auto; font-size:11.5px; font-weight:550; color:var(--d-p); padding:2px 8px; border-radius:var(--r-pill); }
    .mm-cat-all:hover { background:var(--ok-soft); }
    .mm-cat-arrow { font-size:10px; color:var(--ink-3); transition:transform .15s; }
    .mm-cat.open .mm-cat-arrow { transform:rotate(180deg); }
    .mm-cat-body { display:none; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:6px; padding:10px 12px; }
    .mm-cat.open .mm-cat-body { display:grid; }
    .mm-chip { display:flex; align-items:center; gap:6px; padding:6px 9px; border-radius:8px; font-size:12px; cursor:pointer; background:var(--fill-1); color:var(--ink-2); border:1px solid transparent; }
    .mm-chip:hover { background:var(--fill-2); }
    .mm-chip.on { background:var(--ok-soft); color:var(--ink-1); border-color:color-mix(in srgb,var(--d-p) 28%,transparent); }
    .mm-chip input { margin:0; flex-shrink:0; }
    .mm-chip .mt { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .mm-diag { padding:9px 12px; border-radius:var(--r-md); font-size:12.5px; margin-bottom:6px; }
    .mm-diag.ok { background:var(--ok-soft); color:var(--ok); }
    .mm-diag.fail { background:var(--crit-soft); color:var(--crit); }
    .mm-diag-hint { font-size:11px; margin-top:4px; white-space:pre-wrap; line-height:1.6; }
    </style>
    <div class="note info">${ic('eye')}<span><b>fnOS 系统事件实时推送</b>：统一经事件模板化后走「通知设置」里配置的推送渠道；改动后点底部「保存并启动」生效。</span></div>
    ${sec('基础')}
    <div class="card pad" style="padding-top:6px;padding-bottom:6px">
    <div class="set-row inline"><div><div class="lbl">启用监控</div><div class="hint">关闭后所有轮询器停止，事件不再推送</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-enable" aria-checked="false" aria-label="启用监控"></button></div></div>
    <div class="set-row"><div><div class="lbl">轮询间隔</div><div class="hint">主日志库 / 备份库 / SSH journal 的读取周期，3–60 秒</div></div><div class="ctl"><input type="number" id="mm-poll" class="textfield mono" min="3" max="60" value="5" style="width:86px" aria-label="轮询间隔（秒）"><span class="faint">秒</span></div></div>
    </div>
    ${sec('数据源路径', '<span class="faint">右侧开关控制是否启用该数据源，关闭后即使路径有值也不会启动对应轮询器；路径留空同样视为不启用</span>')}
    <div class="card pad" style="padding-top:6px;padding-bottom:6px">
    <div class="set-row"><div><div class="lbl">fnOS 主日志库</div><div class="hint">系统登录 / 文件操作等主事件</div></div><div class="ctl"><input type="text" id="mm-logger" class="textfield mono" placeholder="/usr/trim/var/eventlogger_service/logger_data.db3" style="width:min(280px,42vw)" spellcheck="false" aria-label="fnOS 主日志库路径"><button class="switch" type="button" role="switch" id="mm-src-logger" aria-checked="true" aria-label="启用 fnOS 主日志库"></button></div></div>
    <div class="set-row"><div><div class="lbl">备份任务库</div><div class="hint">备份任务开始 / 完成 / 失败</div></div><div class="ctl"><input type="text" id="mm-backup" class="textfield mono" placeholder="/usr/trim/var/backup_service/basic_backup.db3" style="width:min(280px,42vw)" spellcheck="false" aria-label="备份任务库路径"><button class="switch" type="button" role="switch" id="mm-src-backup" aria-checked="true" aria-label="启用备份任务库"></button></div></div>
    <div class="set-row"><div><div class="lbl">影视库（资源）</div><div class="hint">媒体入库与刮削事件</div></div><div class="ctl"><input type="text" id="mm-trim" class="textfield mono" placeholder="/usr/local/apps/@appdata/trim.media/database/trimmedia.db" style="width:min(280px,42vw)" spellcheck="false" aria-label="影视资源库路径"><button class="switch" type="button" role="switch" id="mm-src-trim" aria-checked="true" aria-label="启用影视库（资源）"></button></div></div>
    <div class="set-row"><div><div class="lbl">影视库（活动）</div><div class="hint">播放 / 观看活动事件</div></div><div class="ctl"><input type="text" id="mm-trimact" class="textfield mono" placeholder="/usr/local/apps/@appdata/trim.media/database/trimactivity.db" style="width:min(280px,42vw)" spellcheck="false" aria-label="影视活动库路径"><button class="switch" type="button" role="switch" id="mm-src-trimact" aria-checked="true" aria-label="启用影视库（活动）"></button></div></div>
    <div class="set-row"><div><div class="lbl">相册库</div><div class="hint">照片备份与相册变更</div></div><div class="ctl"><input type="text" id="mm-photo" class="textfield mono" placeholder="/usr/local/apps/@appdata/trim.photos/db/photo.db" style="width:min(280px,42vw)" spellcheck="false" aria-label="相册库路径"><button class="switch" type="button" role="switch" id="mm-src-photo" aria-checked="true" aria-label="启用相册库"></button></div></div>
    <div class="set-row"><div><div class="lbl">音乐库</div><div class="hint">音乐库变更</div></div><div class="ctl"><input type="text" id="mm-music" class="textfield mono" placeholder="/usr/local/apps/@appdata/trim.music/db/music.db" style="width:min(280px,42vw)" spellcheck="false" aria-label="音乐库路径"><button class="switch" type="button" role="switch" id="mm-src-music" aria-checked="true" aria-label="启用音乐库"></button></div></div>
    <div class="set-row"><div><div class="lbl">Docker Socket</div><div class="hint">容器启动 / 停止 / 健康检查事件</div></div><div class="ctl"><input type="text" id="mm-docker-sock" class="textfield mono" placeholder="/var/run/docker.sock" style="width:min(280px,42vw)" spellcheck="false" aria-label="Docker Socket 路径"><button class="switch" type="button" role="switch" id="mm-src-docker" aria-checked="true" aria-label="启用 Docker 事件监控"></button></div></div>
    </div>
    ${sec('轮询器')}
    <div class="card pad" style="padding-top:6px;padding-bottom:6px">
    <div class="set-row inline"><div><div class="lbl">SSH journal 日志</div><div class="hint">从 systemd journal 读取 SSH 登录 / 断开</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-en-ssh-jrn" aria-checked="true" aria-label="SSH journal 日志"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">SSH 开启超时提醒</div><div class="hint">会话持续超过设定分钟数时推送</div></div><div class="ctl"><input type="number" id="mm-ssh-min" class="textfield mono" min="5" max="300" value="30" style="width:72px" aria-label="超过多少分钟提醒"><span class="faint">分钟</span><button class="switch" type="button" role="switch" id="mm-en-ssh-open" aria-checked="true" aria-label="SSH 开启超时提醒"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">WAN IP 变更</div><div class="hint">公网 IP 变化时推送</div></div><div class="ctl"><input type="number" id="mm-wan-int" class="textfield mono" min="60" max="86400" value="600" style="width:86px" aria-label="WAN IP 轮询间隔（秒）"><span class="faint">秒</span><button class="switch" type="button" role="switch" id="mm-en-wan" aria-checked="true" aria-label="WAN IP 变更"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">忽略 SSH 回环</div><div class="hint">本机 127.0.0.1 的连接不计入</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-ssh-loop" aria-checked="true" aria-label="忽略 SSH 回环"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">忽略 SSH 局域网</div><div class="hint">内网 IP 的连接不计入</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-ssh-lan" aria-checked="false" aria-label="忽略 SSH 局域网"></button></div></div>
    </div>
    ${sec('推送聚合', '<span class="faint">批量操作会集中产生大量同类事件；开启后按窗口 / 静默期合成一条。登录、安全、存储故障、资源告警、UPS、备份结果等事件始终即时推送</span>')}
    <div class="card pad" style="padding-top:6px;padding-bottom:6px">
    <div class="set-row inline"><div><div class="lbl">同类事件合并推送</div><div class="hint">文件删除 / 应用启停 / Docker / 相册 / 音乐收藏等同类事件在窗口内合并（需先在「事件类型」里勾选对应事件）</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-agg" aria-checked="false" aria-label="同类事件合并推送"></button></div></div>
    <div class="set-row"><div><div class="lbl">合并窗口</div><div class="hint">窗口内发生的同类事件合成一条推送，5–3600 秒</div></div><div class="ctl"><input type="number" id="mm-agg-win" class="textfield mono" min="5" max="3600" value="30" style="width:86px" aria-label="同类事件合并窗口（秒）"><span class="faint">秒</span></div></div>
    <div class="set-row inline"><div><div class="lbl">磁盘唤醒 / 休眠合并</div><div class="hint">同一窗口内的磁盘唤醒（或休眠）事件合并为一条</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-disk-agg" aria-checked="true" aria-label="磁盘唤醒休眠合并"></button></div></div>
    <div class="set-row"><div><div class="lbl">磁盘合并窗口</div><div class="hint">磁盘唤醒 / 休眠的合并时间窗口，5–3600 秒</div></div><div class="ctl"><input type="number" id="mm-disk-win" class="textfield mono" min="5" max="3600" value="60" style="width:86px" aria-label="磁盘合并窗口（秒）"><span class="faint">秒</span></div></div>
    <div class="set-row inline"><div><div class="lbl">影视 / 音乐静默汇总</div><div class="hint">入库、刮削、曲目入库 / 移出连续静默后合成一条「本轮汇总」；收藏与加入歌单默认即时推送</div></div><div class="ctl"><button class="switch" type="button" role="switch" id="mm-media-agg" aria-checked="true" aria-label="影视音乐静默汇总"></button></div></div>
    <div class="set-row"><div><div class="lbl">静默判定</div><div class="hint">连续多少秒没有新记录后合成汇总，10–600 秒</div></div><div class="ctl"><input type="number" id="mm-media-sil" class="textfield mono" min="10" max="600" value="60" style="width:86px" aria-label="影视音乐静默秒数"><span class="faint">秒</span></div></div>
    </div>
    ${sec('事件类型', '<span class="faint" id="mm-selected-count"></span>')}
    <div class="card pad">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
    <input type="search" id="mm-q" class="textfield" placeholder="搜索事件，如 SSH、删除文件、备份" style="flex:1;min-width:150px;width:auto" aria-label="搜索事件">
    <button class="btn sm ghost" id="mm-defaults" type="button">推荐默认</button>
    <button class="btn sm ghost" id="mm-select-all" type="button">全选</button>
    <button class="btn sm ghost" id="mm-clear-all" type="button">清空</button>
    </div>
    <div id="mm-cats-wrap" class="mm-cats"></div>
    <p class="sub" style="margin:8px 0 0">轮询周期：主日志库 / 备份库 / SSH journal 用上方「轮询间隔」；影视 / 相册 / 音乐为其 2 倍（最少 10 秒）；SSH 开启检测 120 秒；WAN IP 600 秒。</p>
    </div>
    <div id="mm-diag-wrap" style="display:none">
    ${sec('诊断')}
    <div class="card pad" id="mm-diag-list" style="padding-top:10px;padding-bottom:4px"></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <button class="btn primary" id="mm-save" type="button">${ic('play')}保存并启动</button>
    <button class="btn" id="mm-status" type="button">${ic('pulse')}运行状态</button>
    <button class="btn" id="mm-test" type="button">${ic('bell')}测试推送</button>
    <button class="btn danger" id="mm-disable" type="button">${ic('power')}全部关闭</button>
    </div>
    <div id="mm-msg" class="sub" style="margin-top:10px"></div>
    `;

    // ===== 工具：body 内查找（比全局 $ 精确） =====
    const $b = (sel) => body.querySelector(sel);
    const $$b = (sel) => body.querySelectorAll(sel);
    const setSw = (sel, on) => { const el = $b(sel); if (el) el.setAttribute('aria-checked', on ? 'true' : 'false'); };
    const swOn = (sel) => { const el = $b(sel); return !!el && el.getAttribute('aria-checked') === 'true'; };
    $$b('[role=switch]').forEach(el => el.addEventListener('click', () => {
    el.setAttribute('aria-checked', el.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
    }));

    // ===== 事件绑定（先绑定！即使后面数据填充 throw，按钮也能用） =====
    async function doMonitorSave() {
    try {
    const events = Array.from($$b('#mm-cats-wrap input[type=checkbox]:checked')).map(cb => cb.value);
    const payload = {
    enable: swOn('#mm-enable'),
    monitor_events: events,
    poll_interval: parseInt($b('#mm-poll').value) || 5,
    logger_db_path: $b('#mm-logger').value.trim(),
    backup_db_path: $b('#mm-backup').value.trim(),
    trim_media_db_path: $b('#mm-trim').value.trim(),
    trim_activity_db_path: $b('#mm-trimact').value.trim(),
    photo_db_path: $b('#mm-photo').value.trim(),
    music_db_path: $b('#mm-music').value.trim(),
    docker_socket_path: $b('#mm-docker-sock').value.trim(),
    source_enabled: {
    logger_db_path: swOn('#mm-src-logger'),
    backup_db_path: swOn('#mm-src-backup'),
    trim_media_db_path: swOn('#mm-src-trim'),
    trim_activity_db_path: swOn('#mm-src-trimact'),
    photo_db_path: swOn('#mm-src-photo'),
    music_db_path: swOn('#mm-src-music'),
    },
    enable_docker: swOn('#mm-src-docker'),
    enable_ssh_journal: swOn('#mm-en-ssh-jrn'),
    enable_ssh_open_watch: swOn('#mm-en-ssh-open'),
    ssh_open_remind_minutes: parseInt($b('#mm-ssh-min').value) || 30,
    enable_wan_ip: swOn('#mm-en-wan'),
    wan_ip_poll_interval: parseInt($b('#mm-wan-int').value) || 600,
    ignore_ssh_loopback: swOn('#mm-ssh-loop'),
    ignore_ssh_lan: swOn('#mm-ssh-lan'),
    aggregate_enabled: swOn('#mm-agg'),
    aggregate_window: parseInt($b('#mm-agg-win').value) || 30,
    disk_aggregate_enabled: swOn('#mm-disk-agg'),
    disk_aggregate_window: parseInt($b('#mm-disk-win').value) || 60,
    media_aggregate_enabled: swOn('#mm-media-agg'),
    media_silence_window: parseInt($b('#mm-media-sil').value) || 60,
    };
    setText($b('#mm-msg'), '保存中…');
    const r = await API.post('/api/monitor/save', payload);
    if (!r.ok) { setText($b('#mm-msg'), '保存失败：' + (r.error || '')); return; }
    // 启停现在由后台线程完成，状态可能要等一小会儿才反映出来：轮询几次直到一致
    let runTxt = '';
    let st = null;
    for (let i = 0; i < 6; i++) {
    try { st = await API.get('/api/monitor/status'); } catch (e2) { st = null; }
    if (!payload.enable) break;
    if (st && st.started) break;
    await new Promise(res => setTimeout(res, 400));
    }
    if (st) {
    const runN = Object.values(st.pollers || {}).filter(v => v).length;
    runTxt = `（started=${st.started}，运行中轮询器 ${runN}/${Object.keys(st.pollers || {}).length}）`;
    }
    setText($b('#mm-msg'), '✅ 已保存，监控运行时已' + (payload.enable ? '启动' : '关闭') + runTxt);
    if (r.diagnostics && r.diagnostics.length) {
    $b('#mm-diag-wrap').style.display = '';
    $b('#mm-diag-list').innerHTML = r.diagnostics.map(d => {
    const hint = d.ok ? '' : `<div class="mm-diag-hint">${d.error || ''}\n${d.hint || ''}</div>`;
    return `<div class="mm-diag ${d.ok ? 'ok' : 'fail'}">${d.ok ? '✓ ' : '✗ '}${d.label}${hint}</div>`;
    }).join('');
    }
    } catch (e) { console.error('[monitor] save error', e); setText($b('#mm-msg'), '保存请求异常：' + e.message); }
    }

    // 「保存并启动」= 拨开开关再保存（避免忘了拨开关导致保存后监控仍是关闭）
    $b('#mm-save').addEventListener('click', () => { setSw('#mm-enable', true); doMonitorSave(); });

    $b('#mm-status').addEventListener('click', async () => {
    try {
    const r = await API.get('/api/monitor/status');
    let chLine = '';
    try {
    const n = await API.get('/api/notify');
    const chs = Array.isArray(n.channels) ? n.channels : [];
    const on = chs.filter(c => c.enabled).length;
    chLine = `<div style="padding:4px 0;font-size:12px;color:${on ? 'var(--ok)' : 'var(--crit)'};">通知渠道：已启用 ${on} / 共 ${chs.length}${on ? '' : '（没有已启用渠道时事件不会推送，请先到「通知设置」配置并启用一个渠道）'}</div>`;
    } catch (e) { chLine = ''; }
    const items = Object.entries(r.pollers || {});
    const html = items.map(([k, v]) => `<div class="row-i" style="grid-template-columns:1fr auto;padding:9px 0"><span style="font-size:12.5px">${esc(k)}</span><span class="pill ${v ? 'ok' : ''}" style="height:22px">${v ? '<span class="dot"></span>运行中' : '未启动'}</span></div>`).join('');
    $b('#mm-diag-wrap').style.display = '';
    $b('#mm-diag-list').innerHTML = `<div class="eyebrow" style="margin:4px 0 2px">运行状态 · started=${r.started}</div>${chLine}${html}`;
    } catch (e) { console.error('[monitor] status error', e); setText($b('#mm-msg'), '状态查询异常：' + e.message); }
    });

    $b('#mm-disable').addEventListener('click', async () => {
    setSw('#mm-enable', false);
    doMonitorSave();
    });

    // 测试推送：直接走通知渠道（绕过事件检测），用于确认渠道本身能发消息
    $b('#mm-test').addEventListener('click', async () => {
    setText($b('#mm-msg'), '正在发送测试推送…');
    try {
    const r = await API.post('/api/notify/test', {});
    if (r.ok) {
    const res = r.channels || r.results || r.result || {};
    const ok = Object.values(res).filter(x => x && x.ok).length;
    const total = Object.keys(res).length;
    setText($b('#mm-msg'), `✅ 测试推送完成：成功 ${ok}/${total}${total ? '' : '（无已启用渠道）'}`);
    } else {
    setText($b('#mm-msg'), '❌ 测试推送失败：' + (r.error || r.message || '未知错误'));
    }
    } catch (e) { setText($b('#mm-msg'), '❌ 测试推送异常：' + e.message); }
    });

    // ===== 数据填充（异步执行，独立 try-catch，throw 只影响数据不影响按钮） =====
    (async () => {
    try {
    const resp = await API.get('/api/monitor');
    if (!resp.ok) throw new Error(resp.error || 'backend_not_ok');
    const cfg = resp.config || {};
    const cats = Array.isArray(resp.categories) ? resp.categories : [];
    const defaults = Array.isArray(resp.default_selected) ? resp.default_selected : [];

    // 填充已有配置
    setSw('#mm-enable', cfg.enable === true);
    if (cfg.poll_interval) $b('#mm-poll').value = cfg.poll_interval;
    if (cfg.logger_db_path) $b('#mm-logger').value = cfg.logger_db_path;
    if (cfg.backup_db_path) $b('#mm-backup').value = cfg.backup_db_path;
    if (cfg.trim_media_db_path) $b('#mm-trim').value = cfg.trim_media_db_path;
    if (cfg.trim_activity_db_path) $b('#mm-trimact').value = cfg.trim_activity_db_path;
    if (cfg.photo_db_path) $b('#mm-photo').value = cfg.photo_db_path;
    if (cfg.music_db_path) $b('#mm-music').value = cfg.music_db_path;
    if (cfg.docker_socket_path) $b('#mm-docker-sock').value = cfg.docker_socket_path;
    const srcOn = cfg.source_enabled || {};
    setSw('#mm-src-logger', srcOn.logger_db_path !== false);
    setSw('#mm-src-backup', srcOn.backup_db_path !== false);
    setSw('#mm-src-trim', srcOn.trim_media_db_path !== false);
    setSw('#mm-src-trimact', srcOn.trim_activity_db_path !== false);
    setSw('#mm-src-photo', srcOn.photo_db_path !== false);
    setSw('#mm-src-music', srcOn.music_db_path !== false);
    setSw('#mm-src-docker', cfg.enable_docker !== false);
    setSw('#mm-en-ssh-jrn', cfg.enable_ssh_journal !== false);
    setSw('#mm-en-ssh-open', cfg.enable_ssh_open_watch !== false);
    if (cfg.ssh_open_remind_minutes) $b('#mm-ssh-min').value = cfg.ssh_open_remind_minutes;
    setSw('#mm-en-wan', cfg.enable_wan_ip !== false);
    if (cfg.wan_ip_poll_interval) $b('#mm-wan-int').value = cfg.wan_ip_poll_interval;
    setSw('#mm-ssh-loop', cfg.ignore_ssh_loopback !== false);
    setSw('#mm-ssh-lan', !!cfg.ignore_ssh_lan);
    setSw('#mm-agg', !!cfg.aggregate_enabled);
    if (cfg.aggregate_window) $b('#mm-agg-win').value = cfg.aggregate_window;
    setSw('#mm-disk-agg', cfg.disk_aggregate_enabled !== false);
    if (cfg.disk_aggregate_window) $b('#mm-disk-win').value = cfg.disk_aggregate_window;
    setSw('#mm-media-agg', cfg.media_aggregate_enabled !== false);
    if (cfg.media_silence_window) $b('#mm-media-sil').value = cfg.media_silence_window;

    // 已保存过就按保存的来（空数组=用户主动清空，不能又回填成默认）
    const selected = new Set(
    Array.isArray(cfg.monitor_events) ? cfg.monitor_events : defaults
    );

    const catsWrap = $b('#mm-cats-wrap');
    catsWrap.innerHTML = cats.map((c, ci) => {
    const evs = (Array.isArray(c.events) && c.events.length)
    ? c.events
    : (Array.isArray(c.ids) ? c.ids : []).map(id => ({ id: id, title: id, recommended: false }));
    const label = c.label || ('分类 ' + (ci + 1));
    return `<div class="mm-cat open" data-ci="${ci}">
    <div class="mm-cat-head">
    <span class="mm-cat-name">${label}</span>
    <span class="mm-cat-num" data-num="${ci}"></span>
    <span class="mm-cat-all" data-all="${ci}">全选</span>
    <span class="mm-cat-arrow">▾</span>
    </div>
    <div class="mm-cat-body">
    ${evs.map(e => `<label class="mm-chip${selected.has(e.id) ? ' on' : ''}" data-text="${(e.id + ' ' + (e.title || '')).toLowerCase()}">
    <input type="checkbox" value="${e.id}" ${selected.has(e.id) ? 'checked' : ''}>
    <span class="mt">${e.title || e.id}</span>
    </label>`).join('')}
    </div>
    </div>`;
    }).join('');

    const allCbs = () => Array.from($$b('#mm-cats-wrap input[type=checkbox]'));
    function syncUi() {
    allCbs().forEach(cb => {
    const chip = cb.parentElement;
    if (chip) chip.classList.toggle('on', cb.checked);
    });
    const total = allCbs().length;
    const sel = allCbs().filter(cb => cb.checked).length;
    setText($b('#mm-selected-count'), `已选 ${sel} / ${total}`);
    cats.forEach((c, ci) => {
    const cbs = Array.from($$b(`#mm-cats-wrap [data-ci="${ci}"] input[type=checkbox]`));
    const n = cbs.filter(cb => cb.checked).length;
    const numEl = $b(`#mm-cats-wrap [data-num="${ci}"]`);
    const allEl = $b(`#mm-cats-wrap [data-all="${ci}"]`);
    if (numEl) setText(numEl, `${n}/${cbs.length}`);
    if (allEl) setText(allEl, n === cbs.length ? '取消全选' : '全选');
    });
    }
    syncUi();
    catsWrap.addEventListener('change', syncUi);

    // 分类头：点击折叠；「全选 / 取消全选」只作用于该分类
    catsWrap.addEventListener('click', (ev) => {
    const allEl = ev.target.closest('[data-all]');
    if (allEl) {
    ev.stopPropagation();
    const ci = allEl.getAttribute('data-all');
    const cbs = Array.from($$b(`#mm-cats-wrap [data-ci="${ci}"] input[type=checkbox]`));
    const shouldCheck = cbs.filter(cb => cb.checked).length < cbs.length;
    cbs.forEach(cb => { cb.checked = shouldCheck; });
    syncUi();
    return;
    }
    const headEl = ev.target.closest('.mm-cat-head');
    if (headEl) headEl.parentElement.classList.toggle('open');
    });

    // 搜索：按中文标题或事件 ID 过滤，命中的分类自动展开
    const qEl = $b('#mm-q');
    if (qEl) qEl.addEventListener('input', () => {
    const q = qEl.value.trim().toLowerCase();
    $$b('#mm-cats-wrap .mm-cat').forEach(card => {
    let shown = 0;
    card.querySelectorAll('.mm-chip').forEach(chip => {
    const hit = !q || (chip.getAttribute('data-text') || '').indexOf(q) >= 0;
    chip.style.display = hit ? '' : 'none';
    if (hit) shown++;
    });
    card.style.display = (q && shown === 0) ? 'none' : '';
    if (q && shown > 0) card.classList.add('open');
    });
    });

    $b('#mm-defaults').addEventListener('click', () => {
    allCbs().forEach(cb => { cb.checked = defaults.indexOf(cb.value) >= 0; });
    syncUi();
    });
    $b('#mm-select-all').addEventListener('click', () => { allCbs().forEach(cb => { cb.checked = true; }); syncUi(); });
    $b('#mm-clear-all').addEventListener('click', () => { allCbs().forEach(cb => { cb.checked = false; }); syncUi(); });
    } catch (e) {
    console.error('[monitor] data load error', e);
    setText($b('#mm-msg'), '⚠ 配置加载失败：' + e.message + '（按钮仍可用）');
    }
    })();
    return null;
    },
  },

  pushHistory: {
    icon: () => 'history', title: () => '推送历史与统计', sub: () => '近 1 万条推送记录，可按成功/失败筛选',
    build(body, arg, ctx) {
    body.innerHTML = `
    <style>
    .ph-stat { display:flex; gap:14px; }
    .ph-stat-item { flex:1; text-align:center; padding:12px 0; border-radius:8px; background:var(--bg-2); }
    .ph-stat-item .v { font-size:20px; font-weight:600; }
    .ph-stat-item .l { font-size:12px; color:var(--ink-2); margin-top:2px; }
    .ph-list { margin-top:14px; }
    .ph-row { display:flex; gap:8px; padding:8px 10px; border-bottom:1px solid var(--bg-3); font-size:12px; cursor:pointer; }
    .ph-row:hover { background:var(--bg-2); }
    .ph-row .ok { color:#07c160; width:24px; flex-shrink:0; }
    .ph-row .bad { color:#ff4d4f; width:24px; flex-shrink:0; }
    .ph-row .t { width:150px; color:var(--ink-2); flex-shrink:0; }
    .ph-row .e { width:130px; color:var(--ink-2); flex-shrink:0; }
    .ph-row .s { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .ph-head { display:flex; gap:8px; padding:6px 10px; font-size:11px; color:var(--ink-2); font-weight:600; border-bottom:2px solid var(--bg-3); }
    .ph-head .ok { width:24px; }
    .ph-head .t { width:150px; }
    .ph-head .e { width:130px; }
    .ph-head .s { flex:1; }
    .ph-detail { padding:10px; background:var(--bg-2); border-radius:6px; font-size:12px; white-space:pre-wrap; margin-top:4px; display:none; }
    .ph-filter { display:flex; gap:10px; align-items:center; margin-bottom:10px; font-size:12px; }
    .ph-filter select { padding:4px 8px; border-radius:4px; background:var(--bg-2); color:var(--ink-1); border:1px solid var(--bg-3); }
    .ph-pager { display:flex; justify-content:space-between; align-items:center; margin-top:10px; font-size:12px; color:var(--ink-2); }
    .ph-pager button { padding:4px 12px; border-radius:4px; border:1px solid var(--bg-3); background:var(--bg-2); color:var(--ink-1); cursor:pointer; }
    .ph-pager button:disabled { opacity:0.4; cursor:not-allowed; }
    </style>
    <div class="card">
    <div class="ph-stat" id="ph-stat"></div>
    </div>
    <div class="card">
    <div class="ph-filter">
    <label>筛选：</label>
    <select id="ph-filter">
    <option value="">全部</option>
    <option value="true">仅成功</option>
    <option value="false">仅失败</option>
    </select>
    <span id="ph-count" style="margin-left:auto"></span>
    <button class="btn btn-danger" id="ph-clear" type="button" style="padding:3px 10px; font-size:12px;">清空全部历史</button>
    </div>
    <div class="ph-list">
    <div class="ph-head">
    <div class="ok">状态</div><div class="t">时间</div><div class="e">事件类型</div><div class="s">摘要</div>
    </div>
    <div id="ph-rows"></div>
    </div>
    <div class="ph-pager">
    <button id="ph-prev">上一页</button>
    <span id="ph-page-info"></span>
    <button id="ph-next">下一页</button>
    </div>
    </div>
    `;
    
    let page = 0, limit = 50, filter = '';
    const statEl = $('#ph-stat'), rowsEl = $('#ph-rows'), countEl = $('#ph-count');
    const prevBtn = $('#ph-prev'), nextBtn = $('#ph-next'), pageInfo = $('#ph-page-info');
    
    async function loadStats() {
    try {
    const s = await API.get('/api/notify-stats');
    statEl.innerHTML = `
    <div class="ph-stat-item"><div class="v">${s.total && s.total.total || 0}</div><div class="l">总条数</div></div>
    <div class="ph-stat-item"><div class="v">${s.today && s.today.total || 0}</div><div class="l">今日</div></div>
    <div class="ph-stat-item"><div class="v" style="color:#07c160">${s.total && s.total.success || 0}</div><div class="l">成功</div></div>
    <div class="ph-stat-item"><div class="v" style="color:#ff4d4f">${s.total && s.total.fail || 0}</div><div class="l">失败</div></div>
    `;
    } catch (e) { statEl.innerHTML = '<div style="color:#ff4d4f">加载统计失败</div>'; }
    }
    
    async function loadRows() {
    const qs = new URLSearchParams({ limit, offset: page * limit });
    if (filter) qs.set('success', filter);
    try {
    const rows = await API.get('/api/notify-history?' + qs.toString());
    countEl.textContent = `当前返回 ${rows.length} 条`;
    if (!rows.length && page > 0) { page = 0; return loadRows(); }
    rowsEl.innerHTML = rows.map(r => `
    <div class="ph-row" data-id="${r.id}">
    <div class="${r.success ? 'ok' : 'bad'}">${r.success ? '✓' : '✗'}</div>
    <div class="t">${r.created_at || ''}</div>
    <div class="e">${r.event_type || ''}</div>
    <div class="s" title="${(r.summary || '').replace(/"/g, '&quot;')}">${r.summary || ''}</div>
    </div>
    `).join('') || '<div style="padding:20px;text-align:center;color:var(--ink-3);font-size:13px">暂无推送记录</div>';
    pageInfo.textContent = `第 ${page + 1} 页`;
    prevBtn.disabled = page === 0;
    nextBtn.disabled = rows.length < limit;
    // 行点击展开详情
    rowsEl.querySelectorAll('.ph-row').forEach(row => {
    row.addEventListener('click', async () => {
    const id = row.dataset.id;
    let detail = row.nextElementSibling;
    if (detail && detail.classList.contains('ph-detail')) {
    detail.remove(); return;
    }
    // 先删其它展开
    rowsEl.querySelectorAll('.ph-detail').forEach(el => el.remove());
    detail = document.createElement('div');
    detail.className = 'ph-detail';
    detail.style.display = '';
    try {
    const rec = await API.get('/api/notify-history/' + id);
    if (rec && rec.detail) {
    try { const d = JSON.parse(rec.detail); detail.textContent = JSON.stringify(d, null, 2); } catch (e2) { detail.textContent = rec.detail; }
    } else { detail.textContent = rec ? JSON.stringify({ created_at: rec.created_at, event_type: rec.event_type, summary: rec.summary }, null, 2) : ''; }
    } catch (e) { detail.textContent = '加载详情失败'; }
    row.parentElement.insertBefore(detail, row.nextSibling);
    });
    });
    } catch (e) { rowsEl.innerHTML = '<div style="color:#ff4d4f">加载失败</div>'; }
    }
    
    $('#ph-filter').addEventListener('change', e => { filter = e.target.value; page = 0; loadRows(); });
    prevBtn.addEventListener('click', () => { if (page > 0) { page--; loadRows(); } });
    nextBtn.addEventListener('click', () => { page++; loadRows(); });
    $('#ph-clear').addEventListener('click', async () => {
    if (!confirm('确定清空所有推送历史？此操作不可恢复')) return;
    try { await API.post('/api/history/clear', {}); page = 0; loadRows(); loadStats(); } catch (e) { alert('清空失败'); }
    });
    
    (async () => {
    await loadStats();
    await loadRows();
    })();
    
    // 统计定时刷新（每 10 秒）；抽屉关闭/切换时由 teardownSheet 调用 clearTimer 清理
    const timer = setInterval(loadStats, 10000);
    ctx.clearTimer = () => clearInterval(timer);
    return null;
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
function teardownSheet() { if (sheetCtx) { try { sheetCtx.clearTimer && sheetCtx.clearTimer(); } catch (e) { /* noop */ } sheetCtx.charts.forEach(c => c.destroy()); sheetCtx = null; } }
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


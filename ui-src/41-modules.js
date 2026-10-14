/* =====================================================================
   MODULE FACTORY（可隐藏 / 可拖拽排序的版块；只更新视口内的版块）
   ===================================================================== */
const MODS = {};
const collect = root => { const r = {}; $$('[data-ref]', root).forEach(n => { r[n.dataset.ref] = n; }); return r; };
function runUpdate(m) {
  if (!m.update) return;
  try { m.update(S.o, m); } catch (e) { console.error('[fnmonitor] 版块更新失败', m.id, e); }
}
const visObs = new IntersectionObserver(ens => ens.forEach(en => {
  const m = MODS[en.target.dataset.mod]; if (!m) return;
  m.vis = en.isIntersecting; m.el.classList.toggle('in-view', m.vis);
  if (m.vis && m.dirty && !S.editing) { m.dirty = false; runUpdate(m); }
}), { rootMargin: '240px 0px' });
function defineModule(def) {
  const el = mount(`<section class="module${def.headless ? ' mod-headless' : ''}" data-mod="${def.id}"${def.dc ? ` data-dc="${def.dc}"` : ''} aria-label="${esc(def.title)}">
    <header class="mod-head${def.big ? ' big' : ''}">
      <div class="mod-head-l">
        <button class="grip grip-only" type="button" aria-label="拖动排序：${esc(def.title)}" title="拖动，或聚焦后按 ↑ ↓ 排序">${ic('grip')}</button>
        <div class="mod-tt">
          <div class="mod-kicker"><span data-ref="idx">01</span><span>${def.kicker}</span></div>
          <h2 class="mod-title">${def.title}</h2>
          <p class="mod-dek" data-ref="dek">${def.dek || ''}</p>
        </div>
      </div>
      <div class="mod-actions" data-ref="actions">
        <div class="mod-editbar"><span class="eyebrow" data-ref="visL">显示</span><button class="switch" role="switch" type="button" aria-checked="true" data-ref="vis" aria-label="显示「${esc(def.title)}」"></button></div>
      </div>
    </header>
    <div class="mod-body" data-ref="body"></div>
  </section>`);
  const m = Object.assign({ el, r: el._r, def, vis: false, dirty: true }, def);
  if (def.actions) { const a = def.actions(m); (Array.isArray(a) ? a : [a]).reverse().forEach(x => m.r.actions.prepend(x)); }
  def.build(m.r.body, m);
  MODS[def.id] = m;
  visObs.observe(el);
  return m;
}
const detailBtn = (sheet, label = '详情') => mount(`<button class="link-btn" type="button" data-sheet="${sheet}">${label}${ic('chev', 'i-sm')}</button>`);
const emptyHTML = (title, text = '', icon = 'info') => `<div class="empty-s">${ic(icon)}<div><b>${esc(title)}</b>${text ? `<span>${text}</span>` : ''}</div></div>`;
const vendorModel = d => { const v = String(d.vendor || '').trim(), mo = String(d.model || '').trim(); return (v && !/^0x/i.test(v) && !mo.toLowerCase().startsWith(v.toLowerCase()) ? v + ' ' : '') + mo; };

/* 格式器（给图表/图例用） */
const fPct = v => [F.int(v), '%'];
const fDeg = v => [F.int(v), '℃'];
const fW = v => [F.d1(v), 'W'];
const fRpm = v => [F.int(v), 'RPM'];
const fMBs = v => [F.d1(v), 'MB/s'];
const fLoad = v => [F.d2(v), ''];
const shortBytes = v => { const p = F.bytes(v); return p[0] + (p[1] ? p[1].charAt(0).replace('B', '') : ''); };

/* ---------------------------------------------------------------------
   01 METRICS — 六张指标卡
   --------------------------------------------------------------------- */
defineModule({
  id: 'metrics', page: 'overview', kicker: 'AT A GLANCE', title: '核心指标', dek: '处理器、内存、存储、网络、温度与功耗', headless: true,
  build(body, m) {
    const card = (id, label, sheet, main, gauge) => `<article class="card metric tap" data-sheet="${sheet}" data-m="${id}" tabindex="0" role="button" aria-label="${label}，查看详情">
        <div class="m-top"><span class="label">${label}</span><span class="m-state"><span data-ref="${id}St"></span>${ic('chev', 'i-sm more')}</span></div>
        <div class="m-main"${gauge ? '' : ' style="grid-column:1/-1"'}>${main}</div>
        <div class="m-foot" data-ref="${id}Foot"></div>
      </article>`;
    const val = (id, unit) => `<div class="m-val dnum"><span class="dm" data-ref="${id}V"></span><span class="unit" data-ref="${id}U">${unit}</span></div><div class="m-sub" data-ref="${id}Sub"></div>`;
    body.innerHTML = `<div class="grid metrics">
      ${card('cpu', '处理器', 'cpu', val('cpu', '%'), 1)}
      ${card('mem', '内存', 'mem', val('mem', '%'), 1)}
      ${card('disk', '存储', 'storage', val('disk', '%'), 1)}
      ${card('net', '网络', 'net', `<div class="net-vals"><div class="row">${ic('down', 'arr')}<span class="dnum"><span class="dm" data-ref="rxV"></span><span class="unit" data-ref="rxU"></span></span></div><div class="row sec"><svg class="i arr" style="color:var(--ink-3)" aria-hidden="true"><use href="#i-up"/></svg><span class="dnum"><span class="dm" data-ref="txV"></span><span class="unit" data-ref="txU"></span></span></div></div><div class="m-sub" data-ref="netSub"></div>`, 0)}
      ${card('temp', '温度', 'temp', val('temp', '℃'), 1)}
      ${card('pwr', '功耗', 'power', val('pwr', 'W'), 1)}
    </div>`;
    m.x = collect(body);
    m.cards = {}; $$('[data-m]', body).forEach(c => { m.cards[c.dataset.m] = c; });
    m.g = {};
    [['cpu', 'cpu'], ['mem', 'mem'], ['disk', 'storage'], ['temp', 'temp'], ['pwr', 'bolt']].forEach(([k, icn]) => { m.g[k] = makeGauge(icn); m.cards[k].querySelector('.m-main').after(m.g[k]); });
    m.sp = {};
    ['cpu', 'mem', 'net', 'temp', 'pwr'].forEach(k => { m.sp[k] = makeSpark(); m.x[k + 'Foot'].append(m.sp[k]); });
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, st = (k, lv, txt) => { setCls(m.cards[k], 'card metric tap', lv ? 'is-' + lv : ''); setText(x[k + 'St'], txt); };
    const vals = a => a.map(p => p[1]);
    // CPU
    const cpu = o.cpu || {}, c = cpu.percent, lc = LV.cpu(c);
    dnum(x.cpuV, c); m.g.cpu.set((c || 0) / 100, lc); m.g.cpu.tone(ok(c) ? rampAt(c, 'cpu') : ''); inkTo(x.cpuV, ok(c) ? rampInk(c, 'cpu') : '');
    st('cpu', lc, !ok(c) ? '无数据' : lc === 'crit' ? '高负载' : lc === 'warn' ? '繁忙' : c < 35 ? '平稳' : '适中');
    setText(x.cpuSub, [cpu.cores ? cpu.cores + ' 核' : '', ok(cpu.frequency_mhz) && cpu.frequency_mhz > 0 ? (cpu.frequency_mhz / 1000).toFixed(1) + ' GHz' : '', cpu.load ? '负载 ' + F.d2(cpu.load[0]) : ''].filter(Boolean).join(' · '));
    m.sp.cpu.set([{ values: vals(S.live.cpu) }], { min: 0, max: 100 });
    // MEM
    const mem = o.mem || {}, mp = mem.percent, lm = LV.mem(mp);
    dnum(x.memV, mp); m.g.mem.set((mp || 0) / 100, lm); m.g.mem.tone(ok(mp) ? rampAt(mp, 'mem') : ''); inkTo(x.memV, ok(mp) ? rampInk(mp, 'mem') : '');
    st('mem', lm, !ok(mp) ? '无数据' : lm ? LV_TXT[lm] : '充裕');
    setText(x.memSub, mem.total ? `${gb(mem.used)} / ${gbT(mem.total)} GB · 可用 ${gb(mem.available)} GB` : '内存数据不可用');
    m.sp.mem.set([{ values: vals(S.live.mem) }], { min: 0, max: 100 });
    // DISK
    const has = +o.disk_size > 0, dp = has ? +o.disk_total : null, ld = LV.cap(dp), dd = disksOf(o), sleeping = dd.filter(isSleep).length;
    dnum(x.diskV, dp); m.g.disk.set((dp || 0) / 100, ld); m.g.disk.tone(has ? rampAt(dp, 'cap') : ''); inkTo(x.diskV, has ? rampInk(dp, 'cap') : '');
    const smartBad = dd.filter(d => ['warn', 'crit'].includes(smartState(d).lv)).length;
    st('disk', ld || (smartBad ? 'warn' : ''), !has ? '无存储池' : ld ? (ld === 'crit' ? '将满' : '偏满') : smartBad ? smartBad + ' 块需留意' : '充足');
    setText(x.diskSub, has ? `${F.dsizeStr(o.disk_used)} / ${F.dsizeStr(o.disk_size)}${dd.length ? ` · ${dd.length} 块硬盘` : ''}${sleeping ? ` · ${sleeping} 块休眠` : ''}` : (dd.length ? `${dd.length} 块硬盘 · 未读取到存储池用量` : '未检测到存储'));
    const sig = dd.map(d => d.name).join(',');
    if (m.pipSig !== sig) { m.pipSig = sig; x.diskFoot.innerHTML = dd.length ? `<div class="drive-pips">${dd.slice(0, 12).map(d => `<div><div class="pip" data-d="${esc(d.name)}"><i></i></div><span>${esc(d.name.replace(/^nvme(\d+)n1$/, 'nvme$1'))}</span></div>`).join('')}</div>` : ''; }
    dd.slice(0, 12).forEach(d => {
      const p = x.diskFoot.querySelector(`[data-d="${CSS.escape(d.name)}"]`); if (!p) return;
      const sl = isSleep(d), sm = smartState(d), has2 = +d.total > 0;
      setCls(p, 'pip', sl ? 'sleep' : ['warn', 'crit'].includes(sm.lv) ? 'warn' : '');
      p.firstChild.style.height = (has2 ? clamp(+d.percent, 0, 100) : 0).toFixed(1) + '%';
      const pc = sl || !has2 ? '' : rampAt(+d.percent, 'cap'); if (p.firstChild.style.background !== pc) p.firstChild.style.background = pc;
      p.title = `${d.name} · ${has2 ? '已用 ' + F.int(d.percent) + '%' : '未挂载 / 无用量数据'}${sl ? ' · 休眠中' : ''}`;
    });
    // NET
    const rx = sumRx(o), tx = sumTx(o), ni = nets(o);
    dnumU(x.rxV, x.rxU, ni.length ? rx : null, F.rate); dnumU(x.txV, x.txU, ni.length ? tx : null, F.rate);
    st('net', '', !ni.length ? '无网卡' : rx + tx < 10240 ? '空闲' : '活跃');
    const tt = S.traffic && S.traffic.totals;
    setText(x.netSub, tt ? `今日 ↓ ${F.bytesStr(tt.today_rx)} · ↑ ${F.bytesStr(tt.today_tx)}` : `${ni.length} 个网口`);
    m.sp.net.set([{ values: vals(S.live.rx) }, { values: vals(S.live.tx) }], { min: 0 });
    // TEMP
    const ct = cpuTemp(o), mt = mbTemp(o), dts = diskTemps(o), dmax = dts.length ? Math.max(...dts.map(d => +d.temp)) : null;
    const tv = ct != null ? ct : mt, lt = ct != null ? LV.temp(ct) : '', ldt = dmax != null ? LV.dtemp(dmax) : '';
    setText(x.tempU, '℃');
    dnum(x.tempV, tv != null ? tv : dmax); m.g.temp.set(tv != null ? (tv - 20) / 80 : dmax != null ? (dmax - 20) / 50 : 0, lt || ldt);
    m.g.temp.tone(tv != null ? rampAt(tv, 'temp') : dmax != null ? rampAt(dmax, 'dtemp') : ''); inkTo(x.tempV, tv != null ? rampInk(tv, 'temp') : dmax != null ? rampInk(dmax, 'dtemp') : '');
    const lvT = lt === 'crit' || ldt === 'crit' ? 'crit' : lt || ldt;
    st('temp', lvT, tv == null && dmax == null ? '无传感器' : lvT === 'crit' ? '过热' : lvT === 'warn' ? '偏高' : '正常');
    const tp = [];
    if (ct == null && mt != null) tp.push('主板温度 · CPU 不可读'); else if (ct == null && dmax != null) tp.push('硬盘最高 · CPU 不可读');
    if (ct != null && mt != null) tp.push(`主板 ${F.int(mt)}℃`);
    if (dmax != null && tv != null) tp.push(`硬盘最高 ${F.int(dmax)}℃`);
    setText(x.tempSub, tv == null && dmax == null ? '未读取到温度传感器' : tp.join(' · ') || 'CPU 封装温度');
    m.sp.temp.set([{ values: vals(tv != null ? (ct != null ? S.live.temp : S.live.tempMb) : []) }], {});
    // POWER
    const pw = o.power || {}, has3 = pwrOk(o), w = has3 ? +pw.total : null, ep = S.pstats && S.pstats.estimation_params;
    const capW = ep ? ep.tdp_w + ep.base_w + ep.disk_typical_w * Math.max(1, dd.length) + ep.nic_fixed_w * Math.max(1, ni.length) : Math.max(60, (w || 0) * 1.5);
    dnum(x.pwrV, w, F.d1); m.g.pwr.set(w != null ? w / capW : 0, ''); m.g.pwr.tone(w != null ? rampAt(w / capW * 100, 'gen') : ''); inkTo(x.pwrV, w != null ? rampInk(w / capW * 100, 'gen') : '');
    st('pwr', '', !has3 ? '不可用' : pw.source === 'rapl' ? '实测 + 估算' : '估算');
    const en = S.pstats && S.pstats.energy, co = S.pstats && S.pstats.cost;
    setText(x.pwrSub, !has3 ? '无法读取或估算功耗' : en ? `今日 ${F.kwh(en.today_kwh)} kWh · ${F.yuan(co && co.today_yuan)}` : (pw.source === 'rapl' ? '硬件 RAPL 读数' : '按部件模型估算'));
    m.sp.pwr.set([{ values: vals(S.live.power) }], { min: 0 });
  },
});

/* ---------------------------------------------------------------------
   02 STORAGE — 存储池 + NAS 前面板（每块物理硬盘一个盘位）
   --------------------------------------------------------------------- */
defineModule({
  id: 'storage', page: 'overview', dc: 'util', kicker: 'STORAGE', title: '存储', dek: '存储池用量与每块物理硬盘的状态',
  actions: () => detailBtn('storage', '全部硬盘'),
  build(body, m) {
    body.innerHTML = `<div class="card pool">
        <div><div class="eyebrow">存储池使用率</div><div class="dnum" style="margin-top:12px"><span class="dm" data-ref="poolV"></span><span class="unit">%</span></div></div>
        <div><div class="bar" style="height:10px;border-radius:5px"><i data-ref="poolBar"></i></div><div class="legend" data-ref="poolLeg"></div></div>
        <div style="text-align:right"><div class="eyebrow">可用空间</div><div class="dnum sm" style="margin-top:12px"><span class="dm" data-ref="poolFree"></span><span class="unit" data-ref="poolFreeU"></span></div></div>
      </div>
      <div class="card nas">
        <div class="chassis">
          <div class="ch-top"><span class="pwr" title="电源"><i></i></span><span data-ref="chName"></span><span class="vent"></span><span data-ref="chModel">NAS</span></div>
          <div class="bays" data-ref="bays"></div>
        </div>
        <div class="nas-foot"><span><i class="led-key"></i>读写活动</span><span><i class="led-key warn"></i>需要留意</span><span>${ic('moon')}休眠中</span><span class="faint">点击盘位查看 SMART 详情 · 盘位按设备名排列，非实际槽位</span></div>
      </div>
      <div data-ref="raid"></div>`;
    m.x = collect(body); m.bays = {}; m.io = {};
    // 活动指示灯：闪烁概率 ∝ log(IO 速率)；仅在总览可见、版块在视口内时运行
    setInterval(() => {
      if (S.page !== 'overview' || document.hidden || !m.vis || S.editing) return;
      for (const name in m.bays) {
        const el = m.bays[name], led = el._r.led, rate = m.io[name] || 0;
        const on = el.dataset.st !== 'standby' && rate > 0 && (reduced() ? true : Math.random() < clamp(Math.log10(1 + rate / 65536) / 3.2, 0.05, 0.92));
        if (led._on !== on) { led._on = on; led.classList.toggle('on', on); }
      }
    }, 220);
  },
  buildBays(dd, m) {
    m.x.bays.innerHTML = ''; m.bays = {};
    m.x.bays.style.setProperty('--bays', clamp(dd.length, 1, 6));
    if (!dd.length) { m.x.bays.innerHTML = `<div class="bays-empty">${ic('hdd')}<span>未检测到物理硬盘（需要 lsblk 支持）</span></div>`; return; }
    dd.forEach(d => {
      const k = diskKind(d);
      const el = mount(`<button class="bay" type="button" data-sheet="disk/${esc(encodeURIComponent(d.name))}" data-type="${k === 'nvme' ? 'nvme' : 'hdd'}" aria-label="硬盘 ${esc(d.name)}，${esc(vendorModel(d))}，查看详情">
          <span class="bay-top"><span>${KIND_TXT[k]}</span><span class="bay-warn">${ic('alert')}</span><i class="bay-led" data-ref="led"></i></span>
          <span class="bay-body">
            <span class="bay-gauge" data-ref="g"></span>
            <span class="bay-zz" aria-hidden="true"><span class="dm" data-dm="Zz"></span>休眠</span>
            <span class="bay-info">
              <span class="bay-temp"><span data-ref="t">--</span><small>℃</small></span>
              <span class="bay-name">${esc(d.name)}</span>
              <span class="bay-model">${esc(vendorModel(d))}</span>
              <span class="bay-cap" data-ref="cap"></span>
            </span>
          </span>
          <span class="bay-grip"></span>
        </button>`);
      el._g = dotGauge(el._r.g, k === 'nvme' ? 4 : 5, 16);
      dot(el.querySelector('.bay-zz .dm'));
      m.bays[d.name] = el; m.x.bays.append(el);
    });
  },
  update(o, m) {
    if (!o) return;
    const x = m.x, dd = disksOf(o), has = +o.disk_size > 0;
    dnum(x.poolV, has ? o.disk_total : null); if (has) { rampBar(x.poolBar, +o.disk_total, 'cap'); inkTo(x.poolV, rampInk(+o.disk_total, 'cap')); } else { setW(x.poolBar, 0); inkTo(x.poolV, ''); }
    dnumU(x.poolFree, x.poolFreeU, has ? o.disk_size - o.disk_used : null, F.dsize);
    setHTML(x.poolLeg, has ? `<span>已用 <b>${F.dsizeStr(o.disk_used)}</b></span><span>总容量 <b>${F.dsizeStr(o.disk_size)}</b></span>${o.disk_pools ? `<span>${esc(o.disk_pools)} 个存储池</span>` : ''}` : '<span>未读取到存储池用量（fnOS 存储池挂载在 /vol*）</span>');
    const sy = o.system || {}, hw = (o.hardware || {}).board || {};
    setText(x.chName, sy.hostname || ''); setText(x.chModel, String(hw.product_name || hw.board_name || 'NAS').slice(0, 24) + ` · ${dd.length} 盘`);
    const sig = dd.map(d => d.name + diskKind(d)).join(',');
    if (m.sig !== sig) { m.sig = sig; this.buildBays(dd, m); }
    m.io = {}; (o.diskio || []).forEach(z => { m.io[z.name] = (+z.read_rate || 0) + (+z.write_rate || 0); });
    dd.forEach(d => {
      const el = m.bays[d.name]; if (!el) return; const r = el._r;
      const sm = smartState(d), sl = isSleep(d), warn = sm.lv === 'warn' || sm.lv === 'crit';
      const stt = sl ? 'standby' : warn ? 'warn' : 'ok';
      if (el.dataset.st !== stt) el.dataset.st = stt;
      setText(r.t, sl || !ok(d.temp) ? '--' : F.int(d.temp)); inkTo(r.t, sl || !ok(d.temp) ? '' : rampInk(+d.temp, 'dtemp'));
      const hasCap = +d.total > 0;
      el._g.set(hasCap ? +d.percent / 100 : 0, LV.cap(d.percent) || (warn ? 'warn' : ''), 'cap');
      setText(r.cap, hasCap ? `${F.int(d.percent)}% · ${F.dsizeStr(d.used)} / ${F.dsizeStr(d.total)}` : `${F.dsizeStr(d.size)} · 未挂载`);
      el.title = sl ? `${d.name} 已停转，监控不会唤醒它` : `${d.name} · ${sm.text}`;
    });
    // 阵列（mdadm / 阵列卡）概要
    const raid = o.raid || [], rc = o.raidcard || {};
    setHTML(x.raid, raid.length || (rc.type && rc.type !== 'none') ? `<div class="card pad raid-sum tap" data-sheet="storage" role="button" tabindex="0"><div class="label">${ic('server')}阵列<span class="faint" style="margin-left:auto;font-weight:400">${esc(rc.label || '')}</span></div><div class="raid-chips">${raid.map(r => `<span class="pill ${(r.members || []).some(z => /\(F\)/.test(z)) ? 'crit' : 'ok'}"><span class="dot"></span>${esc(r.device)} · ${esc(r.status)}</span>`).join('')}${(rc.controllers || []).map(c => (c.vd || []).map(v => `<span class="pill ${v.badge === 'off' ? 'crit' : v.badge === 'on' ? 'ok' : ''}"><span class="dot"></span>VD${esc(v.vd)} ${esc(v.type || '')} · ${esc(v.state_label || v.state || '--')}</span>`).join('')).join('')}${rc.type && rc.type !== 'none' && !(rc.controllers || []).length ? `<span class="pill">${esc(rc.detail || rc.error || '未读取到阵列卡详情')}</span>` : ''}</div></div>` : '');
    const warnD = dd.filter(d => ['warn', 'crit'].includes(smartState(d).lv)), sleepD = dd.filter(isSleep);
    let dek = !dd.length ? '未检测到物理硬盘。' : `${dd.length} 块硬盘${has ? `，存储池共 <b>${F.dsizeStr(o.disk_size)}</b>，已用 ${F.int(o.disk_total)}%` : ''}。`;
    if (warnD.length) dek += `<span class="warn">${warnD.map(d => esc(d.name)).join('、')}：${esc(smartState(warnD[0]).text)}</span>，建议确认备份。`;
    if (sleepD.length) dek += `${sleepD.map(d => esc(d.name)).join('、')} 正在休眠。`;
    setHTML(m.r.dek, dek);
  },
});


/* =====================================================================
   HEALTH — 一句话结论 + 健康分（全部基于真实读数与「提醒阈值」）
   ===================================================================== */
/* 忽略提醒：用户可手动忽略某个问题。记录 key（是哪个问题）+ fp（忽略时的状态指纹）。
   指纹规则：数值类（CPU / 内存 / 容量 / 温度）只看级别，升级（留意 → 严重）才重新提醒；
   SMART 看各计数（如重映射扇区 8 → 12 即重新提醒）；容器看是哪些容器未运行；阵列看状态文字。
   问题暂时消失（已恢复或休眠读不到）不清除忽略记录，设置里可查看与取消。 */
const Ignore = {
  list: () => UI.ignored || [],
  get: key => (UI.ignored || []).find(x => x.key === key),
  add(it) {
    UI.ignored = Ignore.list().filter(x => x.key !== it.key).concat([{ key: it.key, fp: it.fp, label: it.base || it.text, level: it.level, at: Date.now() }]).slice(-100);
    saveUI(); renderNow(); syncSettings();
    toast(`已忽略 · ${it.base || it.text}`, { sub: '状态变化时会重新提醒，可在 设置 → 已忽略的提醒 中取消', undo: () => Ignore.remove(it.key, true) });
  },
  remove(key, quiet) { UI.ignored = Ignore.list().filter(x => x.key !== key); saveUI(); renderNow(); syncSettings(); if (!quiet) toast('已恢复提醒', { key: 'save' }); },
};
function computeHealth(o) {
  const all = [], add = (level, key, text, sheet, go, fp) => all.push({ level, key, text, base: text, sheet, go, fp: fp == null ? level : String(fp) });
  if (!o) return { level: 'ok', issues: [], ignored: [], score: null };
  const c = o.cpu && o.cpu.percent, lc = LV.cpu(c);
  // CPU 高占用需持续（≥3 次采样）才算问题，避免转码尖峰让结论闪烁
  if (lc && S.cpuHigh >= 3) add(lc, 'cpu', `CPU 占用${lc === 'crit' ? '过高' : '偏高'}（${F.int(c)}%）`, 'cpu');
  const m = o.mem || {}, lm = LV.mem(m.percent);
  if (lm) add(lm, 'mem', `内存占用${lm === 'crit' ? '过高' : '偏高'}（${F.int(m.percent)}%）`, 'mem');
  if (+o.disk_size > 0) { const lp = LV.cap(o.disk_total); if (lp) add(lp, 'pool', `存储空间${lp === 'crit' ? '即将用尽' : '偏满'}（${F.int(o.disk_total)}%）`, 'storage'); }
  const t = cpuTemp(o), lt = LV.temp(t);
  if (lt) add(lt, 'temp', `CPU 温度${lt === 'crit' ? '过高' : '偏高'}（${F.int(t)}℃）`, 'temp');
  disksOf(o).forEach(d => {
    /* 文案用 diskLabel（插槽 6），跳转 key 仍用设备名：抽屉按 sdk 定位数据 */
    const sk = 'disk/' + d.name, sm = smartState(d), lb = diskLabel(d);
    if (sm.lv === 'crit' || sm.lv === 'warn') add(sm.lv, 'smart:' + d.name, `${lb}：${sm.text}`, sk, null, [sm.lv, String(d.health || '').toUpperCase()].concat(SMART_KEYS.map(([k]) => +d[k] || 0)).join(':'));
    if (!isSleep(d)) { const ld = LV.dtemp(d.temp); if (ld) add(ld, 'dt:' + d.name, `${lb} 温度${ld === 'crit' ? '过高' : '偏高'}（${F.int(d.temp)}℃）`, sk); }
    if (+d.total > 0) { const lcap = LV.cap(d.percent); if (lcap && Math.abs((+d.percent) - (+o.disk_total)) > 0.5) add(lcap, 'cap:' + d.name, `${lb} 已用 ${F.int(d.percent)}%`, sk); }
  });
  (o.raid || []).forEach(r => { const bad = (r.members || []).filter(x => /\(F\)/.test(x)); if (bad.length) add('crit', 'raid:' + r.device, `阵列 ${r.device} 有成员盘故障`, 'storage', null, 'crit:' + bad.slice().sort().join('|')); });
  const rc = o.raidcard || {};
  (rc.controllers || []).forEach(ct => {
    (ct.vd || []).forEach(v => { if (v.badge === 'off') add('crit', 'vd:' + v.vd, `阵列卡 VD${v.vd} ${v.state_label || v.state}`, 'storage', null, 'crit:' + (v.state || '')); });
    (ct.pds || []).forEach(p => { if (p.health && p.health !== 'ok') add(p.health === 'warn' ? 'warn' : 'crit', 'pd:' + p.loc, `阵列卡盘位 ${p.loc}：${p.health_label || p.health}`, 'storage', null, (p.health || '') + ':' + (p.health_label || '')); });
  });
  const dk = S.docker || o.docker;
  if (dockerOk(dk)) { const stopped = (dk.containers || []).filter(x => !ctrRun(x)); if (stopped.length) add('info', 'docker', `${stopped.length} 个容器未运行`, null, 'docker', stopped.map(x => x.name).sort().join('|')); }
  // 忽略分流：指纹一致 → 已忽略（不计入结论与健康分）；指纹变化 → 重新提醒并标注
  const issues = [], ignored = [];
  all.forEach(it => { const ig = Ignore.get(it.key); if (ig && ig.fp === it.fp) ignored.push(it); else { if (ig) { it.changed = true; it.text += ' · 较忽略时有变化'; } issues.push(it); } });
  const level = issues.some(i => i.level === 'crit') ? 'crit' : issues.some(i => i.level === 'warn') ? 'warn' : 'ok';
  const score = clamp(100 - issues.reduce((a, i) => a + (i.level === 'crit' ? 22 : i.level === 'warn' ? 7 : 1), 0), 0, 100);
  return { level, issues, ignored, score };
}

/* 天气图标：按 WMO 天气代码选择（离线内联 SVG） */
function wxIcon(code, day) {
  const c = +code, sun = '<circle cx="30" cy="16" r="6"/><path d="M30 5.5v2.2M38.8 9.2l-1.6 1.6M41.5 16h-2.2M21.2 9.2l1.6 1.6"/>';
  const cloud = (x = 0) => `<path d="M${14 + x} 38h18a7 7 0 0 0 .9-13.94A9.5 9.5 0 0 0 ${14.6 + x} 26 6 6 0 0 0 ${14 + x} 38Z" style="fill:var(--card)"/>`;
  let g;
  if (c <= 1) g = day === 0 ? '<path d="M33 30A11 11 0 1 1 20 13a8.5 8.5 0 0 0 13 17Z"/>' : '<circle cx="24" cy="24" r="8"/><path d="M24 8v3M24 37v3M8 24h3M37 24h3M12.7 12.7l2.1 2.1M33.2 33.2l2.1 2.1M12.7 35.3l2.1-2.1M33.2 14.8l2.1-2.1"/>';
  else if (c === 2) g = sun + cloud();
  else if (c === 3) g = cloud(2);
  else if (c === 45 || c === 48) g = '<path d="M9 18h30M6 25h36M9 32h30"/>';
  else if (c >= 95) g = cloud(2) + '<path d="m25 38-3 5h5l-3 5"/>';
  else if ((c >= 71 && c <= 77) || c === 85 || c === 86) g = cloud(2) + '<path d="M18 42v.1M25 44v.1M32 42v.1"/>';
  else g = cloud(2) + '<path d="m18 41-1.5 3.5M25 41l-1.5 3.5M32 41l-1.5 3.5"/>';
  return `<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${g}</svg>`;
}
const wxText = d => String(d || '').replace(/^[^一-龥A-Za-z]+/, '').trim() || '—';

const Hero = {
  build() {
    const el = $('#hero');
    const r = 52, C = 2 * Math.PI * r;
    this.C = C;
    el.innerHTML = `
      <div class="ring-wrap" data-sheet="health" title="健康检查" role="button" tabindex="0" aria-label="健康检查">
        <svg viewBox="0 0 120 120" aria-hidden="true"><circle class="ring-track" cx="60" cy="60" r="${r}" fill="none" stroke-width="7"/><circle data-ref="ring" class="ring-val" cx="60" cy="60" r="${r}" fill="none" stroke-width="7" stroke-linecap="round" stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${C.toFixed(2)}"/></svg>
        <div class="ring-center"><span class="dm" data-ref="score" style="justify-self:center"></span><span class="eyebrow" data-ref="scoreL">健康度</span></div>
      </div>
      <div class="hero-text">
        <p class="greet"><span data-ref="greet">你好</span><span class="faint">·</span><span data-ref="date"></span></p>
        <h1 class="verdict" data-ref="verdict" aria-live="polite">正在连接 NAS…</h1>
        <div class="issues" data-ref="issues"></div>
        <div class="facts">
          <span>${ic('clock')}已运行 <b class="tnum" data-ref="uptime">--</b></span>
          <span data-ref="osW" hidden>${ic('server')}<span data-ref="os"></span></span>
          <span>${ic('net')}<span class="tnum" data-ref="ip">--</span></span>
          <span data-ref="ctrW">${ic('box')}<span data-ref="ctrs">--</span></span>
        </div>
      </div>
      <div class="card clock-card">
        <div>
          <div class="clock-time"><span class="dm dm-hm" data-ref="hm"></span><span class="dm dm-sec" data-ref="sec"></span></div>
          <div class="clock-date" data-ref="cdate"></div>
          <div class="clock-lunar" data-ref="lunar"></div>
        </div>
        <div class="wx" data-ref="wx">
          <div class="wx-ico" data-ref="wxIco"></div>
          <div class="wx-main"><span class="dm" data-ref="wxT"></span><span data-ref="wxText">天气加载中…</span><span class="faint wx-city" data-ref="wxCity"></span></div>
          <div class="wx-sub" data-ref="wxSub"></div>
        </div>
      </div>`;
    this.r = collect(el);
    this.r.issues.addEventListener('click', e => {
      const b = e.target.closest('[data-i]'); if (!b) return;
      const it = this.issues && this.issues[+b.dataset.i]; if (!it) return;
      if (it.sheet) openSheet(it.sheet);
      else if (it.go === 'docker') { Docker.setFilter('exited'); go('docker'); }
      else if (it.go === 'retry') Poll.run('overview');
    });
    this.r.issues.addEventListener('click', e => { if (e.target.closest('[data-ignored]')) openSheet('health'); });
    this.r.wx.addEventListener('click', e => { if (e.target.closest('[data-goset]')) focusSetting('weatherCity'); });
    this.clock(); setInterval(() => this.clock(), 1000);
  },
  clock() {
    if (document.hidden) return;
    const d = new Date(), hh = F.p2(d.getHours()), mm = F.p2(d.getMinutes());
    setText($('#clockMini'), hh + ':' + mm);
    if (S.page !== 'overview' || scrollY > (this.r.hm.getBoundingClientRect().bottom + scrollY)) return;
    dot(this.r.hm).set(hh + (reduced() || d.getSeconds() % 2 === 0 ? ':' : ';') + mm); dot(this.r.sec).set(F.p2(d.getSeconds()));
    const h = d.getHours();
    setText(this.r.greet, h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好');
    const ds = `${d.getMonth() + 1}月${d.getDate()}日 ${WEEK[d.getDay()]}`;
    setText(this.r.date, ds); setText(this.r.cdate, d.getFullYear() + '年' + ds);
    const dk = dayKey(d); if (this._lunarDay !== dk) { this._lunarDay = dk; setText(this.r.lunar, '农历 ' + lunarText(d)); }
  },
  weather(w) {
    const r = this.r;
    if (!w) return;
    if (!w.ok) {
      r.wxIco.innerHTML = wxIcon(3); dot(r.wxT).set('--');
      setText(r.wxText, '天气不可用'); setText(r.wxCity, '');
      setHTML(r.wxSub, `${esc(w.error || '需要外网访问')} · <button class="link-like" type="button" data-goset="1">设置位置</button>`);
      return;
    }
    setHTML(r.wxIco, wxIcon(w.code, w.is_day));
    dot(r.wxT).set(Math.round(+w.temp) + '°'); setText(r.wxText, wxText(w.desc));
    setText(r.wxCity, String(w.city || '').slice(0, 24)); r.wxCity.title = w.city || '';
    setText(r.wxSub, `体感 ${F.int(w.feels)}° · 湿度 ${F.int(w.humidity)}% · 风速 ${F.d1(w.wind)} km/h`);
  },
  update(o) {
    const r = this.r;
    if (S.online === false || !o) {
      this.issues = S.online === false ? [{ level: 'crit', text: '重新连接', go: 'retry' }] : [];
      r.ring.style.stroke = 'var(--sleep)'; r.ring.style.strokeDashoffset = this.C.toFixed(2); dot(r.score).set('--'); r.score.style.color = '';
      setText(r.scoreL, S.online === false ? '已断开' : '健康度');
      setHTML(r.verdict, S.online === false ? `暂时无法连接到 NAS<span class="soft">每隔几秒自动重试${S.lastOk ? '，上次更新于 ' + esc(F.hms(S.lastOk / 1000)) : ''}。</span>` : '正在连接 NAS…');
      setHTML(r.issues, S.online === false ? `<button class="pill btn crit" type="button" data-i="0">${ic('refresh')}立即重试</button>` : '');
      return;
    }
    const h = this.health = computeHealth(o);
    this.issues = h.issues;
    dnum(r.score, h.score);
    r.ring.style.strokeDashoffset = (this.C * (1 - h.score / 100)).toFixed(2);
    r.ring.style.stroke = h.level === 'ok' ? 'var(--ok)' : h.level === 'warn' ? 'var(--warn-fill)' : 'var(--crit)';
    r.score.style.color = h.level === 'crit' ? 'var(--crit)' : '';
    setText(r.scoreL, h.level === 'ok' ? '健康度 · 良好' : h.level === 'warn' ? '健康度 · 留意' : '健康度 · 警示');
    const sy = o.system || {}, host = esc(sy.hostname || 'NAS'), up = +o.uptime || 0;
    const att = h.issues.filter(i => i.level !== 'info');
    let v;
    if (h.level === 'ok') v = `${host} 运行良好<span class="soft">${up >= 86400 ? `已连续运行 ${Math.floor(up / 86400)} 天` : `已运行 ${esc(F.dur(up))}`}，${h.ignored.length ? `除已忽略的 ${h.ignored.length} 项外，` : ''}各项指标都在阈值以内。</span>`;
    else if (h.level === 'warn') v = `${host} 运行平稳<span class="soft">有 ${att.length} 项需要留意：${esc(att[0].text)}${att.length > 1 ? ' 等' : ''}。</span>`;
    else v = `${host} 需要你的关注<span class="soft">${esc(att.filter(i => i.level === 'crit')[0].text)}${att.length > 1 ? `，另有 ${att.length - 1} 项` : ''}。</span>`;
    setHTML(r.verdict, v);
    setHTML(r.issues, h.issues.slice(0, 6).map((it, i) => `<button class="pill btn ${it.level === 'info' ? '' : it.level}" type="button" data-i="${i}">${ic(it.level === 'info' ? 'info' : 'alert')}${esc(it.text)}${ic('chev')}</button>`).join('') + (h.issues.length > 6 ? `<button class="pill btn" type="button" data-sheet="health">另有 ${h.issues.length - 6} 项${ic('chev')}</button>` : '') + (h.ignored.length ? `<button class="pill btn ghost" type="button" data-ignored="1" title="已忽略的提醒不计入健康分">${ic('eye-off')}已忽略 ${h.ignored.length} 项</button>` : ''));
    setText(r.uptime, F.dur(up));
    // 只显示飞牛 fnOS 版本，读不到就整项隐藏（不退回底层 Debian）
    setText(r.os, sy.fnos_version ? 'fnOS ' + sy.fnos_version : ''); r.osW.hidden = !sy.fnos_version;
    const ip = (sy.ips || []).find(x => (x.ipv4 || []).length); setText(r.ip, ip ? ip.ipv4[0] : '--');
    const dk = S.docker || o.docker;
    if (dockerOk(dk)) { const cs = dk.containers || []; setText(r.ctrs, `${cs.filter(ctrRun).length} / ${cs.length} 个容器运行中`); r.ctrW.hidden = false; }
    else r.ctrW.hidden = true;
  },
};


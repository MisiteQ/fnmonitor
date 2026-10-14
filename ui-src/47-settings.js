/* =====================================================================
   SETTINGS — 改动即保存：服务配置走 /api/config/save，界面配置走 /api/ui
   ===================================================================== */
const SECRET_KEYS = ['auth_password', 'old_password'];
const stripSecrets = c => { const o = Object.assign({}, c || {}); SECRET_KEYS.forEach(k => delete o[k]); return o; };
const pwSet = () => !!(S.cfg && (S.cfg.auth_password_set != null ? S.cfg.auth_password_set : S.cfg.auth_has_password));
const authOn = () => !!(S.cfg && +S.cfg.auth_enabled);
const inIframe = (() => { try { return window.self !== window.top; } catch (e) { return true; } })();
function flashRow(row) { if (!row) return; row.classList.remove('saved'); void row.offsetWidth; row.classList.add('saved'); }
async function loadConfig() {
  const j = await API.get('/api/config');
  S.cfg = stripSecrets(j.config || {});
  applyConfig();
  return S.cfg;
}
/** 保存服务端配置；成功后以服务端返回为准刷新本地副本 */
async function saveCfg(data, label, row, quiet) {
  try {
    const j = await API.post('/api/config/save', data);
    if (!j || !j.ok) throw new Error((j && j.error) || '未知错误');
    if (j.config) Object.assign(S.cfg, stripSecrets(j.config));
    Object.keys(data).forEach(k => { if (!SECRET_KEYS.includes(k) && S.cfg[k] === undefined) S.cfg[k] = data[k]; });
    flashRow(row); if (!quiet) toast(j.note ? `已保存 · ${label}。${j.note}` : `已保存 · ${label}`, { key: 'save', ms: j.note ? 9000 : 2400 });
    applyConfig(); syncSettings();
    return j;
  } catch (e) {
    toast(`保存失败 · ${label}：${String(e.message).slice(0, 80)}`, { icon: 'alert', ms: 5000 });
    syncSettings();
    return null;
  }
}
/** 配置变化带来的界面效果（上月汇总显隐、登出按钮、更新红点） */
function applyConfig() {
  const c = S.cfg || {};
  if (MODS.monthly) MODS.monthly.el.hidden = !+c.monthly_summary_enabled;
  $('#btnLogout').hidden = !(authOn() && pwSet());
  const st = c.update_status || {};
  S.hasUpdate = !!st.has_update || !!(S.upd && S.upd.has_update);
  $$('#pageSeg [data-page="settings"], #bottomNav [data-page="settings"], #setNav [data-g="update"]').forEach(b => b.classList.toggle('has-dot', S.hasUpdate));
  if (c.version) { setText($('#verTag'), 'v' + c.version); }
}

/* ---- 控件 ---- */
function numField(it, row) {
  const el = mount(`<span class="numfield"><button class="st" type="button" aria-label="减少">−</button><input type="number" inputmode="decimal" min="${it.min}" max="${it.max}" step="${it.step || 1}" aria-label="${esc(it.label)}"><span class="u">${esc(it.unit || '')}</span><button class="st" type="button" aria-label="增加">+</button></span>`);
  const inp = el.querySelector('input'), [dec, inc] = el.querySelectorAll('.st'), st = it.step || 1;
  let tm;
  const cur = () => { const v = it.get(); return ok(v) ? +v : null; };
  el.sync = () => { if (document.activeElement !== inp) { const v = cur(); inp.value = v == null ? '' : v; } };
  const commit = () => {
    let n = parseFloat(inp.value);
    if (!isFinite(n)) { el.sync(); return; }
    if (it.strict && (n < it.min || n > it.max)) { toast(`${it.label}需在 ${it.min} – ${it.max} 之间，未保存`, { icon: 'alert', ms: 3600 }); el.sync(); return; }
    const c = clamp(Math.round(n / st) * st, it.min, it.max), fixed = +c.toFixed(4);
    if (fixed !== n) toast(`${it.label}的允许范围是 ${it.min} – ${it.max}${it.unit ? ' ' + it.unit : ''}，已调整为 ${fixed}`, { icon: 'info', ms: 3600 });
    inp.value = fixed;
    if (fixed !== cur()) it.set(fixed, row);
  };
  inp.addEventListener('change', commit);
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') inp.blur(); });
  [[dec, -1], [inc, 1]].forEach(([b, d]) => b.addEventListener('click', () => { inp.value = +(clamp((parseFloat(inp.value) || 0) + d * st, it.min, it.max)).toFixed(4); clearTimeout(tm); tm = setTimeout(commit, 600); }));
  el.sync();
  return el;
}
function controlFor(it, row) {
  if (it.type === 'toggle') {
    const b = mount(`<button class="switch" type="button" role="switch" aria-checked="false" aria-label="${esc(it.label)}"></button>`);
    b.sync = () => b.setAttribute('aria-checked', String(!!it.get()));
    b.onclick = () => { const nv = b.getAttribute('aria-checked') !== 'true'; b.setAttribute('aria-checked', String(nv)); it.set(nv, row); };
    row.classList.add('inline'); b.sync(); return b;
  }
  if (it.type === 'seg') { const s = makeSeg(it.options, it.get(), v => it.set(v, row), 'sm'); s.sync = () => { const v = it.get(); if (String(v) !== String(s.value)) s.set(v); }; return s; }
  if (it.type === 'number') return numField(it, row);
  if (it.type === 'text') {
    const el = mount(`<span class="txt-ctl"><input class="textfield ${it.mono ? 'mono' : ''}" placeholder="${esc(it.placeholder || '')}" aria-label="${esc(it.label)}" spellcheck="false"><button class="btn" type="button" data-ref="save">保存</button>${it.cancel ? '<button class="btn ghost" type="button" data-ref="cancel" hidden>取消</button>' : ''}</span>`);
    const inp = el.querySelector('input'), x = el._r;
    const dirty = () => { const d = inp.value.trim() !== String(it.get() || ''); x.save.disabled = !d; if (x.cancel) x.cancel.hidden = !d; };
    el.sync = () => { if (document.activeElement !== inp) inp.value = it.get() || ''; if (it.ph) inp.placeholder = it.ph(); dirty(); };
    inp.addEventListener('input', dirty);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') x.save.click(); if (e.key === 'Escape' && x.cancel) { e.stopPropagation(); x.cancel.click(); } });
    x.save.onclick = async () => { const v = inp.value.trim(); if (it.validate) { const err = it.validate(v); if (err) { toast(err, { icon: 'alert', ms: 4200 }); return; } } x.save.disabled = true; await it.set(v, row); inp.blur(); el.sync(); };
    if (x.cancel) x.cancel.onclick = () => { inp.value = it.get() || ''; dirty(); };
    el.sync(); return el;
  }
  if (it.type === 'input') {
    const el = mount(`<input class="textfield ${it.mono ? 'mono' : ''}" placeholder="${esc(it.placeholder || '')}" aria-label="${esc(it.label)}" spellcheck="false">`);
    el.sync = () => { if (document.activeElement !== el) el.value = it.get() || ''; };
    el.addEventListener('input', () => it.set(el.value.trim(), row));
    el.sync(); return el;
  }
  if (it.type === 'accent') {
    const el = mount(`<div class="swatches" role="radiogroup" aria-label="主色">${ACCENTS.map(([id, l, c]) => `<button class="swatch" type="button" role="radio" style="--sw:${c}" data-v="${id}" title="${l}" aria-label="${l}" aria-checked="false"></button>`).join('')}<span class="swatch-l" data-ref="l"></span></div>`);
    el.sync = () => { $$('.swatch', el).forEach(x => x.setAttribute('aria-checked', String(x.dataset.v === UI.accent))); setText(el._r.l, (ACCENTS.find(a => a[0] === UI.accent) || ACCENTS[0])[1]); };
    el.addEventListener('click', e => { const b = e.target.closest('.swatch'); if (!b) return; it.set(b.dataset.v, row); el.sync(); });
    el.sync(); return el;
  }
  if (it.type === 'colorspec') return colorSpec();
  if (it.type === 'action') { const b = mount(`<button class="btn ${it.danger ? 'danger' : ''}" type="button">${esc(it.button)}</button>`); b.onclick = () => it.run(b, row); return b; }
  if (it.type === 'info') { const el = mount(`<div class="set-info"></div>`); el.sync = () => setHTML(el, it.html()); el.sync(); return el; }
}
const ACCENTS = [['black', '墨色', '#0E0E0E'], ['blue', '钴蓝', '#2B50C0'], ['green', '翡翠', '#0B9B61'], ['yellow', '琥珀', '#C98A00'], ['cyan', '青', '#0098B0']];
function colorSpec() {
  const sw = (v, t, d) => `<div class="cs-item"><i style="background:${v}"></i><div><b>${t}</b><span>${d}</span></div></div>`;
  const el = mount(`<div class="colorspec">
    <div class="cs-col"><div class="eyebrow">概览层 · 主色</div>${sw('var(--d-p)', '界面与概览', '选中态 · 按钮 · 指标卡迷你趋势')}${sw('color-mix(in srgb,var(--d-p) 55%,var(--card))', '构成 / 次要', '内存构成 · 功耗分项')}${sw('var(--fill-3)', '参考', '轨道 · 网格 · 历史对照')}</div>
    <div class="cs-col"><div class="eyebrow">分析层 · 数据实体色</div>${sw('var(--e-blue)', '处理与流入', 'CPU · 负载 · 下行 · 读取')}${sw('var(--e-coral)', '流出与发热', '上行 · 写入 · CPU 温度')}${sw('var(--e-teal)', '资源与能量', '内存 · 功耗 · 用电')}${sw('var(--e-violet)', '辅助硬件', '主板温度 · 风扇')}</div>
    <div class="cs-col"><div class="eyebrow">负载色阶 · 容量与进度</div>
      <div class="cs-ramp" data-ref="ramp"></div>
      <div class="cs-rl"><span>充裕</span><span>适中</span><span>偏高 · 警告</span><span>严重</span></div>
      <div class="cs-rdemo" data-ref="demo"></div>
      <span class="cs-note">条形、圆弧、点阵、温度条按数值着色，旁边联动的数字同步变色（浅色模式下压暗一档保证可读）；转折点跟随「提醒阈值」</span></div>
    <div class="cs-col"><div class="eyebrow">紧急程度 · 覆盖两层</div>${sw('var(--crit)', '严重', '整件变红 · 曲线越线段变红')}${sw('var(--warn-fill)', '警告', '整件变暖橙 · 画阈值线')}${sw('var(--sleep)', '休眠 / 离线', '石板灰 + ☾')}<div class="cs-item"><i class="cs-none"></i><div><b>正常</b><span>不着色，只用 ✓</span></div></div></div>
  </div>`);
  el.sync = () => {
    el._r.ramp.style.background = rampGrad('cpu');
    el._r.demo.innerHTML = [18, 52, 74, 88, 97].map(v => `<div><i style="width:${v}%;background-image:${rampGrad('cpu')};background-size:${(10000 / v).toFixed(1)}% 100%"></i></div>`).join('');
  };
  el.sync(); return el;
}
function setRow(it) {
  const row = mount(`<div class="set-row${it.wide ? ' wide' : ''}" data-k="${it.k}"><div><div class="lbl">${esc(it.label)}${it.help ? `<button class="q" type="button" aria-label="说明" aria-expanded="false">${ic('help')}</button>` : ''}</div><div class="hint" data-ref="hint"></div></div><div class="ctl"></div></div>`);
  const ctl = controlFor(it, row); row.querySelector('.ctl').append(ctl);
  const hint = row._r.hint;
  row._sync = () => { ctl.sync && ctl.sync(); const h = typeof it.hint === 'function' ? it.hint() : it.hint || ''; setHTML(hint, h); hint.hidden = !h; if (it.showIf) row.hidden = !it.showIf(); };
  if (it.help) row.querySelector('.q').addEventListener('click', e => { e.stopPropagation(); openPop(e.currentTarget, it.label, it.help); });
  row._sync();
  return row;
}
/* ---- 配置项定义 ---- */
const cfgItem = (k, label, type, extra) => Object.assign({ k, label, type, get: () => S.cfg ? S.cfg[k] : undefined, set: (v, row) => saveCfg({ [k]: type === 'toggle' ? (v ? 1 : 0) : v }, label, row) }, extra);
const cfgToggle = (k, label, extra) => Object.assign(cfgItem(k, label, 'toggle', extra), { get: () => S.cfg ? !!+S.cfg[k] : false });
const uiItem = (k, label, type, extra) => Object.assign({ k, label, type, get: () => UI[k], set: v => { UI[k] = v; applyUI(k); saveUI(); toast(`已保存 · ${label}`, { key: 'save' }); } }, extra);
const SET_SCHEMA = [
  { id: 'service', title: '服务', icon: 'server', desc: '飞牛监控自身的运行方式。', items: [
    Object.assign(cfgItem('port', '服务端口', 'number', { min: 1024, max: 65535, strict: true, hint: () => '1024 – 65535；修改后需在 应用中心 → 飞牛监控 中重启应用生效，并在飞牛防火墙放行新端口' + (S.cfg && +S.cfg.port && S.cfg.listen_port && +S.cfg.listen_port !== +S.cfg.port ? `；当前实际监听 ${esc(S.cfg.listen_port)}（重启后切换）` : '') }), { get: () => S.cfg ? (+S.cfg.port || +S.cfg.listen_port || +location.port || 8777) : undefined }),
    cfgItem('open_mode', '飞牛桌面打开方式', 'seg', { options: [['url', '独立网页'], ['iframe', '飞牛窗口']], help: '点击飞牛桌面「飞牛监控」图标时的打开方式。独立网页：直接在浏览器新标签页打开（完整浏览器能力：全屏 / 下载 / 插件）。飞牛窗口：在 fnOS 桌面内嵌窗口中打开。保存时自动写入桌面入口——刷新飞牛桌面页面（F5）或重新登录后生效；若桌面尚未刷新仍弹出内嵌窗口，窗口内会显示引导页可一键转到浏览器。', hint: () => inIframe ? '当前在飞牛窗口内 · <a href="/" target="_blank" rel="noopener">立即在新标签页打开 ↗</a>' : '' }),
    { k: 'weatherAuto', label: '天气自动定位', type: 'toggle', hint: '按 NAS 公网 IP 估算城市；需要外网', get: () => !(S.cfg && S.cfg.weather_city) && !S.weatherManual, set: (v, row) => { if (v) { S.weatherManual = false; saveCfg({ weather_city: '' }, '已切换为自动定位天气', row).then(() => { syncSettings(); setTimeout(() => Poll.run('weather'), 800); }); } else { S.weatherManual = true; syncSettings(); setTimeout(() => { const i = $('.set-row[data-k="weather_city"] input'); i && i.focus(); }, 60); } } },
    cfgItem('weather_city', '天气位置', 'text', { placeholder: '城市名（如 徐州）或 纬度,经度', showIf: () => !!(S.cfg && S.cfg.weather_city) || !!S.weatherManual, hint: '填城市名，或 纬度,经度（如 34.26,117.18）',
      validate: v => !v ? '请输入城市名或坐标；如需自动定位，请打开上方开关' : v.includes(',') && !/^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$/.test(v) ? '坐标格式应为 纬度,经度（如 34.26,117.18）' : v.length > 100 ? '位置太长了' : '',
      set: (v, row) => saveCfg({ weather_city: v }, '天气位置', row).then(() => setTimeout(() => Poll.run('weather'), 800)) }),
  ] },
  { id: 'collect', title: '采集与历史', icon: 'pulse', desc: '更短的间隔更实时，也会写入更多数据、占用更多 NAS 资源。', items: [
    cfgItem('interval', '实时采集间隔', 'number', { min: 5, max: 3600, unit: '秒', hint: '监控刷新周期，值越小越实时但负载略高' }),
    cfgItem('history_interval', '历史采样间隔', 'number', { min: 60, max: 86400, unit: '秒', hint: '历史趋势每隔多久记录一条，默认 60 秒' }),
    cfgItem('retention_days', '历史保留', 'number', { min: 1, max: 365, unit: '天', hint: '超过保留期的历史数据会自动清理' }),
    cfgToggle('monthly_summary_enabled', '上月汇总', { hint: () => `在总览底部显示上个月的运行回顾${S.cfg && +S.cfg.retention_days < 31 ? `；历史保留 ${esc(S.cfg.retention_days)} 天不足整月，会自动叠加保存汇总` : ''}`, help: '每月自动生成。即使历史保留不足一个月，也会单独保存月度聚合数据，确保汇总完整。', set: (v, row) => saveCfg({ monthly_summary_enabled: v ? 1 : 0 }, '上月汇总', row).then(() => { Poll.run('monthly'); }) }),
  ] },
  { id: 'thresholds', title: '提醒阈值', icon: 'alert', desc: '平时不着色；超过警告阈值显示暖橙，超过严重阈值显示红色。总览结论、问题提示、趋势阈值线与负载色阶都使用这里的数值，健康报告也使用同一套阈值。', custom: renderThresholds },
  { id: 'power', title: '功耗与电费', icon: 'bolt', desc: '用于电费计算，以及无 RAPL 时的整机功耗估算。', items: [
    cfgItem('power_rate_yuan', '电价', 'number', { min: 0, max: 10, step: 0.01, unit: '元/kWh' }),
    cfgItem('power_tdp_w', 'CPU TDP', 'number', { min: 5, max: 500, unit: 'W', hint: 'RAPL 不可用时（AMD / ARM）的估算基线；ARM SoC 建议 15–35 W' }),
    cfgItem('power_disk_typical_w', '单盘典型功耗', 'number', { min: 0, max: 50, step: 0.5, unit: 'W', hint: '每块活动硬盘，3.5 寸 HDD 约 8 W，SSD 约 3 W' }),
    cfgItem('power_nic_fixed_w', '网卡固定功耗', 'number', { min: 0, max: 50, step: 0.5, unit: 'W', hint: '每块物理网卡' }),
    cfgItem('power_base_w', '主板与其他', 'number', { min: 0, max: 100, step: 0.5, unit: 'W', hint: 'RAPL 只测 CPU 封装，整机估算叠加此底座，可按插座实测补差' }),
  ] },
  { id: 'disk', title: '网络与硬盘', icon: 'net', items: [
    cfgToggle('traffic_exclude_bridge', '排除 Docker 网桥流量', { help: '开启后不计入 docker / veth / br- / virbr / tun / tap 等虚拟网卡的累计流量，统计更接近真实的外网用量。' }),
    cfgToggle('disk_standby_protect', '休眠硬盘保护', { hint: '不唤醒已停转的硬盘', help: '开启后跳过休眠硬盘的容量 / SMART / 温度读取，避免把它唤醒；历史在硬盘醒来后补写。关闭则每个周期完整采集，硬盘可能无法进入休眠。' }),
    cfgToggle('ec_fan_probe', '实验性 · EC 风扇读取', { hint: '通过硬件 I/O 端口读取部分商用机（NEC / 联想）风扇转速，默认关闭', help: '仅在标准 hwmon / IPMI 读不到风扇时使用。该方式直接访问主板 EC 端口（只读），个别机型可能不兼容，如出现异常请关闭。' }),
  ] },
  { id: 'ignored', title: '已忽略的提醒', icon: 'eye-off', desc: '在 健康检查 中忽略的问题会列在这里：不计入健康分与首页结论；问题状态发生变化（如重映射扇区数增加、级别从留意升为严重、未运行的容器变化）时会自动重新提醒。', custom: renderIgnored },
  { id: 'look', title: '外观', icon: 'palette', items: [
    uiItem('theme', '外观模式', 'seg', { options: [['light', '浅色'], ['dark', '深色'], ['auto', '跟随系统']] }),
    { k: 'colorspec', label: '色彩规范', type: 'colorspec', wide: true, hint: '概览安静、分析丰富：颜色按层级分工，紧急程度覆盖一切' },
    uiItem('accent', '主色', 'accent', { hint: '主色用于界面与概览层；趋势图使用固定的数据实体色，换主色不必重新认色' }),
    uiItem('dotMatrix', '点阵数字', 'toggle', { hint: '关键读数使用 5×7 点阵字形，关闭后改用常规数字' }),
    uiItem('fontScale', '字体大小', 'seg', { options: [[0.9, '较小'], [1, '标准'], [1.1, '较大'], [1.25, '特大']] }),
    uiItem('motion', '动效', 'toggle', { hint: '系统开启“减少动态效果”时始终关闭' }),
    { k: 'uiSync', label: '界面设置同步', type: 'action', button: '立即同步', hint: () => `外观、布局与阈值保存在 NAS：<span class="mono">${esc((S.cfg && S.cfg.ui_file) || '数据目录/ui.json')}</span>，换设备或清缓存后自动恢复`, run: async b => { b.disabled = true; const okk = await postUI(); b.disabled = false; toast(okk ? '已同步到 NAS' : '同步失败，请检查连接', okk ? {} : { icon: 'alert' }); } },
    { k: 'hiddenMods', label: '已隐藏的版块', type: 'info', html: hiddenHTML },
    { k: 'resetLayout', label: '布局', type: 'action', button: '恢复默认布局', run: () => { Object.keys(LAYOUT_DEFAULT).forEach(resetLayout); toast('已恢复默认布局'); syncSettings(); } },
    { k: 'resetUI', label: '一键初始化界面', type: 'action', button: '恢复默认界面', danger: true, hint: '外观、主色、字号、布局与隐藏状态恢复默认（提醒阈值保持不变）', run: resetUI },
  ] },
  { id: 'auth', title: '登录口令', icon: 'lock', desc: '开启后，访问飞牛监控需要先输入口令。', custom: renderAuth },
  { id: 'data', title: '数据', icon: 'folder', custom: renderData },
  { id: 'update', title: '软件更新', icon: 'download', custom: renderUpdate },
  { id: 'about', title: '关于', icon: 'info', custom: renderAbout },
];
function hiddenHTML() {
  const list = [].concat(...Object.keys(UI.hidden).map(p => UI.hidden[p].map(id => [p, id])));
  if (!list.length) return '<span class="faint">没有隐藏的版块 · 在「编辑布局」中可隐藏或排序</span>';
  return `<div class="chips">${list.map(([p, id]) => `<button class="chip-t" type="button" data-restore="${p}:${id}">${ic('eye')}${esc(MODS[id] ? MODS[id].title : id)}</button>`).join('')}<button class="chip-t" type="button" data-restore="*">全部恢复</button></div>`;
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-restore]'); if (!b) return;
  const v = b.dataset.restore;
  if (v === '*') Object.keys(UI.hidden).forEach(p => { UI.hidden[p] = []; });
  else { const [p, id] = v.split(':'); UI.hidden[p] = (UI.hidden[p] || []).filter(x => x !== id); }
  Object.keys(LAYOUT_DEFAULT).forEach(applyLayout); saveUI(); syncSettings(); toast('已恢复显示');
});

function renderIgnored(card) {
  card.addEventListener('click', e => {
    const un = e.target.closest('[data-unig]'); if (un) Ignore.remove(un.dataset.unig);
    if (e.target.closest('[data-unall]')) { const n = Ignore.list().length; UI.ignored = []; saveUI(); renderNow(); syncSettings(); toast(`已恢复全部 ${n} 项提醒`, { key: 'save' }); }
    if (e.target.closest('[data-openhealth]')) openSheet('health');
  });
  card._sync = () => {
    const list = Ignore.list().slice().sort((a, b) => b.at - a.at);
    if (!list.length) { setHTML(card, `<div class="set-row inline"><div><div class="lbl">暂无已忽略的提醒</div><div class="hint">在首页点击健康分或问题标签打开「健康检查」，可对单个问题选择忽略</div></div><div class="ctl"><button class="btn" type="button" data-openhealth="1">打开健康检查</button></div></div>`); return; }
    const h = S.o ? computeHealth(S.o) : { issues: [], ignored: [] };
    const state = ig => { if (h.ignored.some(i => i.key === ig.key)) return '<span class="ig-state on">仍存在 · 已静默</span>'; const cur = h.issues.find(i => i.key === ig.key); if (cur) return '<span class="ig-state on">有变化 · 已重新提醒</span>'; return '<span class="ig-state gone">当前未出现</span>'; };
    setHTML(card, list.map(ig => `<div class="set-row ig-row"><span class="${ig.level === 'info' ? 'faint' : 'st-' + ig.level}">${ic(ig.level === 'info' ? 'info' : 'alert')}</span><div style="min-width:0"><div class="lbl">${esc(ig.label)}</div><div class="ig-meta">${state(ig)} · 忽略于 ${esc(ig.at ? F.mdhm(ig.at / 1000) : '--')}</div></div><div class="ctl"><button class="btn sm" type="button" data-unig="${esc(ig.key)}">${ic('eye')}取消忽略</button></div></div>`).join('')
      + `<div class="set-row inline"><div><div class="lbl">全部恢复提醒</div><div class="hint">共 ${list.length} 项</div></div><div class="ctl"><button class="btn" type="button" data-unall="1">全部取消忽略</button></div></div>`);
  };
  card._sync();
}
function renderThresholds(card) {
  const ROWS = [['cpu', 'CPU 占用', '%'], ['mem', '内存占用', '%'], ['cap', '存储容量', '%'], ['temp', 'CPU 温度', '℃'], ['diskTemp', '硬盘温度', '℃']];
  card.innerHTML = ROWS.map(([k, l]) => `<div class="set-row th-row" data-k="${k}Warn"><div><div class="lbl">${l}</div><div class="hint" data-h="${k}"></div></div><div class="ctl th-pair" data-p="${k}"></div></div>`).join('') +
    `<div class="set-row inline"><div><div class="lbl">恢复默认阈值</div><div class="hint">CPU / 内存 / 容量 85% · 95%，CPU 温度 80 · 90℃，硬盘温度 50 · 58℃</div></div><div class="ctl"><button class="btn" type="button" data-ref="reset">恢复默认</button></div></div>`;
  const save = (k, v, row) => {
    const t = Object.assign({}, TH, { [k]: v }), pair = TH_PAIRS.find(p => p.includes(k));
    if (pair && t[pair[1]] < t[pair[0]]) { if (k === pair[0]) t[pair[1]] = t[pair[0]]; else t[pair[0]] = t[pair[1]]; toast('严重阈值不能低于警告阈值，已同步调整', { icon: 'info', ms: 3600 }); }
    setThresholds(t); saveUI(); flashRow(row); toast('已保存 · 提醒阈值', { key: 'save' }); applyThresholds();
  };
  ROWS.forEach(([k, , u]) => {
    const host = card.querySelector(`[data-p="${k}"]`), row = host.closest('.set-row');
    [['Warn', '警告'], ['Crit', '严重']].forEach(([s, l]) => {
      const key = k + s, [, lo, hi] = TH_SPEC[key];
      const w = mount(`<label class="th-f"><span class="th-l ${s === 'Warn' ? 'w' : 'c'}">${l}</span></label>`);
      w.append(numField({ label: l, min: lo, max: hi, step: 1, unit: u, get: () => TH[key], set: v => save(key, v, row) }, row));
      host.append(w);
    });
  });
  card._r = collect(card);
  card._r.reset.onclick = () => { setThresholds({}); saveUI(); applyThresholds(); syncSettings(); toast('已恢复默认阈值'); };
  card._sync = () => { $$('.numfield', card).forEach(n => n.sync && n.sync()); ROWS.forEach(([k, , u]) => setText(card.querySelector(`[data-h="${k}"]`), `≥ ${TH[k + 'Warn']}${u} 警告 · ≥ ${TH[k + 'Crit']}${u} 严重`)); };
  card._sync();
}

function renderAuth(card) {
  card.innerHTML = `<div class="note warn auth-note">${ic('shield')}<span>飞牛监控以 root 权限运行，并能启动 / 停止 Docker 容器。即使只在局域网使用，也建议开启登录口令；通过域名或外网访问时必须开启。</span></div>
    <div class="set-row inline"><div><div class="lbl">启用登录口令</div><div class="hint" data-ref="h1"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="sw" aria-label="启用登录口令"></button></div></div>
    <div class="set-row"><div><div class="lbl" data-ref="pwL">设置口令</div><div class="hint" data-ref="pwH"></div></div><div class="ctl pw-ctl"><input class="textfield" type="password" style="width:180px" placeholder="当前口令" data-ref="old" autocomplete="current-password" aria-label="当前口令"><input class="textfield" type="password" style="width:180px" placeholder="新口令（至少 6 位）" data-ref="pw" autocomplete="new-password" aria-label="新口令"><span class="pw-meter" data-ref="meter"><i></i><i></i><i></i><i></i></span><button class="btn primary" type="button" data-ref="pwBtn" disabled>保存口令</button></div></div>
    <div class="set-row inline" data-k="auth_session_days"><div><div class="lbl">会话有效期</div><div class="hint">登录后多少天内免输口令（1–365 天）</div></div><div class="ctl" data-ref="days"></div></div>
    <div class="set-row inline" data-ref="outRow"><div><div class="lbl">退出登录</div><div class="hint">退出当前浏览器的登录状态</div></div><div class="ctl"><button class="btn danger" type="button" data-ref="out">${ic('lock')}退出登录</button></div></div>`;
  const x = collect(card);
  const dayRow = x.days.closest('.set-row'), days = numField(cfgItem('auth_session_days', '会话有效期', 'number', { min: 1, max: 365, unit: '天' }), dayRow);
  x.days.append(days);
  const minLen = () => +(S.cfg && S.cfg.password_min_len) || 6;
  const check = () => {
    const MIN = minLen(), v = x.pw.value, sc = (v.length >= MIN) + (v.length >= 10) + (/[A-Z]/.test(v) && /[a-z]/.test(v)) + (/\d/.test(v) && /[^\w]/.test(v));
    const col = sc <= 1 ? 'var(--crit)' : sc === 2 ? 'var(--warn-fill)' : 'var(--ink-1)';
    $$('i', x.meter).forEach((i, k) => { i.style.background = v && k < Math.max(1, sc) ? col : ''; });
    x.pwBtn.disabled = v.length < MIN || (pwSet() && !x.old.value);
  };
  x.pw.addEventListener('input', check); x.old.addEventListener('input', check);
  x.pw.addEventListener('keydown', e => { if (e.key === 'Enter' && !x.pwBtn.disabled) x.pwBtn.click(); });
  card._sync = () => {
    const has = pwSet(), on = authOn();
    x.sw.setAttribute('aria-checked', String(on));
    setText(x.h1, !has ? '尚未设置口令：先在下方设置口令再开启' : on ? '已开启：访问需要口令登录' : '已关闭：局域网内任何人可直接访问');
    setText(x.pwL, has ? '修改口令' : '设置口令'); setText(x.pwH, has ? `需输入当前口令；新口令至少 ${minLen()} 位，修改后其他设备需重新登录` : `至少 ${minLen()} 位，建议混合大小写、数字与符号`); x.pw.placeholder = `新口令（至少 ${minLen()} 位）`;
    x.old.hidden = !has; x.outRow.hidden = !(on && has); days.sync(); check();
  };
  x.sw.onclick = async () => {
    const on = x.sw.getAttribute('aria-checked') !== 'true';
    if (on && !pwSet()) { toast('请先设置口令', { icon: 'info' }); x.pw.focus(); return; }
    x.sw.setAttribute('aria-checked', String(on));
    const j = await saveCfg({ auth_enabled: on ? '1' : '0' }, on ? '已开启登录口令' : '已关闭登录口令', x.sw.closest('.set-row'));
    if (j && on) {
      const ss = await API.get('/api/auth/session').catch(() => null);
      if (ss && ss.logged_in) toast('登录口令已开启，当前浏览器保持登录', { icon: 'lock', ms: 4000 });
      else { toast('登录口令已开启，请用口令重新登录', { icon: 'lock', ms: 4000 }); setTimeout(() => { location.href = loginURL(); }, 1600); }
    }
  };
  x.pwBtn.onclick = async () => {
    const pw = x.pw.value, had = pwSet(), MIN = minLen();
    if (pw.length < MIN) { toast(`口令至少 ${MIN} 位`, { icon: 'alert' }); return; }
    if (had && !x.old.value) { toast('请输入当前口令', { icon: 'alert' }); x.old.focus(); return; }
    x.pwBtn.disabled = true;
    const j = await saveCfg(had ? { auth_password: pw, old_password: x.old.value } : { auth_password: pw }, had ? '登录口令' : '登录口令', x.pw.closest('.set-row'), true);
    if (j) { S.cfg.auth_password_set = true; S.cfg.auth_has_password = true; x.pw.value = ''; x.old.value = ''; toast(had ? '口令已更新，其他设备需要重新登录' : '口令已设置，可以开启登录口令了', { ms: 4200 }); }
    card._sync();
  };
  x.out.onclick = doLogout;
  card._sync();
}
async function doLogout() {
  try { await API.post('/api/auth/logout', {}); } catch (e) { /* 仍然跳转 */ }
  location.href = loginURL();
}
/** 下载：飞牛 iframe 沙箱会拦截 <a download>，改用新窗口 / 顶层导航触发 */
function doDownload(url) {
  let w = null;
  try { w = window.open(url, '_blank'); } catch (e) { w = null; }
  if (!w) { try { window.location.href = url; } catch (e) { /* ignore */ } }
  return false;
}
function renderData(card) {
  card.innerHTML = `<div data-ref="dir"></div>
    <div class="set-row block"><div><div class="lbl">导出</div><div class="hint">历史 / 流量 / 功耗 CSV 可用 Excel、WPS 打开；时间范围仅作用于这三项</div></div><div class="ctl" data-ref="rng"></div><div class="export-grid" data-ref="ex"></div></div>`;
  const x = collect(card);
  x.dir.append(setRow(cfgItem('data_dir', '数据保存目录', 'text', { mono: true, cancel: true, ph: () => S.cfg && S.cfg.data_dir_actual ? '当前默认：' + S.cfg.data_dir_actual : '留空 = 默认 localdata 目录', hint: '历史数据与界面设置的保存位置；可改为飞牛存储空间路径（如 /vol1/监控数据），留空为默认。保存后需在应用中心重启「飞牛监控」生效，旧数据不会自动迁移',
    validate: v => v && !v.startsWith('/') ? '请填写以 / 开头的绝对路径，留空表示默认目录' : '',
    set: (v, row) => saveCfg({ data_dir: v }, '数据目录（重启应用后生效）', row) })));
  let range = '7d';
  x.rng.append(makeSeg([['all', '全部'], ['7d', '7 天'], ['24h', '24 小时'], ['6h', '6 小时'], ['1h', '1 小时']], range, v => { range = v; }, 'sm'));
  const items = [
    ['history', '历史趋势', 'CSV · 全部指标', () => '/api/export?type=history&range=' + range],
    ['status', '当前状态快照', 'JSON · 概览 / 容器 / 进程', () => '/api/export?type=status'],
    ['report', '健康报告', 'HTML · 近 7 天 · 同一套阈值', () => '/api/report?format=html'],
    ['traffic', '流量统计', 'CSV · 网卡与容器计数', () => '/api/export?type=traffic&range=' + range],
    ['power', '功耗统计', 'CSV · 整机功耗序列', () => '/api/export?type=power&range=' + range],
  ];
  x.ex.innerHTML = items.map(([id, t, s]) => `<button class="export" type="button" data-x="${id}">${ic(id === 'report' ? 'shield' : 'download')}<span><b>${t}</b><small>${s}</small></span></button>`).join('');
  x.ex.addEventListener('click', e => { const b = e.target.closest('[data-x]'); if (!b) return; const it = items.find(i => i[0] === b.dataset.x); doDownload(it[3]()); toast(`正在导出${it[1]}…`, { icon: 'download' }); });
  card._sync = () => $$('.set-row', card).forEach(r => r._sync && r._sync());
}
/** 更新安装 / 下载到 NAS：优先 POST（新版后端），旧后端只接受 GET 时回退 */
async function updCall(path, body, getUrl) {
  try { return await API.post(path, body); } catch (e) { if (e.status === 404 || e.status === 405 || e.status === 501) return API.get(getUrl); throw e; }
}
function renderUpdate(card) {
  card.innerHTML = `<div class="update-card" data-ref="uc"><span class="app-ico">${ic('logo')}</span><div style="min-width:0"><b style="font-size:16px;font-weight:600" data-ref="ver">飞牛监控</b><div class="sub" data-ref="st"></div></div><div class="btns" data-ref="btns"></div><div class="update-notes" data-ref="notes" hidden></div><div class="upd-msg sub" data-ref="msg" hidden></div></div><div class="upd-rows" data-ref="rows"></div>`;
  const x = collect(card);
  const btn = (u, label, icon, cls = '') => `<button class="btn ${cls}" type="button" data-u="${u}">${icon ? ic(icon) : ''}${label}</button>`;
  const msg = t => { x.msg.hidden = !t; setText(x.msg, t || ''); };
  const draw = () => {
    const c = S.cfg || {}, st = c.update_status || {}, u = S.upd;
    setText(x.ver, `飞牛监控 v${c.version || '--'}`);
    setText(x.st, S.updBusy ? S.updBusy : st.installing ? '正在安装新版本…' : u && u.error && !u.ok ? '检查失败：' + u.error : (u && u.has_update) || st.has_update ? `发现新版本 v${(u && u.latest) || st.latest}` + (c.arch ? ` · ${c.arch}` : '') : [c.arch ? 'NAS 架构 ' + c.arch : '', st.last_check ? '上次检查 ' + st.last_check : '尚未检查', st.latest ? '线上最新 v' + st.latest : '', st.error ? '上次错误：' + st.error : ''].filter(Boolean).join(' · '));
    const has = (u && u.has_update) || st.has_update;
    setHTML(x.btns, S.updBusy ? '<span class="spin"></span>' : (has ? btn('go', '立即更新', 'download', 'primary') : '') + btn('check', '检查更新', 'refresh') + btn('nas', '下载到 NAS', 'folder', 'ghost') + btn('pc', '下载到本电脑', 'download', 'ghost'));
    const notes = u && u.has_update ? String(u.notes || '').replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, '').slice(0, 800).trim() : '';
    x.notes.hidden = !notes; setText(x.notes, notes);
    if (st.downloaded_file && !x.msg.textContent) msg('已下载到 NAS：' + st.downloaded_file);
  };
  x.uc.addEventListener('click', async e => {
    const b = e.target.closest('[data-u]'); if (!b) return; const a = b.dataset.u;
    if (a === 'check') {
      S.updBusy = '正在连接 GitHub…'; draw();
      try { S.upd = await API.get('/api/update/check?force=1'); if (S.cfg) S.cfg.update_status = Object.assign({}, S.cfg.update_status, { last_check: '刚刚', latest: S.upd.latest, has_update: S.upd.has_update, error: S.upd.error }); }
      catch (err) { S.upd = { ok: false, error: err.message }; }
      S.updBusy = ''; draw(); applyConfig();
      toast(S.upd.ok ? (S.upd.has_update ? `发现新版本 v${S.upd.latest}` : '已是最新版本') : '检查失败：' + (S.upd.error || ''), { icon: S.upd.ok ? 'check' : 'alert' });
    }
    if (a === 'nas') {
      const p = S.updDir || '';
      S.updBusy = '正在下载到 NAS…'; draw();
      try { const j = await updCall('/api/update/download', p.trim() ? { path: p.trim() } : {}, '/api/update/download?to=nas' + (p.trim() ? '&path=' + encodeURIComponent(p.trim()) : '')); msg(j.ok ? '已下载到 NAS：' + j.path + (j.sha256 ? '（SHA-256 ' + String(j.sha256).slice(0, 16) + '…）' : '') + (j.note ? ' —— ' + j.note : '') : '下载失败：' + (j.error || '未知错误')); }
      catch (err) { msg('下载失败：' + err.message); }
      S.updBusy = ''; draw();
    }
    if (a === 'pc') {
      S.updBusy = '正在经 NAS 代理下载到本电脑…'; draw();
      try {
        const r = await fetch('/api/update/download?to=browser', { credentials: 'same-origin' });
        const ct = r.headers.get('Content-Type') || '';
        if (!r.ok || ct.includes('json')) { const j = await r.json().catch(() => ({})); throw new Error(j.error || 'HTTP ' + r.status); }
        const blob = await r.blob(), m = (r.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/);
        const el = document.createElement('a'); el.href = URL.createObjectURL(blob); el.download = m ? m[1] : 'fnmonitor.fpk';
        document.body.appendChild(el); el.click(); el.remove(); setTimeout(() => URL.revokeObjectURL(el.href), 30000);
        msg('已开始下载到本电脑（若浏览器弹出保存对话框，选择保存位置即可）');
      } catch (err) { msg('下载失败：' + err.message); }
      S.updBusy = ''; draw();
    }
    if (a === 'go') {
      if (!confirm('将自动下载最新版本并直接安装（安装后服务自动重启，期间页面会短暂断开），是否继续？')) return;
      S.updBusy = '正在下载并安装新版本，请稍候…'; draw();
      try {
        const j = await updCall('/api/update/install', {}, '/api/update/install');
        if (j.ok) { msg(j.note || '安装完成，服务重启中…'); setTimeout(() => { location.href = location.pathname + '?v=' + Date.now(); }, 4000); }
        else { S.updBusy = ''; msg('更新失败：' + (j.error || '未知错误')); draw(); }
      } catch (err) { msg('更新请求中断（服务可能正在重启），页面将自动刷新…'); setTimeout(() => { location.href = location.pathname + '?v=' + Date.now(); }, 4000); }
    }
  });
  x.rows.append(setRow(cfgToggle('update_autoupdate', '自动更新', { hint: '后台每 6 小时检查一次，发现新版本自动下载并直接安装、重启服务' })));
  const dirRow = setRow({ k: 'updDir', label: '安装包保存目录', type: 'input', mono: true, placeholder: '留空 = 数据目录下的 update 文件夹', hint: '「下载到 NAS」的保存位置：留空为默认目录，或填写存储空间路径（/vol1/…）；下载后可在飞牛文件管理中查看，或到应用中心「手动安装」选择该文件升级', get: () => S.updDir || '', set: v => { S.updDir = v; } });
  x.rows.append(dirRow);
  card._sync = () => { draw(); $$('.set-row', x.rows).forEach(r => r._sync && r._sync()); };
  draw();
}
function renderAbout(card) {
  card.innerHTML = `<div class="about"><div class="brand-mark">${ic('logo')}</div><h4>飞牛监控 fnMonitor</h4><p>为飞牛 fnOS 打造的本地系统监控 · <span data-ref="v">v--</span></p>
    <p class="faint" style="margin-top:2px">纯本地运行 · 历史记录、界面设置与配置都保存在 NAS 的程序数据目录中，与访问用的电脑无关</p>
    <p class="about-credit">作者 <a href="https://github.com/MisiteQ" target="_blank" rel="noopener noreferrer">Misite齊（@MisiteQ）</a> · 项目 <a href="https://github.com/MisiteQ/fnmonitor" target="_blank" rel="noopener noreferrer">github.com/MisiteQ/fnmonitor</a><br>界面与功能贡献 <a href="https://github.com/iqiuzixuan" target="_blank" rel="noopener noreferrer">iqiuzixuan</a></p>
    <div style="display:flex;gap:8px;justify-content:center;margin-top:20px;flex-wrap:wrap"><a class="btn" href="https://github.com/MisiteQ/fnmonitor" target="_blank" rel="noopener noreferrer">${ic('ext')}项目主页</a><a class="btn" href="https://github.com/MisiteQ/fnmonitor/issues" target="_blank" rel="noopener noreferrer">${ic('help')}问题反馈</a><a class="btn" href="https://github.com/MisiteQ/fnmonitor/releases/latest" target="_blank" rel="noopener noreferrer">${ic('download')}版本发布</a><button class="btn" type="button" data-sheet="keys">${ic('key')}快捷键</button></div>
    <p class="st-crit" style="margin-top:14px" data-ref="upd" hidden></p></div>`;
  const x = collect(card);
  card._sync = () => { setText(x.v, 'v' + ((S.cfg && S.cfg.version) || '--')); x.upd.hidden = !S.hasUpdate; setText(x.upd, S.hasUpdate ? '发现新版本，可在上方「软件更新」一键更新，或下载后自行安装' : ''); };
  card._sync();
}

let settingsBuilt = false;
function buildSettings() {
  if (settingsBuilt) return; settingsBuilt = true;
  const nav = $('#setNav'), main = $('#setMain');
  SET_SCHEMA.forEach(g => {
    nav.insertAdjacentHTML('beforeend', `<a href="#/settings" data-g="${g.id}">${ic(g.icon)}${g.title}</a>`);
    const grp = mount(`<section class="set-group" id="set-${g.id}"><h3>${g.title}</h3><p>${g.desc || ''}</p><div class="card set-card"></div></section>`);
    const card = grp.querySelector('.set-card');
    if (g.custom) { g.custom(card); if (g.id === 'about' || g.id === 'update') card.classList.remove('set-card'); }
    else g.items.forEach(it => card.append(setRow(it)));
    main.append(grp);
  });
  const links = $$('a', nav), groups = $$('.set-group', main);
  let navLock = 0, navTick = false;
  const setActive = a => links.forEach(x => x.classList.toggle('on', x === a));
  nav.addEventListener('click', e => {
    const a = e.target.closest('a'); if (!a) return; e.preventDefault();
    setActive(a);                                    // 点击立即高亮，不等滚动
    const ms = reduced() ? 120 : 900;
    navLock = Date.now() + ms;                       // 平滑滚动期间不让滚动追踪改写高亮
    setTimeout(() => { navLock = 0; trackNav(); }, ms + 60);
    $('#set-' + a.dataset.g).scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' });
  });
  // 高亮跟随滚动位置：取「顶边已越过探测线」的最后一组；页面滚到底时高亮最后一组
  // （矮分组如「关于 / 软件更新」永远滚不进旧 IntersectionObserver 的观察带，导致高亮停在错误的项上）
  function trackNav() {
    if (Date.now() < navLock) return;
    const probe = innerHeight * 0.28;
    let act = 0;
    groups.forEach((g, i) => { if (g.getBoundingClientRect().top <= probe) act = i; });
    const de = document.documentElement;
    if (innerHeight + scrollY >= de.scrollHeight - 4) act = groups.length - 1;
    setActive(links[act]);
  }
  addEventListener('scroll', () => { if (!navTick) { navTick = true; requestAnimationFrame(() => { navTick = false; trackNav(); }); } }, { passive: true });
  links[0].classList.add('on');
  applyConfig();
}
function syncSettings() {
  if (!settingsBuilt) return;
  $$('#setMain .set-row').forEach(r => r._sync && r._sync());
  $$('#setMain .set-card, #setMain .set-group > .card').forEach(c => c._sync && c._sync());
}
function focusSetting(k) {
  go('settings');
  setTimeout(() => { const r = $(`.set-row[data-k="${k}"]`); if (!r) return; if (r.hidden) { S.weatherManual = true; syncSettings(); } r.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'center' }); flashRow(r); const i = r.querySelector('input'); i && i.focus({ preventScroll: true }); }, 80);
}
function focusGroup(g) { go('settings'); setTimeout(() => { const el = $('#set-' + g); el && el.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' }); }, 80); }


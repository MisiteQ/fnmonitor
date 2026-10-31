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
    cfgToggle('disk_scan_enabled', '硬盘巡检', { hint: '定时全盘读取 SMART 并分级，盘位卡片的健康状态来自这里', help: '巡检在后台按固定周期执行：全盘 smartctl → 红黄绿分级 → 存入本机数据库，盘位卡片只读这份结果，打开页面不会再逐盘读取。关闭后不再巡检，盘位显示「未巡检」。休眠中的硬盘会被跳过（带 -n standby，不会唤醒），沿用上次结论。' }),
    cfgItem('disk_scan_interval_h', '巡检间隔', 'seg', { options: [[6, '6 小时'], [12, '12 小时'], [24, '24 小时'], [72, '3 天'], [168, '7 天']], hint: '间隔越短发现退化越及时，读取次数也越多；14 块盘一轮约 15–20 秒' }),
    { k: 'diskScanNow', label: '立即巡检', type: 'action', button: '现在巡检一次',
      hint: '不等下一个周期，立刻跑一轮全盘 SMART 并更新盘位分级；换盘或刚装完想马上看到结果时用',
      run: async b => {
        b.disabled = true;
        try {
          const j = await API.post('/api/disk_scan/now', {});
          if (j && j.ok) { toast(j.message || '已开始巡检', { key: 'scan', icon: 'check', ms: 4000 }); setTimeout(() => Poll.run('overview'), 16000); }
          else toast((j && j.message) || '启动失败', { icon: 'alert', ms: 4000 });
        } catch (e) { toast('启动失败 · ' + String(e.message).slice(0, 60), { icon: 'alert', ms: 4000 }); }
        b.disabled = false;
      } },
    { k: 'scanLog', label: '巡检历史', type: 'action', button: '查看巡检记录',
      hint: '每次巡检一行：有变动记变动内容，没变动记「未见异常」；未见异常保留 7 天，有变动的永久保留直到手动清理',
      run: () => openSheet('scanlog') },
    cfgToggle('ec_fan_probe', '实验性 · EC 风扇读取', { hint: '通过硬件 I/O 端口读取部分商用机（NEC / 联想）风扇转速，默认关闭', help: '仅在标准 hwmon / IPMI 读不到风扇时使用。该方式直接访问主板 EC 端口（只读），个别机型可能不兼容，如出现异常请关闭。' }),
  ] },
  { id: 'notify', title: '通知', icon: 'bell', desc: '硬盘巡检发现故障变化时，把消息推送到邮箱或微信；两个通道各自独立，测试也各测各的。', custom: renderNotify },
  { id: 'idrac', title: 'iDRAC', icon: 'server', desc: '戴尔服务器带外管理：直连 iDRAC 读风扇转速、温度、功耗与硬件日志（只读，不改服务器任何设置）。', custom: renderIdrac },
  { id: 'ignored', title: '已忽略的提醒', icon: 'eye-off', desc: '在 健康检查 中忽略的问题会列在这里：不计入健康分与首页结论；问题状态发生变化（如重映射扇区数增加、级别从留意升为严重、未运行的容器变化）时会自动重新提醒。', custom: renderIgnored },
  { id: 'look', title: '外观', icon: 'palette', items: [
    uiItem('theme', '外观模式', 'seg', { options: [['light', '浅色'], ['dark', '深色'], ['auto', '跟随系统']] }),
    { k: 'colorspec', label: '色彩规范', type: 'colorspec', wide: true, hint: '概览安静、分析丰富：颜色按层级分工，紧急程度覆盖一切' },
    uiItem('accent', '主色', 'accent', { hint: '主色用于界面与概览层；趋势图使用固定的数据实体色，换主色不必重新认色' }),
    uiItem('dotMatrix', '点阵数字', 'toggle', { hint: '关键读数使用 5×7 点阵字形，关闭后改用常规数字' }),
    uiItem('fontScale', '字体大小', 'seg', { options: [[0.9, '较小'], [1, '标准'], [1.1, '较大'], [1.25, '特大']] }),
    uiItem('motion', '动效', 'toggle', { hint: '系统开启“减少动态效果”时始终关闭' }),
    { k: 'uiSync', label: '界面设置同步', type: 'action', button: '立即同步', hint: () => `外观、布局与阈值保存在 NAS：<span class="mono">${esc((S.cfg && S.cfg.ui_file) || '数据目录/ui.json')}</span>，换设备或清缓存后自动恢复`, run: async b => { b.disabled = true; const okk = await postUI(); b.disabled = false; toast(okk ? '已同步到 NAS' : '同步失败，请检查连接', okk ? {} : { icon: 'alert' }); } },
    { k: 'resetLayout', label: '布局', type: 'action', button: '恢复默认布局', run: () => { Object.keys(LAYOUT_DEFAULT).forEach(resetLayout); toast('已恢复默认布局'); syncSettings(); } },
    { k: 'resetUI', label: '一键初始化界面', type: 'action', button: '恢复默认界面', danger: true, hint: '外观、主色、字号、布局与隐藏状态恢复默认（提醒阈值保持不变）', run: resetUI },
  ] },
  { id: 'blocks', title: '版块显示', icon: 'layout',
    desc: '按页面列出所有版块，关掉的版块不显示，后端也会尽量停掉对应采集（省 CPU 与磁盘 IO）。顶部「一句话结论 / 健康分」始终显示，它依赖 CPU、内存、容量、CPU 温度、阵列与容器列表——这几类数据无论怎么关都会继续采集，否则结论会失真。',
    custom: renderBlocks },
  { id: 'auth', title: '登录口令', icon: 'lock', desc: '开启后，访问飞牛监控需要先输入口令。', custom: renderAuth },
  { id: 'data', title: '数据', icon: 'folder', custom: renderData },
  { id: 'update', title: '软件更新', icon: 'download', custom: renderUpdate },
  { id: 'about', title: '关于', icon: 'info', custom: renderAbout },
];
/* ---------------------------------------------------------------------
   版块显示：直接读写 UI.hidden，与「编辑布局（E 键）」里的开关是同一份状态。
   BLOCK_KEEP_COLLECT 必须与后端 BLOCK_DEPS（app/server.py）以及顶部健康结论的
   依赖保持一致：这些版块关掉后页面不再渲染，但后端仍要采它们的一部分数据给
   顶部结论用，所以提示文案不能说「隐藏后停止采集」。
   --------------------------------------------------------------------- */
const BLOCK_KEEP_COLLECT = ['metrics', 'storage', 'thermal', 'network', 'energy', 'containers'];
const BLOCK_STOP_COLLECT = ['procs', 'apps', 'ports', 'vms'];
const BLOCK_PAGE_LABEL = { overview: '总览', docker: '容器', vms: '虚拟机' };

function renderBlocks(card) {
  card.innerHTML = Object.keys(LAYOUT_DEFAULT).map(p => {
    const rows = LAYOUT_DEFAULT[p].map(id => {
      if (!MODS[id]) return '';   // 本机不支持（如没装 virsh 的虚拟机页）就不列
      const title = MODS[id].title;
      const hint = BLOCK_KEEP_COLLECT.includes(id) ? '隐藏后仍会采集：顶部健康结论依赖它'
        : BLOCK_STOP_COLLECT.includes(id) ? '隐藏后停止采集，省 CPU'
          : '隐藏后不再显示';
      return `<div class="set-row inline blk-row" data-blk="${p}:${id}">
        <div><div class="lbl">${esc(title)}</div><div class="hint">${hint}</div></div>
        <div class="ctl"><button class="switch" type="button" role="switch" aria-checked="true" aria-label="显示「${esc(title)}」"></button></div>
      </div>`;
    }).join('');
    return rows ? `<div class="set-sub">${BLOCK_PAGE_LABEL[p] || p}</div>` + rows : '';
  }).join('') +
    `<div class="set-row inline"><div><div class="lbl">全部显示</div><div class="hint">把所有版块恢复为显示状态，排序不变</div></div><div class="ctl"><button class="btn" type="button" data-blkall="1">${ic('eye')}全部显示</button></div></div>`;

  const split = key => { const i = key.indexOf(':'); return [key.slice(0, i), key.slice(i + 1)]; };
  card._sync = () => {
    $$('[data-blk]', card).forEach(row => {
      const [p, id] = split(row.dataset.blk), sw = row.querySelector('.switch');
      if (!sw) return;
      const off = (UI.hidden[p] || []).includes(id);
      sw.setAttribute('aria-checked', String(!off));
      row.classList.toggle('blk-off', off);
    });
  };
  card.addEventListener('click', e => {
    if (e.target.closest('[data-blkall]')) {
      Object.keys(UI.hidden).forEach(p => { UI.hidden[p] = []; });
      Object.keys(LAYOUT_DEFAULT).forEach(applyLayout);
      saveUI(); syncSettings(); toast('已显示全部版块');
      return;
    }
    const sw = e.target.closest('.switch'); if (!sw) return;
    const box = sw.closest('[data-blk]'); if (!box) return;
    const [p, id] = split(box.dataset.blk), hid = UI.hidden[p] || [], off = !hid.includes(id);
    UI.hidden[p] = off ? hid.concat(id) : hid.filter(x => x !== id);
    applyLayout(p); saveUI(); syncSettings();
    const title = MODS[id] ? MODS[id].title : id;
    toast(off ? `已隐藏 · ${title}` : `已显示 · ${title}`, { key: 'blk' });
  });
  card._sync();
}

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
/* ---- 通知：硬盘告警推到邮箱 / 微信 ---- */
let NB = null;
const NOTIFY_PROVIDERS = [['qq', 'QQ 邮箱', 'smtp.qq.com', 465, 'ssl'], ['163', '163 邮箱', 'smtp.163.com', 465, 'ssl'],
  ['126', '126 邮箱', 'smtp.126.com', 465, 'ssl'], ['exmail', '腾讯企业邮', 'smtp.exmail.qq.com', 465, 'ssl'],
  ['outlook', 'Outlook', 'smtp.office365.com', 587, 'starttls'], ['custom', '自定义', '', 465, 'ssl']];
async function loadNotify(force) {
  if (NB && !force) return NB;
  try { NB = await API.get('/api/notify'); }
  catch (e) { NB = Object.assign({}, NB || {}, { load_error: e.message || '读取失败' }); }
  return NB;
}
function renderNotify(card) {
  // 各渠道字段定义：type=text/password/number/seg/textarea，advanced 折叠，secret 保密字段
  const SCHEMA = {
    mail: { color: '#4f8cff', fields: [
      { key: 'provider', label: '邮箱服务商', type: 'seg', options: NOTIFY_PROVIDERS.map(p => [p[0], p[1]]), hint: '选择后自动填入服务器、端口与加密方式' },
      { key: 'host', label: 'SMTP 服务器', type: 'text', mono: true, placeholder: 'smtp.qq.com' },
      { key: 'port', label: '端口', type: 'number', min: 1, max: 65535 },
      { key: 'secure', label: '加密方式', type: 'seg', options: [['ssl', 'SSL'], ['starttls', 'STARTTLS'], ['none', '不加密']] },
      { key: 'user', label: '发件账号', type: 'text', placeholder: 'you@qq.com' },
      { key: 'password', label: '授权码', type: 'password', secret: true, hint: '邮箱网页端开启 SMTP 后生成的授权码，不是登录密码' },
      { key: 'to', label: '收件人', type: 'text', placeholder: '多个用英文逗号分隔' },
    ]},
    sct: { color: '#07c160', fields: [
      { key: 'key', label: 'SendKey', type: 'password', secret: true, placeholder: 'SCT…', hint: 'sct.ftqq.com 注册并关注「方糖」服务号后复制' },
    ]},
    wechat: { color: '#2dc100', fields: [
      { key: 'url', label: 'Webhook 地址', type: 'password', secret: true, mono: true, placeholder: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…', hint: '群机器人 → 添加机器人 → 复制 Webhook' },
      { key: 'msg_type', label: '消息类型', type: 'seg', options: [['markdown', 'Markdown'], ['text', '纯文本']], advanced: true },
    ]},
    dingtalk: { color: '#1677ff', fields: [
      { key: 'url', label: 'Webhook 地址', type: 'password', secret: true, mono: true, placeholder: 'https://oapi.dingtalk.com/robot/send?access_token=…', hint: '群设置 → 智能群助手 → 添加机器人 → 自定义' },
      { key: 'secret', label: '加签密钥', type: 'password', secret: true, mono: true, placeholder: 'SEC…', advanced: true, hint: '安全设置选了「加签」时填，未选留空' },
      { key: 'msg_type', label: '消息类型', type: 'seg', options: [['markdown', 'Markdown'], ['text', '纯文本']], advanced: true },
    ]},
    feishu: { color: '#3370ff', fields: [
      { key: 'url', label: 'Webhook 地址', type: 'password', secret: true, mono: true, placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/…', hint: '群设置 → 群机器人 → 添加机器人 → 自定义' },
      { key: 'msg_type', label: '消息类型', type: 'seg', options: [['text', '纯文本'], ['post', '富文本']], advanced: true },
    ]},
    bark: { color: '#ff7a00', fields: [
      { key: 'url', label: '推送地址', type: 'password', secret: true, mono: true, placeholder: 'https://api.day.app/…', hint: 'iOS App「Bark」里复制的服务器地址' },
      { key: 'icon', label: '图标 URL', type: 'text', mono: true, placeholder: 'https://…/icon.png', advanced: true },
      { key: 'sound', label: '铃声', type: 'text', mono: true, placeholder: 'alarm', advanced: true },
      { key: 'group', label: '分组', type: 'text', mono: true, placeholder: 'fnmonitor', advanced: true },
      { key: 'jump_url', label: '跳转链接', type: 'text', mono: true, placeholder: 'https://…', advanced: true },
    ]},
    pushplus: { color: '#f5a623', fields: [
      { key: 'token', label: 'Token', type: 'password', secret: true, mono: true, placeholder: 'PushPlus Token', hint: 'pushplus.plus 登录后在「发送消息」页复制' },
      { key: 'channel', label: '推送渠道', type: 'seg', options: [['wechat', '微信公众号'], ['app', 'App'], ['extension', '插件/桌面'], ['webhook', 'Webhook'], ['clawbot', 'ClawBot'], ['qq', 'QQ'], ['cp', '企微应用'], ['mail', '邮箱']], advanced: true },
      { key: 'template', label: '消息格式', type: 'seg', options: [['txt', 'TXT'], ['html', 'HTML'], ['markdown', 'Markdown'], ['json', 'JSON']], advanced: true, hint: 'PushPlus 的消息渲染格式' },
      { key: 'topic', label: '群组编码', type: 'text', mono: true, placeholder: '群组 topic', advanced: true },
      { key: 'option', label: '渠道配置', type: 'text', mono: true, placeholder: '渠道配置编码', advanced: true, hint: 'webhook/cp/mail/qq 渠道需填' },
    ]},
    webhook: { color: '#8e44ad', fields: [
      { key: 'url', label: 'Webhook 地址', type: 'password', secret: true, mono: true, placeholder: 'https://your-server.com/webhook', hint: '默认 POST JSON {title, content}，可在下方自定义' },
      { key: 'method', label: '请求方法', type: 'seg', options: [['POST', 'POST'], ['GET', 'GET'], ['PUT', 'PUT']], advanced: true },
      { key: 'content_type', label: 'Content-Type', type: 'seg', options: [['json', 'JSON'], ['form', 'FORM'], ['text', 'TEXT']], advanced: true },
      { key: 'headers', label: '自定义请求头', type: 'textarea', mono: true, placeholder: 'Authorization: Bearer xxx\nX-Custom: value', advanced: true, hint: '每行 "K: V"，或直接写 JSON 对象' },
      { key: 'body', label: 'Body 模板', type: 'textarea', mono: true, placeholder: '{"msg":"{title}","detail":"{content}"}', advanced: true, hint: '支持 {title}{content}{text}{time} 占位符；留空用默认 {"title":"...","content":"..."}' },
    ]},
    wecom_app: { color: '#00d4b4', fields: [
      { key: 'corp_id', label: 'CorpID', type: 'text', mono: true, placeholder: '企业 ID', hint: '企业微信管理后台 → 我的企业 → 企业 ID' },
      { key: 'corp_secret', label: 'Secret', type: 'password', secret: true, mono: true, placeholder: '应用 Secret' },
      { key: 'agent_id', label: 'AgentId', type: 'text', mono: true, placeholder: '1000002' },
      { key: 'to_user', label: '接收人', type: 'text', mono: true, placeholder: '@all', hint: '默认 @all；多人用 | 分隔' },
      { key: 'to_party', label: '接收部门', type: 'text', mono: true, placeholder: '1|2', advanced: true },
      { key: 'to_tag', label: '接收标签', type: 'text', mono: true, placeholder: '1|2', advanced: true },
      { key: 'msg_type', label: '消息类型', type: 'seg', options: [['text', '纯文本'], ['markdown', 'Markdown']], advanced: true },
    ]},
  };
  const TPL_HINT = '留空用默认格式。可用占位符：{title}=告警标题，{content}=告警正文，{text}=正文别名，{time}=当前时间';
  const TYPE_LABELS = { mail: '邮件', sct: 'Server酱', wechat: '企业微信群机器人', dingtalk: '钉钉群机器人',
    feishu: '飞书群机器人', bark: 'Bark', pushplus: 'PushPlus', webhook: '通用 Webhook', wecom_app: '企业微信应用' };
  // 状态：channels 是可变数组，每项 {id,type,name,enabled,config:{...}}
  let f = { channels: [], hw_enabled: 0, hw_time: '08:00', notify_title_prefix: '飞牛监控',
    notify_dnd_enabled: 0, notify_dnd_start: '23:00', notify_dnd_end: '08:00', notify_minimal: 0,
    report_enabled: 0, report_detail: 'detail',
    report_schedule: { type: 'daily', time: '08:00', weekday: 0, day: 1, interval_minutes: 60 },
    report_modules: ['system', 'cpu', 'memory', 'disk', 'network'] };
  // 报告模块定义：key → [中文标签, 分类]
  const REPORT_MODS = {
    system: ['系统信息', '系统'], cpu: ['CPU', '系统'], memory: ['内存', '系统'], load: ['负载', '系统'], gpu: ['显卡', '系统'],
    disk: ['硬盘状态', '存储'], smart: ['SMART 健康', '存储'], storage: ['存储池/阵列', '存储'],
    network: ['网络', '网络'], temp: ['温度', '散热'], fan: ['风扇', '散热'], power: ['功耗与电费', '功耗'],
    docker: ['Docker 容器', '容器'], vms: ['虚拟机', '容器'],
    idrac: ['iDRAC 硬件', '硬件'], ups: ['UPS', '硬件'], weather: ['天气', '其他'],
  };
  const REPORT_MOD_ORDER = ['system', 'cpu', 'memory', 'load', 'gpu', 'disk', 'smart', 'storage', 'network', 'temp', 'fan', 'power', 'docker', 'vms', 'idrac', 'ups', 'weather'];
  let chSeq = 0; // 新建渠道 id 自增
  const newId = () => 'ch_' + Date.now().toString(36) + (chSeq++);

  card.innerHTML = `<div class="note info notify-note">${ic('bell')}<span><b>两条独立的告警线，可分别开关</b>：<br>
· <b>硬盘巡检</b>：新增故障 / 加重 / 已恢复各发一条，同一块盘的同一状态只发一次，发送失败下一轮巡检自动重试（跟着「硬盘巡检」走，需先在 网络与硬盘 里打开巡检）；<br>
· <b>硬件告警</b>：iDRAC 的 SEL 硬件日志与风扇 / 温度 / 电源，攒到每天指定时刻统一发一封，<b>到点前已经恢复的整条不发</b>，一直没恢复的每 7 天再提醒，正文带发生时间。</span></div>
    <div class="notify-channels" data-ref="chList"></div>
    <div style="display:flex;gap:8px;margin:8px 0 16px">
      <button class="btn" type="button" data-ref="addCh" style="flex:1">+ 添加推送渠道</button>
      <button class="btn" type="button" data-ref="guideBtn" style="flex:0 0 auto">? 使用说明</button>
    </div>
    <div class="set-row"><div><div class="lbl" style="margin-top:6px">全局通知设置</div><div class="hint">标题前缀、勿扰模式、极简推送，对所有渠道生效</div></div></div>
    <div class="set-row"><div><div class="lbl">标题前缀</div><div class="hint">每条通知标题前加的前缀，默认「飞牛监控」</div></div><div class="ctl"><input class="textfield" data-ref="titlePrefix" placeholder="飞牛监控" maxlength="32" aria-label="通知标题前缀"></div></div>
    <div class="set-row inline"><div><div class="lbl">极简推送</div><div class="hint" data-ref="hMin"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="swMin" aria-label="极简推送"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">勿扰模式</div><div class="hint" data-ref="hDnd"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="swDnd" aria-label="勿扰模式"></button></div></div>
    <div class="set-row"><div><div class="lbl">勿扰时段</div><div class="hint">该时段内的通知会缓存，结束后汇总成一条发送</div></div><div class="ctl" style="display:flex;gap:6px;align-items:center"><input class="textfield mono" style="width:90px" type="time" data-ref="dndStart" aria-label="勿扰开始时间"><span>至</span><input class="textfield mono" style="width:90px" type="time" data-ref="dndEnd" aria-label="勿扰结束时间"></div></div>
    <div class="set-row inline"><div><div class="lbl">硬件告警</div><div class="hint" data-ref="hHw"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="swHw" aria-label="硬件告警"></button></div></div>
    <div class="set-row"><div><div class="lbl">硬件告警发送时间</div><div class="hint">每天到这个时刻汇总发一封：只发仍未恢复的，正文带发生时间；持续没恢复的每 7 天再提醒一次，通知过又恢复的补一封「已恢复」</div></div><div class="ctl"><input class="textfield mono" style="width:110px" type="time" data-ref="hwTime" aria-label="硬件告警发送时间"></div></div>
    <div class="set-row"><div><div class="lbl" style="margin-top:6px">定时推送报告</div><div class="hint">按设定周期把系统各模块数据汇总推送一条报告；与告警推送互不干扰</div></div></div>
    <div class="set-row inline"><div><div class="lbl">开启定时报告</div><div class="hint" data-ref="hRep"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="swRep" aria-label="开启定时报告"></button></div></div>
    <div class="set-row" data-ref="repSchRow"><div><div class="lbl">推送周期</div><div class="hint">到点自动发一条汇总报告</div></div><div class="ctl" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <select class="textfield" data-ref="repType" aria-label="推送周期类型" style="width:auto">
        <option value="hourly">每小时</option>
        <option value="daily">每天</option>
        <option value="weekly">每周</option>
        <option value="monthly">每月</option>
        <option value="interval">自定义间隔</option>
      </select>
      <input class="textfield mono" type="time" data-ref="repTime" aria-label="推送时间" style="width:90px">
      <select class="textfield" data-ref="repWeekday" aria-label="星期" style="width:auto;display:none">
        <option value="0">周一</option><option value="1">周二</option><option value="2">周三</option><option value="3">周四</option><option value="4">周五</option><option value="5">周六</option><option value="6">周日</option>
      </select>
      <input class="textfield mono" type="number" min="1" max="31" data-ref="repDay" aria-label="日期" style="width:60px;display:none" placeholder="日">
      <input class="textfield mono" type="number" min="1" max="10080" data-ref="repInterval" aria-label="间隔分钟" style="width:90px;display:none" placeholder="分钟">
    </div></div>
    <div class="set-row wide" data-ref="repModRow"><div><div class="lbl">推送内容</div><div class="hint">勾选要在报告里推送的模块，并选择详细程度</div></div><div class="ctl" style="flex-direction:column;align-items:stretch;gap:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="set-sub" style="margin:0">详细程度</span>
        <div data-ref="repDetailSeg"></div>
      </div>
      <div class="rep-mods" data-ref="repMods"></div>
    </div></div>
    <div class="set-row" data-ref="repTestRow"><div><div class="lbl">测试报告</div><div class="hint" data-ref="hRepTest">点「发送测试报告」立刻发一条当前数据的汇总报告</div></div><div class="ctl"><button class="btn" type="button" data-ref="repTestBtn">发送测试报告</button></div></div>
    <div class="set-row"><div><div class="lbl">保存设置</div><div class="hint" data-ref="hMsg"></div></div><div class="ctl"><button class="btn primary" type="button" data-ref="save">保存</button></div></div>`;
  const x = collect(card);

  // 生成单个字段的 HTML + 绑定
  function fieldHtml(chIdx, field, value) {
    const k = field.key;
    const id = `f_${chIdx}_${k}`;
    const adv = field.advanced ? ' ch-advanced' : '';
    const mono = field.mono ? ' mono' : '';
    const label = `<div class="lbl">${esc(field.label)}</div>` + (field.hint ? `<div class="hint">${esc(field.hint)}</div>` : '');
    let ctl = '';
    if (field.type === 'seg') {
      ctl = `<div class="ctl" data-seg="${id}" data-k="${k}"></div>`;
    } else if (field.type === 'textarea') {
      ctl = `<div class="ctl"><textarea class="textfield${mono}" style="width:100%;min-height:56px;font-size:12px" data-id="${id}" data-k="${k}" placeholder="${esc(field.placeholder || '')}" spellcheck="false">${esc(value || '')}</textarea></div>`;
    } else if (field.type === 'number') {
      ctl = `<div class="ctl"><input class="textfield${mono}" style="width:86px" type="number" min="${field.min || ''}" max="${field.max || ''}" data-id="${id}" data-k="${k}" value="${esc(value || '')}" aria-label="${esc(field.label)}"></div>`;
    } else {
      const pwd = field.type === 'password';
      ctl = `<div class="ctl"><input class="textfield${mono}" type="${pwd ? 'password' : 'text'}" data-id="${id}" data-k="${k}" value="${pwd ? '' : esc(value || '')}" placeholder="${esc(field.placeholder || '')}" spellcheck="false" autocomplete="${pwd ? 'new-password' : 'off'}" aria-label="${esc(field.label)}"></div>`;
    }
    return `<div class="set-row${adv}"><div>${label}</div>${ctl}</div>`;
  }

  // 渲染一个渠道卡片
  function renderChannel(ch, idx) {
    const schema = SCHEMA[ch.type] || { fields: [] };
    const cfg = ch.config || {};
    const isSet = k => cfg[k + '_set'] === true || cfg[k + '_set'] === 1;
    // 普通字段 + 高级字段分别渲染
    const normal = schema.fields.filter(f2 => !f2.advanced);
    const adv = schema.fields.filter(f2 => f2.advanced);
    const rows = normal.map(f2 => fieldHtml(idx, f2, cfg[f2.key])).join('');
    const advRows = adv.map(f2 => fieldHtml(idx, f2, cfg[f2.key])).join('');
    // 消息模板字段（webhook 已有 body，其它渠道加 template）
    const tplKey = ch.type === 'pushplus' ? 'msg_template' : 'template';
    const tplField = ch.type === 'webhook' ? '' :
      `<div class="set-row ch-advanced"><div><div class="lbl">消息模板</div><div class="hint">${TPL_HINT}</div></div><div class="ctl"><textarea class="textfield mono" style="width:100%;min-height:56px;font-size:12px" data-id="f_${idx}_tpl" data-k="${tplKey}" placeholder="**{title}**\n{content}" spellcheck="false">${esc(cfg[tplKey] || '')}</textarea></div></div>`;
    const hasAdv = advRows || tplField;
    return `<div class="ch-group" data-ch="${esc(ch.type)}" data-idx="${idx}">
      <div class="ch-head">
        <div class="ch-name-row" style="display:flex;align-items:center;gap:8px;min-width:0">
          <input class="ch-name-input" style="flex:1;min-width:0;background:transparent;border:0;font-weight:600;font-size:14px;color:var(--ink);outline:none" data-k="name" value="${esc(ch.name || TYPE_LABELS[ch.type] || ch.type)}" aria-label="渠道名称">
        </div>
        <div class="ch-ctl" style="display:flex;align-items:center;gap:8px">
          <span class="ch-type-badge" style="font-size:11px;color:var(--ink-3);background:var(--bg-2);padding:2px 8px;border-radius:10px;white-space:nowrap">${esc(TYPE_LABELS[ch.type] || ch.type)}</span>
          <span class="test-msg" data-ref="mCh"></span>
          <button class="btn" type="button" data-ref="tCh">测试</button>
          <button class="switch" type="button" role="switch" aria-checked="${!!ch.enabled}" data-ref="swCh" aria-label="启用该渠道"></button>
          <button class="btn" type="button" data-ref="delCh" aria-label="删除渠道" style="padding:4px 8px;color:var(--warn)">✕</button>
        </div>
      </div>
      <div class="ch-body">${rows}${advRows}${tplField}</div>
      ${hasAdv ? '<div class="ch-adv-toggle" style="padding:6px 14px;border-top:1px solid var(--line)">高级设置 ▾</div>' : ''}
    </div>`;
  }

  function renderAll() {
    // 保留滚动位置与高级设置展开状态，避免重渲染后页面跳动
    const scrollTop = window.scrollY;
    const advStates = {};
    x.chList.querySelectorAll('.ch-group').forEach(g => { advStates[g.dataset.idx] = g.classList.contains('show-adv'); });
    x.chList.innerHTML = f.channels.map((ch, i) => renderChannel(ch, i)).join('');
    bindChannels();
    x.chList.querySelectorAll('.ch-group').forEach(g => { if (advStates[g.dataset.idx]) g.classList.add('show-adv'); });
    // 恢复展开/折叠按钮文字
    x.chList.querySelectorAll('.ch-adv-toggle').forEach(tog => {
      const show = tog.closest('.ch-group').classList.contains('show-adv');
      tog.textContent = show ? '高级设置 ▴' : '高级设置 ▾';
    });
    window.scrollTo(0, scrollTop);
  }

  // 绑定所有渠道卡片的事件
  function bindChannels() {
    x.chList.querySelectorAll('.ch-group').forEach(group => {
      const idx = +group.dataset.idx;
      const ch = f.channels[idx];
      if (!ch) return;
      const r = collect(group);
      const cfg = ch.config;
      // 名称
      const nameEl = group.querySelector('[data-k="name"]');
      nameEl.addEventListener('input', () => { ch.name = nameEl.value; });
      // 开关
      r.swCh.addEventListener('click', () => {
        const on = r.swCh.getAttribute('aria-checked') !== 'true';
        r.swCh.setAttribute('aria-checked', String(on)); ch.enabled = on ? 1 : 0;
      });
      // 删除
      r.delCh.addEventListener('click', () => {
        f.channels.splice(idx, 1); renderAll(); sync();
      });
      // 测试
      r.tCh.addEventListener('click', () => testChannel(ch, r.tCh, r.mCh));
      // 高级设置折叠
      const tog = group.querySelector('.ch-adv-toggle');
      if (tog) tog.addEventListener('click', () => {
        const show = group.classList.toggle('show-adv');
        tog.textContent = show ? '高级设置 ▴' : '高级设置 ▾';
      });
      // 字段输入
      group.querySelectorAll('[data-k]').forEach(el => {
        const k = el.dataset.k;
        if (k === 'name') return;
        el.addEventListener('input', () => {
          if (el.tagName === 'TEXTAREA' || el.type === 'text' || el.type === 'password') {
            cfg[k] = el.value;
          } else if (el.type === 'number') {
            cfg[k] = parseInt(el.value, 10) || 0;
          }
        });
      });
      // seg 控件
      group.querySelectorAll('[data-seg]').forEach(segEl => {
        const k = segEl.dataset.k;
        const field = (SCHEMA[ch.type]?.fields || []).find(f2 => f2.key === k);
        if (!field) return;
        const seg = makeSeg(field.options, cfg[k] || field.options[0][0], v => { cfg[k] = v; }, 'sm');
        segEl.innerHTML = ''; segEl.appendChild(seg);
        // mail provider 联动 host/port/secure
        if (ch.type === 'mail' && k === 'provider') {
          seg.addEventListener('click', () => {
            const pv = NOTIFY_PROVIDERS.find(p => p[0] === cfg.provider);
            if (pv && pv[2]) {
              cfg.host = pv[2]; cfg.port = pv[3]; cfg.secure = pv[4];
              const hostEl = group.querySelector('[data-k="host"]');
              const portEl = group.querySelector('[data-k="port"]');
              if (hostEl) hostEl.value = pv[2];
              if (portEl) portEl.value = pv[3];
              const secSeg = group.querySelector('[data-seg$="secure"]');
              if (secSeg) { const s = secSeg.querySelector('.seg'); if (s && s.set) s.set(pv[4]); }
            }
          });
        }
      });
      // 保密字段 placeholder（已保存则提示留空不修改）
      const schema = SCHEMA[ch.type] || { fields: [] };
      schema.fields.forEach(f2 => {
        if (!f2.secret) return;
        const el = group.querySelector(`[data-k="${f2.key}"]`);
        if (el && cfg[f2.key + '_set']) el.placeholder = '已保存 · 留空不修改';
      });
    });
  }

  // 测试单个渠道
  async function testChannel(ch, btn, msg) {
    btn.disabled = true; setText(msg, '发送中…');
    try {
      const j = await API.post('/api/notify/test', { channel: ch.id });
      if (j && j.ok) {
        setText(msg, '已发送');
        toast((ch.name || TYPE_LABELS[ch.type] || '渠道') + ' 测试消息已发出', { icon: 'check', ms: 5000 });
      } else {
        setText(msg, '');
        toast('测试失败 · ' + String(((j && j.error) || '未知错误')).slice(0, 100), { icon: 'alert', ms: 7000 });
      }
    } catch (e) { setText(msg, ''); toast('测试失败 · ' + String(e.message).slice(0, 80), { icon: 'alert', ms: 6000 }); }
    btn.disabled = false;
  }

  // 添加渠道
  x.guideBtn.addEventListener('click', () => openSheet('notifyguide'));
  x.addCh.addEventListener('click', () => {
    // 弹窗选类型（用 prompt 简单实现，或做成下拉）
    const types = Object.keys(SCHEMA);
    // 简单做法：弹出一个选择层
    const pick = mount(`<div class="ch-pick" style="position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:9999">
      <div style="background:var(--bg);border-radius:12px;padding:16px;min-width:280px;max-width:90vw">
        <div style="font-weight:600;margin-bottom:12px">选择推送渠道类型</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          ${types.map(t => `<button class="btn" type="button" data-t="${t}" style="justify-content:flex-start"><span style="width:10px;height:10px;border-radius:50%;background:${SCHEMA[t].color};display:inline-block;margin-right:8px"></span>${esc(TYPE_LABELS[t])}</button>`).join('')}
        </div>
        <button class="btn" type="button" data-ref="cancel" style="width:100%;margin-top:12px">取消</button>
      </div></div>`);
    document.body.appendChild(pick);
    const close = () => pick.remove();
    pick.querySelector('[data-ref="cancel"]').addEventListener('click', close);
    pick.addEventListener('click', e => { if (e.target === pick) close(); });
    pick.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', () => {
      const t = b.dataset.t;
      const defaults = {};
      (SCHEMA[t].fields || []).forEach(f2 => {
        if (f2.type === 'seg') defaults[f2.key] = f2.options[0][0];
        else if (f2.type === 'number') defaults[f2.key] = f2.key === 'port' ? 465 : 0;
        else defaults[f2.key] = '';
      });
      if (t === 'pushplus') defaults.msg_template = '';
      else if (t !== 'webhook') defaults.template = '';
      f.channels.push({ id: newId(), type: t, name: TYPE_LABELS[t], enabled: 0, config: defaults });
      close(); renderAll(); sync();
    }));
  });

  // 全局设置绑定
  const bind = (el, k, num) => el.addEventListener('input', () => { f[k] = num ? (parseInt(el.value, 10) || 0) : el.value; });
  bind(x.titlePrefix, 'notify_title_prefix');
  bind(x.dndStart, 'notify_dnd_start'); bind(x.dndEnd, 'notify_dnd_end'); bind(x.hwTime, 'hw_time');
  const setSw = (el, k) => el.addEventListener('click', () => {
    const on = el.getAttribute('aria-checked') !== 'true';
    el.setAttribute('aria-checked', String(on)); f[k] = on ? 1 : 0; sync();
  });
  setSw(x.swMin, 'notify_minimal'); setSw(x.swDnd, 'notify_dnd_enabled'); setSw(x.swHw, 'hw_enabled');

  // ---- 定时推送报告 ----
  setSw(x.swRep, 'report_enabled');
  // 渲染模块勾选框（按分类分组）
  function renderRepMods() {
    const cats = {};
    REPORT_MOD_ORDER.forEach(k => { const [label, cat] = REPORT_MODS[k]; (cats[cat] = cats[cat] || []).push([k, label]); });
    x.repMods.innerHTML = Object.entries(cats).map(([cat, items]) =>
      `<div style="margin-bottom:8px"><div class="set-sub" style="margin:0 0 4px">${esc(cat)}</div><div class="chips">` +
      items.map(([k, label]) => {
        const on = f.report_modules.includes(k);
        return `<button type="button" class="chip-t" data-repmod="${k}" aria-pressed="${on}">${esc(label)}</button>`;
      }).join('') + '</div></div>'
    ).join('');
    x.repMods.querySelectorAll('[data-repmod]').forEach(btn => {
      btn.addEventListener('click', () => {
        const k = btn.dataset.repmod;
        const on = btn.getAttribute('aria-pressed') === 'true';
        if (on) { f.report_modules = f.report_modules.filter(m => m !== k); btn.setAttribute('aria-pressed', 'false'); }
        else { if (!f.report_modules.includes(k)) f.report_modules.push(k); btn.setAttribute('aria-pressed', 'true'); }
      });
    });
  }
  // 周期类型切换：显示/隐藏对应输入
  function syncRepSchVis() {
    const t = x.repType.value;
    x.repTime.style.display = (t === 'hourly' || t === 'interval') ? 'none' : '';
    x.repWeekday.style.display = t === 'weekly' ? '' : 'none';
    x.repDay.style.display = t === 'monthly' ? '' : 'none';
    x.repInterval.style.display = t === 'interval' ? '' : 'none';
  }
  x.repType.addEventListener('change', () => { f.report_schedule.type = x.repType.value; syncRepSchVis(); });
  x.repTime.addEventListener('input', () => { f.report_schedule.time = x.repTime.value; });
  x.repWeekday.addEventListener('change', () => { f.report_schedule.weekday = parseInt(x.repWeekday.value, 10); });
  x.repDay.addEventListener('input', () => { f.report_schedule.day = parseInt(x.repDay.value, 10) || 1; });
  x.repInterval.addEventListener('input', () => { f.report_schedule.interval_minutes = parseInt(x.repInterval.value, 10) || 60; });
  // 详细程度分段控件
  const repDetailSeg = makeSeg([['brief', '简略'], ['detail', '详细']], f.report_detail || 'detail', v => { f.report_detail = v; }, 'sm');
  x.repDetailSeg.appendChild(repDetailSeg);
  // 测试报告
  x.repTestBtn.addEventListener('click', async () => {
    x.repTestBtn.disabled = true; setText(x.hRepTest, '正在发送测试报告…');
    try {
      const j = await API.post('/api/notify/report_test', {});
      if (j && j.ok) { setText(x.hRepTest, '测试报告已发送，请到推送渠道查看'); toast('测试报告已发送', { icon: 'check', ms: 4000 }); }
      else { setText(x.hRepTest, '发送失败：' + ((j && j.error) || '未知错误')); toast('测试报告发送失败', { icon: 'alert', ms: 5000 }); }
    } catch (e) { setText(x.hRepTest, '发送失败：' + String(e.message).slice(0, 60)); toast('测试报告发送失败', { icon: 'alert', ms: 5000 }); }
    x.repTestBtn.disabled = false;
  });

  const stamp = ts => { try { return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }); } catch (e) { return ''; } };

  function sync() {
    const n = NB || {};
    x.swMin.setAttribute('aria-checked', String(!!+f.notify_minimal));
    x.swDnd.setAttribute('aria-checked', String(!!+f.notify_dnd_enabled));
    x.swHw.setAttribute('aria-checked', String(!!+f.hw_enabled));
    if (document.activeElement !== x.titlePrefix) x.titlePrefix.value = f.notify_title_prefix || '飞牛监控';
    if (document.activeElement !== x.dndStart) x.dndStart.value = f.notify_dnd_start || '23:00';
    if (document.activeElement !== x.dndEnd) x.dndEnd.value = f.notify_dnd_end || '08:00';
    if (document.activeElement !== x.hwTime) x.hwTime.value = f.hw_time || '08:00';
    const onCnt = f.channels.filter(c => c.enabled).length;
    setText(x.hMin, +f.notify_minimal ? '已开启：只推送一行关键信息' : '关闭：推送完整告警正文');
    setText(x.hDnd, +f.notify_dnd_enabled ? `已开启：${f.notify_dnd_start || '23:00'}–${f.notify_dnd_end || '08:00'} 时段缓存，结束后汇总发送` : '关闭：有告警立即推送');
    setText(x.hHw, +f.hw_enabled
      ? `已开启：每天 ${f.hw_time || '08:00'} 汇总发一封，只发到点仍未恢复的`
      : '关闭：SEL 硬件日志与风扇 / 温度 / 电源不推送（硬盘巡检仍按上面的开关走）');
    // 定时报告
    x.swRep.setAttribute('aria-checked', String(!!+f.report_enabled));
    const sch = f.report_schedule || {};
    if (document.activeElement !== x.repType) x.repType.value = sch.type || 'daily';
    if (document.activeElement !== x.repTime) x.repTime.value = sch.time || '08:00';
    if (document.activeElement !== x.repWeekday) x.repWeekday.value = String(sch.weekday || 0);
    if (document.activeElement !== x.repDay) x.repDay.value = String(sch.day || 1);
    if (document.activeElement !== x.repInterval) x.repInterval.value = String(sch.interval_minutes || 60);
    repDetailSeg.set(f.report_detail || 'detail');
    syncRepSchVis();
    renderRepMods();
    const schLabel = ({ hourly: '每小时整点', daily: `每天 ${sch.time || '08:00'}`, weekly: `每周${'一二三四五六日'[sch.weekday || 0]} ${sch.time || '08:00'}`, monthly: `每月 ${sch.day || 1} 日 ${sch.time || '08:00'}`, interval: `每 ${sch.interval_minutes || 60} 分钟` })[sch.type || 'daily'] || '每天';
    setText(x.hRep, +f.report_enabled
      ? `已开启：${schLabel} 推送，${f.report_modules.length} 个模块`
      : '关闭：不发送定时汇总报告');
    const last = n.last || {};
    const failCh = last.channels ? Object.entries(last.channels).filter(([, v]) => v && !v.ok).map(([, v]) => v.err).filter(Boolean).join('；') : '';
    setText(x.hMsg, n.load_error ? '读取配置失败：' + n.load_error : last.ts
      ? `上次推送 ${stamp(last.ts)} · ${last.ok ? '成功' : '失败：' + (failCh || '未知错误')} · ${onCnt} 个渠道已启用`
      : `共 ${f.channels.length} 个渠道，${onCnt} 个已启用。想确认配置可用，先「保存」再点渠道上的「测试」`);
  }

  async function doSave() {
    x.save.disabled = true;
    const body = {
      channels: f.channels.map(c => ({ id: c.id, type: c.type, name: c.name, enabled: c.enabled, config: Object.assign({}, c.config) })),
      hw_enabled: f.hw_enabled, hw_time: f.hw_time || '08:00',
      notify_title_prefix: f.notify_title_prefix || '飞牛监控',
      notify_dnd_enabled: f.notify_dnd_enabled,
      notify_dnd_start: f.notify_dnd_start || '23:00',
      notify_dnd_end: f.notify_dnd_end || '08:00',
      notify_minimal: f.notify_minimal,
      report_enabled: f.report_enabled,
      report_detail: f.report_detail || 'detail',
      report_schedule: Object.assign({}, f.report_schedule),
      report_modules: f.report_modules.slice(),
    };
    try {
      const j = await API.post('/api/notify/save', body);
      if (!j || !j.ok) throw new Error((j && j.error) || '未知错误');
      NB = Object.assign(await loadNotify(true), { last: (NB || {}).last || {} });
      // 保存后清空本地的保密字段值（后端不回传，留空表示不修改）
      f.channels.forEach(c => {
        const secrets = { mail: ['password'], sct: ['key'], wechat: ['url'], dingtalk: ['url', 'secret'],
          feishu: ['url'], bark: ['url'], pushplus: ['token'], webhook: ['url'], wecom_app: ['corp_secret'] }[c.type] || [];
        secrets.forEach(s => { c.config[s] = ''; });
      });
      renderAll();
      toast('通知配置已保存', { key: 'save' }); flashRow(x.save.closest('.set-row'));
    } catch (e) { toast('保存失败 · ' + String(e.message).slice(0, 80), { icon: 'alert', ms: 5000 }); }
    x.save.disabled = false; sync();
  }
  x.save.onclick = doSave;
  card._sync = sync;

  loadNotify().then(() => {
    const n = NB || {};
    // 深拷贝 channels，config 里的保密字段已被后端替换为 xxx_set
    f.channels = (n.channels || []).map(c => ({
      id: c.id, type: c.type, name: c.name, enabled: c.enabled,
      config: Object.assign({}, c.config || {}),
    }));
    f.hw_enabled = n.hw_enabled || 0;
    f.hw_time = n.hw_time || '08:00';
    f.notify_title_prefix = n.notify_title_prefix || '飞牛监控';
    f.notify_dnd_enabled = n.notify_dnd_enabled || 0;
    f.notify_dnd_start = n.notify_dnd_start || '23:00';
    f.notify_dnd_end = n.notify_dnd_end || '08:00';
    f.notify_minimal = n.notify_minimal || 0;
    f.report_enabled = n.report_enabled || 0;
    f.report_detail = (n.report_detail === 'brief' || n.report_detail === 'detail') ? n.report_detail : 'detail';
    f.report_schedule = Object.assign({ type: 'daily', time: '08:00', weekday: 0, day: 1, interval_minutes: 60 }, n.report_schedule || {});
    f.report_modules = Array.isArray(n.report_modules) && n.report_modules.length ? n.report_modules.slice() : ['system', 'cpu', 'memory', 'disk', 'network'];
    renderAll();
    sync();
  });
  sync();
}
let IB = null;
/**
 * 读 iDRAC 状态。
 * refresh=true 让后端强制重读（保存 / 测试连接后用，iDRAC8 每个请求 5 秒，较慢）；
 * 其余情况只读后端缓存——后台轮询线程在刷，页面刷新不该去等 iDRAC。
 */
async function loadIdrac(opts) {
  opts = opts || {};
  if (IB && !opts.refresh && !opts.reload) return IB;
  try { IB = await API.get('/api/idrac?sel=1' + (opts.refresh ? '&refresh=1' : '')); }
  catch (e) { IB = Object.assign({}, IB || {}, { ok: false, configured: true, error: e.message || '读取失败' }); }
  return IB;
}
/** iDRAC：Redfish 只读接带外管理口，风扇 / 温度 / 功耗 / 硬件日志。 */
function renderIdrac(card) {
  card.innerHTML = `<div class="note info notify-note">${ic('server')}<span><b>只读</b>：通过 Redfish 直连 iDRAC 读取风扇转速、温度、功耗与硬件日志，不会改动服务器任何设置。密码单独存 <span class="mono">idrac.json</span>（0600），接口只回「是否已填」。自签证书需要关闭校验，仅建议在内网使用。</span></div>
    <div class="set-row inline"><div><div class="lbl">iDRAC</div><div class="hint" data-ref="hOn"></div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="false" data-ref="swOn" aria-label="启用 iDRAC"></button></div></div>
    <div class="set-row"><div><div class="lbl">地址</div><div class="hint">iDRAC 管理口 IP 或主机名，只填地址，不要带 https://</div></div><div class="ctl"><input class="textfield mono" data-ref="host" placeholder="192.168.1.120" spellcheck="false" aria-label="iDRAC 地址"></div></div>
    <div class="set-row"><div><div class="lbl">用户名</div><div class="hint">建议在 iDRAC 里单建一个只读账号</div></div><div class="ctl"><input class="textfield" data-ref="user" placeholder="root" spellcheck="false" autocomplete="username" aria-label="iDRAC 用户名"></div></div>
    <div class="set-row"><div><div class="lbl">密码</div><div class="hint" data-ref="hPw"></div></div><div class="ctl"><input class="textfield" type="password" data-ref="pw" placeholder="iDRAC 登录密码" autocomplete="new-password" aria-label="iDRAC 密码"></div></div>
    <div class="set-row inline"><div><div class="lbl">跳过证书校验</div><div class="hint">iDRAC 出厂是自签证书，内网访问需要关闭校验</div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="true" data-ref="swTls" aria-label="跳过证书校验"></button></div></div>
    <div class="set-row inline"><div><div class="lbl">硬件日志纳入告警</div><div class="hint">SEL 出现新的 Critical / Warning 时，跟硬盘告警一起推送</div></div><div class="ctl"><button class="switch" type="button" role="switch" aria-checked="true" data-ref="swSel" aria-label="硬件日志纳入告警"></button></div></div>
    <div class="set-row"><div><div class="lbl">连接与保存</div><div class="hint" data-ref="hMsg"></div></div><div class="ctl" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button class="btn" type="button" data-ref="test">测试连接</button><button class="btn primary" type="button" data-ref="save">保存</button></div></div>
    <div class="set-row inline"><div><div class="lbl">固定信息</div><div class="hint" data-ref="hStatic">槽位 / 内存 / 阵列卡 / 物理盘 / 卷只在首次连接时读一次，之后不再自动刷新</div></div><div class="ctl"><button class="btn" type="button" data-ref="refresh">刷新固定信息</button></div></div>
    <div class="idrac-live" data-ref="live"></div>`;
  const x = collect(card);
  const f = { enabled: 0, host: '', user: '', password: '', verify_tls: 0, sel_alert: 1 };
  const bind = (el, k) => el.addEventListener('input', () => { f[k] = el.value; });
  bind(x.host, 'host'); bind(x.user, 'user'); bind(x.pw, 'password');
  const setSw = (el, k, invert) => el.addEventListener('click', () => {
    const on = el.getAttribute('aria-checked') !== 'true';
    el.setAttribute('aria-checked', String(on));
    f[k] = (invert ? !on : on) ? 1 : 0;
    sync();
  });
  setSw(x.swOn, 'enabled'); setSw(x.swTls, 'verify_tls', true); setSw(x.swSel, 'sel_alert');
  const hcls = h => /crit/i.test(h) ? 'h-crit' : /warn/i.test(h) ? 'h-warn' : 'h-ok';
  const when = s => { try { return new Date(s).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); } catch (e) { return String(s || ''); } };
  function renderLive() {
    const d = IB || {};
    // 固定信息是台账不是实时数据，把「什么时候读的」直接写在行上，
    // 免得以后有人看到旧槽位以为是 bug
    const sts = Number(d.static_ts || 0);
    x.refresh.disabled = !d.configured;
    setText(x.hStatic, sts
      ? `读取于 ${when(sts * 1000)}${d.static_from === 'disk' ? '' : '（刚读取）'} · 槽位 / 内存 / 阵列卡 / 物理盘 / 卷都来自这份台账，之后不再自动刷新`
      : '还没读到：iDRAC 连通后会自动读一次（1.5–3 分钟），之后不再自动刷新');
    if (!d.configured) { x.live.innerHTML = ''; return; }
    if (d.warming) {
      x.live.innerHTML = `<div class="idrac-head"><span class="h-ok">正在读取</span><span>iDRAC8 的 Redfish 每个请求约 5 秒，首次读取要 30–60 秒；后台会持续刷新，这里稍后自动出数据</span></div>`;
      return;
    }
    if (!d.ok) {
      x.live.innerHTML = `<div class="idrac-head"><span class="h-crit">读取失败</span><span>${esc(d.error || '未知错误')}</span></div>`;
      return;
    }
    const fs = d.fan_summary || {};
    const head = [
      `<b>${esc(d.model || 'Dell 服务器')}</b>`,
      d.service_tag ? `服务标签 ${esc(d.service_tag)}` : '',
      `整机 <span class="${hcls(d.health || '')}">${esc(d.health || '未知')}</span>`,
      d.power_state ? (d.power_state === 'On' ? '已开机' : esc(d.power_state)) : '',
      d.power_watts != null ? `功耗 <b>${esc(String(d.power_watts))}</b> W` : '',
      fs.count ? `风扇 ${fs.count} 颗 · ${fs.min}–${fs.max} RPM · 均 ${fs.avg}` : '',
      d.bios ? `BIOS ${esc(d.bios)}` : ''
    ].filter(Boolean).join(' · ');
    const fans = (d.fans || []).map(v => `<div class="idrac-fan${v.health && v.health.toUpperCase() !== 'OK' ? ' bad' : ''}"><b>${esc(v.name)}</b>${esc(String(v.rpm))}<small>RPM</small></div>`).join('');
    const temps = (d.temps || []).slice(0, 8).map(t => `<span>${esc(String(t.name))} <b>${esc(String(t.c))}</b>°C${t.crit != null ? `<small class="faint"> / ${esc(String(t.crit))}</small>` : ''}</span>`).join('');

    // ---- CPU ----
    const cpus = d.cpus || [];
    const cpuLine = cpus.length ? cpus.map(c => `${esc(c.model || 'CPU')} <span class="faint">${c.cores ? c.cores + ' 核' : ''}${c.threads ? ' / ' + c.threads + ' 线程' : ''}${c.mhz ? ' · ' + (c.mhz / 1000).toFixed(1) + 'GHz' : ''}</span>`).join('　') : '';

    // ---- 内存条 ----
    const dimms = d.dimms || [];
    const dimmHead = dimms.length
      ? `${dimms.length} 条 · ${d.dimm_gib || 0} GiB${d.dimm_bad ? ` · <span class="h-crit">${d.dimm_bad} 条异常</span>` : ' · 全部正常'}`
      : '';
    const dimmRows = dimms.map(m => `<div class="${m.health && m.health.toUpperCase() !== 'OK' ? 'bad' : ''}"><span class="mono">${esc(m.slot)}</span><span>${Math.round(m.mb / 1024)} GB</span><span class="faint">${esc(m.type)}${m.mhz ? ' · ' + m.mhz + 'MHz' : ''}</span><span class="faint">${esc(m.vendor || '')}</span></div>`).join('');

    // ---- 阵列卡与物理盘 ----
    const ctrls = d.controllers || [];
    const disks = d.disks || [];
    const vols = (d.volumes || []).filter(v => v.raid && v.raid.toUpperCase() !== 'NONE');
    const ctrlLine = ctrls.map(c => `${esc(c.model || c.id)}${c.firmware ? ` <span class="faint">固件 ${esc(c.firmware)}</span>` : ''}${c.health && c.health.toUpperCase() !== 'OK' ? ` <span class="h-crit">${esc(c.health)}</span>` : ''}`).join('　');
    const diskHead = disks.length
      ? `${disks.length} 块${d.disk_bad ? ` · <span class="h-crit">${d.disk_bad} 块异常</span>` : ' · 全部正常'}${vols.length ? ` · ${vols.length} 个卷` : ''}`
      : '';
    const diskRows = disks.map(k => {
      const bad = k.fail_pred || (k.health && k.health.toUpperCase() !== 'OK');
      const st = k.fail_pred ? '预测故障' : (k.health && k.health.toUpperCase() !== 'OK' ? k.health : '正常');
      return `<div class="dr${bad ? ' bad' : ''}"><span class="mono">${esc(String(k.bay))}</span><span class="mono" title="${esc(k.serial || '')}">${esc(k.model || '?')}</span><span>${k.cap_gb ? k.cap_gb + 'G' : '-'}</span><span class="faint">${esc(k.protocol || k.media || '')}${k.rpm ? ' ' + (k.rpm / 1000).toFixed(1) + 'k' : ''}</span><span class="${bad ? 'h-crit' : 'h-ok'}">${esc(st)}</span></div>`;
    }).join('');

    // ---- 固件 ----
    const fw = [
      d.idrac_fw ? `iDRAC ${esc(d.idrac_fw)}` : '',
      d.bios ? `BIOS ${esc(d.bios)}` : '',
      ...ctrls.filter(c => c.firmware).map(c => `${esc(c.model || '阵列卡')} ${esc(c.firmware)}`)
    ].filter(Boolean).join(' · ');

    const sel = ((d.sel && d.sel.entries) || []).slice(0, 5);
    const selHtml = sel.length ? `<div class="idrac-sec"><div class="idrac-sec-h">硬件日志 <span class="faint">最近 ${sel.length} 条</span></div><div class="idrac-sel">${sel.map(e => `<div class="e"><span class="t">${esc(when(e.created))}</span><span class="s ${esc(e.severity)}">${esc(e.severity || '-')}</span><span class="m">${esc(e.message)}</span></div>`).join('')}</div></div>` : '';

    x.live.innerHTML = `<div class="idrac-head">${head}</div>`
      + (fans ? `<div class="idrac-fans">${fans}</div>` : '')
      + (temps ? `<div class="idrac-temps">${temps}</div>` : '')
      + (cpuLine ? `<div class="idrac-sec"><div class="idrac-sec-h">处理器</div><div class="idrac-line">${cpuLine}</div></div>` : '')
      + (dimms.length ? `<div class="idrac-sec"><div class="idrac-sec-h">内存 <span class="faint">${dimmHead}</span></div><div class="idrac-dimms">${dimmRows}</div></div>` : '')
      + (ctrlLine ? `<div class="idrac-sec"><div class="idrac-sec-h">阵列卡</div><div class="idrac-line">${ctrlLine}</div></div>` : '')
      + (disks.length ? `<div class="idrac-sec"><div class="idrac-sec-h">物理盘 <span class="faint">${diskHead}</span></div><div class="idrac-disks"><div class="hr"><span class="ih">槽位</span><span class="ih">型号</span><span class="ih">容量</span><span class="ih">接口</span><span class="ih">状态</span></div>${diskRows}</div></div>` : '')
      + (fw ? `<div class="idrac-sec"><div class="idrac-sec-h">固件</div><div class="idrac-line faint">${fw}</div></div>` : '')
      + selHtml;
  }
  function sync() {
    const n = (IB && IB.config) || {};
    x.swOn.setAttribute('aria-checked', String(!!+f.enabled));
    x.swTls.setAttribute('aria-checked', String(!+f.verify_tls));
    x.swSel.setAttribute('aria-checked', String(!!+f.sel_alert));
    if (document.activeElement !== x.host) x.host.value = f.host || '';
    if (document.activeElement !== x.user) x.user.value = f.user || '';
    x.pw.placeholder = n.password_set ? '已保存 · 留空不修改' : 'iDRAC 登录密码';
    setText(x.hPw, n.password_set ? '已保存过密码，留空表示不修改' : 'iDRAC 的登录密码（就是打开 iDRAC 网页时用的那个）');
    setText(x.hOn, +f.enabled ? (+f.host && n.password_set && f.user ? '已启用：巡检时一并读取整机硬件状态' : '已启用，保存前请补全地址、用户名与密码') : '关闭：不读取 iDRAC');
    if (document.activeElement !== x.host && !x.hMsg.textContent) {
      setText(x.hMsg, (IB && IB.ok) ? '最近一次读取成功 · 打开设置页会自动刷新状态' : ((IB && IB.error) || '填好地址与账号后点「测试连接」'));
    }
    renderLive();
  }
  async function doSave() {
    x.save.disabled = true;
    const body = { enabled: f.enabled, host: f.host, user: f.user, verify_tls: f.verify_tls, sel_alert: f.sel_alert };
    if (f.password) body.password = f.password;
    try {
      const j = await API.post('/api/idrac/save', body);
      if (!j || !j.ok) throw new Error((j && j.error) || '未知错误');
      f.password = ''; x.pw.value = '';
      IB = await loadIdrac({ refresh: true });
      toast('iDRAC 配置已保存', { key: 'save' }); flashRow(x.save.closest('.set-row'));
      setText(x.hMsg, (IB && IB.ok) ? '已保存并读取成功' : ('已保存 · ' + ((IB && IB.error) || '尚未读取到数据')));
    } catch (e) { toast('保存失败 · ' + String(e.message).slice(0, 80), { icon: 'alert', ms: 5000 }); }
    x.save.disabled = false; sync();
  }
  x.save.onclick = doSave;
  x.test.onclick = async () => {
    x.test.disabled = true; setText(x.hMsg, '正在读取 iDRAC…（iDRAC8 每个请求约 5 秒，首次要 30–60 秒）');
    const body = { host: f.host, user: f.user };
    if (f.password) body.password = f.password;
    try {
      const j = await API.post('/api/idrac/test', body);
      const steps = (j && j.steps) || [];
      const bad = steps.find(s => !s.ok);
      if (j && j.ok) {
        const ov = j.overview || {};
        setText(x.hMsg, `连接成功 · ${ov.model || 'Dell'} · 服务标签 ${j.service_tag || ov.service_tag || '-'} · 风扇 ${(ov.fan_summary || {}).count || 0} 颗 · 物理盘 ${(j.disks || []).length} 块 · 内存 ${(j.dimms || []).length} 条 · 硬件日志 ${(j.sel || {}).count || 0} 条`);
        toast('iDRAC 连接成功', { icon: 'check', ms: 4000 });
      } else {
        setText(x.hMsg, '连接失败：' + String((bad && (bad.error || bad.detail)) || (j && j.error) || '未知错误'));
        toast('iDRAC 连接失败', { icon: 'alert', ms: 5000 });
      }
      IB = await loadIdrac({ refresh: true }); sync();
    } catch (e) { setText(x.hMsg, '连接失败：' + String(e.message).slice(0, 80)); toast('iDRAC 连接失败', { icon: 'alert', ms: 5000 }); }
    x.test.disabled = false;
  };
  card._sync = sync;
  // 固定信息只读一次：换盘 / 加内存 / 换阵列卡之后点这里重读。
  // 一轮 1.5~3 分钟，后端起线程跑，这里轮询 static_ts 变了就刷新界面。
  x.refresh.onclick = async () => {
    const label = x.refresh.textContent;
    const base = Number((IB || {}).static_ts || 0);
    x.refresh.disabled = true;
    x.refresh.textContent = '读取中…';
    setText(x.hStatic, '正在重读固定信息（CPU / 内存 / 阵列卡 / 物理盘 / 卷），iDRAC8 一轮要 1.5–3 分钟，读完自动更新');
    let done = false;
    try {
      const j = await API.post('/api/idrac/refresh', {});
      if (!j || !j.ok) {
        toast('刷新失败 · ' + String((j && j.error) || '未知错误').slice(0, 90), { icon: 'alert', ms: 7000 });
        return;
      }
      if (j.busy) toast('已经有一轮在读取了，等它跑完会自动更新');
      const t0 = Date.now();
      while (Date.now() - t0 < 300000) {
        await new Promise(r => setTimeout(r, 4000));
        IB = await loadIdrac({ reload: true });
        if (Number((IB || {}).static_ts || 0) > base) {
          done = true;
          renderLive(); sync();
          toast('固定信息已更新', { icon: 'check', ms: 4000 });
          return;
        }
      }
      toast('读取超过 5 分钟还没回来，稍后再点一次', { icon: 'alert', ms: 7000 });
    } catch (e) {
      toast('刷新失败 · ' + String(e.message || e).slice(0, 90), { icon: 'alert', ms: 6000 });
    } finally {
      x.refresh.textContent = label;
      x.refresh.disabled = false;
      if (!done) renderLive();
    }
  };
  // 后台轮询线程还在读取时自动再取几轮，用户不用手动刷新页面
  let warmTries = 0;
  function retryWarm() {
    if (!IB || !IB.warming || warmTries >= 8) return;
    warmTries += 1;
    setTimeout(async () => {
      await loadIdrac({ reload: true });
      sync();
      retryWarm();
    }, 15000);
  }
  loadIdrac().then(() => {
    const c = (IB && IB.config) || {};
    ['enabled', 'host', 'user', 'verify_tls', 'sel_alert'].forEach(k => { if (c[k] !== undefined) f[k] = c[k]; });
    f.password = '';
    sync();
    retryWarm();
  });
  sync();
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
    <p class="about-credit">作者 <a href="https://github.com/MisiteQ" target="_blank" rel="noopener noreferrer">Misite齊（@MisiteQ）</a> · 项目 <a href="https://github.com/MisiteQ/fnmonitor" target="_blank" rel="noopener noreferrer">github.com/MisiteQ/fnmonitor</a><br>界面与功能贡献 <a href="https://github.com/iqiuzixuan" target="_blank" rel="noopener noreferrer">iqiuzixuan</a>、<a href="https://github.com/ttkl9559-cell" target="_blank" rel="noopener noreferrer">ttkl9559-cell</a><br>通知推送架构参考 <a href="https://github.com/Sunanang/FNMessageBots" target="_blank" rel="noopener noreferrer">Sunanang/FNMessageBots</a>，致谢作者 Sunanang</p>
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


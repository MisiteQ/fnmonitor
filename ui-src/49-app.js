/* =====================================================================
   UI CONFIG SYNC — GET /api/ui 恢复，改动后防抖 POST；启动门禁防止默认值覆盖服务器配置
   ===================================================================== */
let uiBootReady = false, uiPending = false, uiTimer = 0;
const uiPayload = () => ({ theme: UI.theme, accent: UI.accent, dotMatrix: UI.dotMatrix, motion: UI.motion, fontScale: UI.fontScale, range: UI.range, layoutVersion: LAYOUT_VERSION, layout: UI.layout, hidden: UI.hidden, thresholds: TH, ignored: UI.ignored || [] });
function saveUI() {
  store.set('ui', uiPayload());
  clearTimeout(uiTimer);
  // 启动竞态保护：服务器配置恢复前只写本地缓存（手机 webview 的 localStorage 常不持久，默认布局不能回写覆盖 NAS 配置）
  if (!uiBootReady) { uiPending = true; return; }
  uiTimer = setTimeout(() => postUI(), 600);
}
async function postUI(retry) {
  try { const j = await API.post('/api/ui', uiPayload()); if (j && j.ok === false) throw new Error(j.error || '保存失败'); return true; }
  catch (e) {
    if (S.authGone) return false;
    toast(retry ? '界面设置同步失败，请检查连接后重试' : '界面设置同步失败，正在重试…', { icon: 'alert', key: 'uisync', ms: 4000 });
    if (!retry) setTimeout(() => postUI(true), 5000);
    return false;
  }
}
async function loadUI() {
  try {
    const j = await API.get('/api/ui'), raw = (j && j.ui) || {};
    if (Object.keys(raw).length) {
      const n = normalizeUI(raw);
      // 服务器是旧版配置（无新版布局）时沿用本地缓存里的新版布局，仅迁移外观
      if (!(+raw.layoutVersion >= LAYOUT_VERSION)) { n.layout = UI.layout; n.hidden = UI.hidden; if (!raw.thresholds) n.thresholds = Object.assign({}, TH); }
      setThresholds(n.thresholds);
      Object.assign(UI, n, { thresholds: TH });
      store.set('ui', uiPayload());
      applyUIAll(); Object.keys(LAYOUT_DEFAULT).forEach(applyLayout);
      if (MODS.trends && MODS.trends.seg) { MODS.trends.seg.set(UI.range); if (MODS.trends.range && MODS.trends.range !== UI.range) MODS.trends.load(); }
      syncSettings();
    }
  } catch (e) { /* 读取失败：保持本地缓存，不回写 */ }
  uiBootReady = true;
  if (uiPending) { uiPending = false; saveUI(); }
}
const mqDark = matchMedia('(prefers-color-scheme: dark)');
function applyUI(k) {
  const html = document.documentElement;
  if (k === 'theme') { const dark = UI.theme === 'dark' || (UI.theme === 'auto' && mqDark.matches); html.dataset.theme = dark ? 'dark' : 'light'; setHTML($('#btnTheme'), ic(dark ? 'sun' : 'moon')); $('#btnTheme').title = (dark ? '切换到浅色' : '切换到深色') + '  T'; }
  if (k === 'accent') html.dataset.accent = UI.accent;
  if (k === 'dotMatrix') DOTS.forEach(d => d.refresh());
  if (k === 'fontScale') { const s = clamp(+UI.fontScale || 1, 0.8, 1.5); html.style.zoom = s === 1 ? '' : String(s); }
  if (k === 'motion') html.classList.toggle('reduce-motion', !UI.motion);
  if (k === 'theme' || k === 'accent') requestAnimationFrame(() => placeThumb($('#pageSeg')));
}
function applyUIAll() { ['theme', 'accent', 'dotMatrix', 'fontScale', 'motion'].forEach(applyUI); }
mqDark.addEventListener('change', () => { if (UI.theme === 'auto') applyUI('theme'); });
/** 阈值改变：色阶、等级、阈值线、结论全部重算 */
function applyThresholds() {
  renderNow();
  const redraw = c => { c.render(); };
  if (MODS.trends && MODS.trends.charts) MODS.trends.charts.forEach(({ c }) => redraw(c));
  if (sheetCtx) sheetCtx.charts.forEach(redraw);
  $$('.colorspec').forEach(el => el.sync && el.sync());
  syncSettings();
}
async function resetUI() {
  if (!confirm('确定恢复默认界面？外观、主色、字号、布局与隐藏状态将重置为初始状态（提醒阈值保持不变）。')) return;
  try { ['fnm.ui', 'fnmonitor-theme', 'fnmonitor-layout-version', 'fnmonitor-panel-order', 'fnmonitor-hide-panels', 'fnmonitor-hide-mods', 'fnmonitor-tab', 'fnmonitor-font-scale', 'fnmonitor-side-collapsed'].forEach(k => localStorage.removeItem(k)); } catch (e) { /* ignore */ }
  const keep = Object.assign({}, TH);
  Object.assign(UI, uiDefaults()); setThresholds(keep); UI.thresholds = TH;
  uiBootReady = true; await postUI();
  location.reload();
}

/* =====================================================================
   RENDER LOOP — 只更新当前页面、未隐藏、在视口附近的版块
   ===================================================================== */
function renderNow() {
  if (document.hidden) return;
  if (S.page === 'overview') Hero.update(S.o);
  if (S.page !== 'settings' && !S.editing) orderOf(S.page).forEach(id => { const m = MODS[id]; if (!m || m.el.classList.contains('off') || m.el.hidden) return; if (m.vis) { m.dirty = false; runUpdate(m); } else m.dirty = true; });
  if (sheetCtx && sheetCtx.update && S.o) { try { sheetCtx.update(S.o); sheetCtx.chartTick && sheetCtx.chartTick(); } catch (e) { console.error('[fnmonitor] 抽屉更新失败', e); } }
}
function setStatus() {
  const d = $('#liveDot'), chip = $('#hostChip');
  const off = S.online === false;
  chip.classList.toggle('err', off);
  setText($('#hostName'), off ? '连接中断 · 重试中' : (S.o && S.o.system && S.o.system.hostname) || '连接中…');
  chip.title = off ? '无法连接到飞牛监控服务，正在自动重试' : `实时数据 · 每 ${(S.o && S.o.interval) || (S.cfg && S.cfg.interval) || '--'} 秒采集一次${S.lastOk ? ' · 更新于 ' + F.hms(S.lastOk / 1000) : ''}`;
  if (!off) { d.classList.remove('beat', 'paused'); void d.offsetWidth; d.classList.add('beat'); }
}
/** 记录实时缓冲：快照每 interval 秒才更新，按快照时间去重 */
function feed(o) {
  const ts = +o.ts || Date.now() / 1000;
  if (S._lastTs === ts) return false; S._lastTs = ts;
  const P = (k, v) => { if (ok(v)) push(S.live[k], [ts, +v]); };
  P('cpu', o.cpu && o.cpu.percent); P('mem', o.mem && o.mem.percent); P('temp', cpuTemp(o)); P('tempMb', mbTemp(o));
  P('power', pwrOk(o) ? o.power.total : null); P('disk', +o.disk_size > 0 ? o.disk_total : null); P('load', o.cpu && o.cpu.load && o.cpu.load[0]);
  const ni = nets(o); if (ni.length) { P('rx', sumRx(o)); P('tx', sumTx(o)); }
  const io = o.diskio || []; if (io.length) { P('dr', io.reduce((a, z) => a + (+z.read_rate || 0), 0) / 1048576); P('dw', io.reduce((a, z) => a + (+z.write_rate || 0), 0) / 1048576); }
  const rpm = ((o.sensors || {}).fans || []).map(f => +f.rpm).filter(v => v > 0); if (rpm.length) P('fan', rpm.reduce((a, b) => a + b, 0) / rpm.length);
  io.forEach(z => { const b = S.diskBuf[z.name] || (S.diskBuf[z.name] = { r: [], w: [] }); push(b.r, +z.read_rate || 0); push(b.w, +z.write_rate || 0); });
  S.cpuHigh = LV.cpu(o.cpu && o.cpu.percent) ? S.cpuHigh + 1 : 0;
  return true;
}
const sheetIs = (...t) => !!(sheetCtx && t.includes(sheetCtx.type));
const modVis = id => !!(MODS[id] && MODS[id].vis && !MODS[id].el.classList.contains('off'));
let dockerAt = 0;
Poll.add('overview', async () => {
  try {
    const o = await API.get('/api/overview');
    delete o.config;  // 不在前端保留服务端配置副本（旧版后端会在此带出口令）
    S.o = o;
    if (Date.now() - dockerAt > 20000 && o.docker) { S.docker = o.docker; }
    feedDocker(S.docker);
    const was = S.online; S.online = true; S.fails = 0; S.lastOk = Date.now();
    if (was === false) toast('已重新连接', { key: 'conn' });
    feed(o); noteSeen(o); setStatus(); renderNow();
    if (S.pendingSheet) { const k = S.pendingSheet; S.pendingSheet = null; openSheet(k, true); }
  } catch (e) {
    if (S.authGone || e.status === 403) return;
    S.fails++;
    if (S.online !== false && S.fails >= 2) { S.online = false; toast('与 NAS 的连接已中断，正在重试…', { icon: 'alert', key: 'conn', ms: 4000 }); }
    if (S.online === false || !S.o) { if (S.fails >= 2) S.online = false; setStatus(); renderNow(); }
  }
}, () => S.online === false ? Math.min(30000, 3000 * Math.pow(1.6, S.fails)) : S.page === 'overview' ? 5000 : 15000);
/* 巡检结果：盘位卡片的健康分级来源。60 秒拉一次（后端只读缓存，不会触发 smartctl）；
   总线别接口不受影响，失败时保留上一次结果，不把分级清空成「未巡检」。 */
Poll.add('diskhealth', async () => {
  try { S.diskHealth = await API.get('/api/disk_health'); renderNow(); } catch (e) { /* 保留旧值 */ }
}, 60000, () => S.page === 'overview' || sheetIs('disk'));
Poll.add('docker', async () => { S.docker = await API.get('/api/docker'); dockerAt = Date.now(); feedDocker(S.docker); renderNow(); }, 10000, () => S.page === 'docker' || sheetIs('ctr'));
Poll.add('vms', async () => {
  S.vms = await API.get('/api/vms'); feedVms(S.vms); if (S.vms.installed) setVmTab(true);
  if (sheetCtx && sheetCtx.retry) { const k = sheetCtx.key; sheetCtx.key = ''; openSheet(k, true); }
  renderNow();
}, 5000, () => S.page === 'vms' || sheetIs('vm'));
Poll.add('ports', async () => { const j = await API.get('/api/ports'); S.ports = j.ports || {}; renderNow(); }, 30000, () => S.page === 'docker');
Poll.add('apps', async () => { S.apps = await API.get('/api/apps'); renderNow(); }, 60000, () => S.page === 'docker');
Poll.add('procs', async () => { S.procs = await API.get('/api/processes'); renderNow(); }, 15000, () => (S.page === 'overview' && modVis('procs')) || sheetIs('cpu', 'mem'));
Poll.add('traffic', async () => { S.traffic = await API.get('/api/traffic'); renderNow(); }, 15000, () => S.page === 'overview' || sheetIs('net'));
Poll.add('pstats', async () => { S.pstats = await API.get('/api/power_stats'); renderNow(); }, 15000, () => S.page === 'overview');
Poll.add('monthly', async () => { S.monthly = await API.get('/api/monthly_summary'); renderNow(); }, 300000, () => S.page === 'overview' && !!(S.cfg && +S.cfg.monthly_summary_enabled));
Poll.add('weather', async () => { try { S.weather = await API.get('/api/weather'); } catch (e) { S.weather = { ok: false, error: e.message }; } Hero.weather(S.weather); }, 600000, () => S.page === 'overview');
Poll.add('hist', async () => { if (S.page === 'overview' && modVis('trends') && MODS.trends.res) await MODS.trends.load(); if (sheetCtx && sheetCtx.chartReload) await sheetCtx.chartReload(); }, 60000, () => (S.page === 'overview' && modVis('trends')) || !!(sheetCtx && sheetCtx.chartReload));
Poll.add('config', async () => { await loadConfig(); syncSettings(); }, 1800000);
function onPageChange(p) {
  if (p === 'docker') Poll.runAll(['docker', 'ports', 'apps']);
  if (p === 'vms') Poll.run('vms');
  if (p === 'overview') Poll.runAll(['overview', 'diskhealth']);
}
function globalRefresh() {
  const b = $('#btnRefresh'); b.classList.add('spinning'); setTimeout(() => b.classList.remove('spinning'), 800);
  Hist.clear(); if (MODS.trends) MODS.trends.res = null;
  if (MODS.energy) MODS.energy.barsAt = 0; if (MODS.network) MODS.network.barsAt = 0;
  Poll.runAll(Object.keys(Poll.jobs).filter(k => { const j = Poll.jobs[k]; return !j.when || j.when(); }));
  toast('正在刷新全部数据…', { key: 'refresh', icon: 'refresh', ms: 1400 });
}
$('#btnRefresh').onclick = globalRefresh;
$('#btnLogout').onclick = doLogout;
/** 历史缓冲预热：让指标卡迷你趋势打开即有曲线 */
async function seedSparks() {
  const take = (pts, k) => { if (!pts || !pts.length || S.live[k].length > 3) return; S.live[k] = pts.slice(-BUF_N).map(p => [p[0], +p[1]]).concat(S.live[k]).slice(-BUF_N); };
  const [c, m, t, p, n] = await Promise.all(['cpu', 'mem', 'temp', 'power', 'net'].map(k => Hist.get(k, '1h').catch(() => null)));
  take(c && c.points, 'cpu'); take(m && m.points, 'mem'); take(t && t.points, 'temp'); take(p && p.points, 'power'); take(n && n.rx, 'rx'); take(n && n.tx, 'tx');
  renderNow();
}

/* =====================================================================
   访问策略拦截 / 飞牛窗口引导 / 全屏 / 安全区
   ===================================================================== */
function blockedPage(msg) {
  if ($('#blocked')) return;
  const el = mount(`<div class="fullmask" id="blocked" role="alertdialog" aria-labelledby="blkT"><div class="card fm-card"><div class="fm-ico">${ic('shield', 'i-lg')}</div><h2 id="blkT">需要先开启登录口令</h2><p>${esc(msg || '通过域名访问前，请先在局域网内（使用 IP 地址访问）开启登录口令。')}</p><p class="faint">飞牛监控以 root 运行并可控制 Docker，为安全起见，未设置口令时只允许通过局域网 IP、localhost 或 .local 等内网地址访问。</p><div class="fm-btns"><a class="btn primary" href="${esc(loginURL())}">${ic('lock')}前往登录页</a><button class="btn" type="button" data-r="1">${ic('refresh')}重新检查</button></div></div></div>`);
  el.querySelector('[data-r]').onclick = () => location.reload();
  document.body.append(el);
}
(function iframeGuide() {
  if (!inIframe) return;
  API.get('/api/config').then(cfg => {
    const c = cfg && (cfg.config || cfg);
    if (!c || c.open_mode !== 'url') return;
    try { if (sessionStorage.getItem('__fnm_stay_iframe') === '1') return; } catch (e) { /* ignore */ }
    let popped = null;
    try { popped = window.open(location.href, '_blank', 'noopener'); } catch (e) { popped = null; }
    const el = mount(`<div class="fullmask" id="openmodeMask"><div class="card fm-card"><div class="fm-ico">${ic('ext', 'i-lg')}</div><h2>已选择「独立网页」模式</h2><p>${popped ? '页面已在浏览器新标签页打开，本窗口可以关闭。' : '本页面将在浏览器新标签页中打开，以获得完整浏览器能力（全屏 / 下载 / 插件）。'}</p><div class="fm-btns"><button class="btn primary" type="button" data-o="1">${ic('ext')}在浏览器新标签页打开</button><button class="btn" type="button" data-s="1">仍在此窗口查看</button></div><p class="faint" style="font-size:12px;margin-top:14px">切换打开方式：设置 → 服务 → 飞牛桌面打开方式</p></div></div>`);
    el.querySelector('[data-o]').onclick = () => { try { window.open(location.href, '_blank', 'noopener'); } catch (e) { /* ignore */ } };
    el.querySelector('[data-s]').onclick = () => { try { sessionStorage.setItem('__fnm_stay_iframe', '1'); } catch (e) { /* ignore */ } el.remove(); };
    document.body.append(el);
  }).catch(() => {});
})();
(function fullscreen() {
  const btn = $('#btnFull');
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  function toggle() {
    // 飞牛 iframe 模式：父页面未声明 allowfullscreen，Fullscreen API 会被静默拒绝
    if (inIframe) { toast('当前在飞牛窗口内，请点击 fnOS 窗口标题栏的最大化按钮', { icon: 'info', ms: 3600 }); return; }
    if (fsEl()) { const p = document.exitFullscreen ? document.exitFullscreen() : (document.webkitExitFullscreen && document.webkitExitFullscreen()); if (p && p.catch) p.catch(() => {}); }
    else {
      const el = document.documentElement;
      try { const p = el.requestFullscreen ? el.requestFullscreen() : (el.webkitRequestFullscreen ? el.webkitRequestFullscreen() : null); if (p && p.catch) p.catch(() => toast('浏览器拒绝了全屏请求', { icon: 'alert' })); }
      catch (e) { toast('浏览器不支持全屏', { icon: 'alert' }); }
    }
  }
  function sync() { const on = !!fsEl(); setHTML(btn, ic(inIframe ? 'winmax' : on ? 'shrink' : 'expand')); btn.title = inIframe ? '飞牛窗口内：请使用窗口标题栏的最大化' : on ? '退出全屏  F11' : '全屏  F11'; btn.setAttribute('aria-pressed', String(on)); }
  btn.onclick = toggle; sync(); S.fsToggle = toggle;
  document.addEventListener('fullscreenchange', sync); document.addEventListener('webkitfullscreenchange', sync);
  // 拦截 F11 改走 Fullscreen API：全屏期间浏览器栏不会因鼠标移到顶部而浮现
  addEventListener('keydown', e => { if (e.key === 'F11' || e.keyCode === 122) { e.preventDefault(); if (!e.repeat) toggle(); } }, true);
})();
(function bottomInset() {
  // 部分安卓三键导航在沉浸式渲染时会盖住底部导航：探测被遮挡高度写入 --extra-inset
  const root = document.documentElement, probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;left:0;bottom:0;width:0;height:0;visibility:hidden;pointer-events:none;padding-bottom:env(safe-area-inset-bottom,0px);';
  document.body.appendChild(probe);
  let last = -1;
  function detect() {
    let inset = parseFloat(getComputedStyle(probe).paddingBottom) || 0;
    if (inset <= 0) {
      const coarse = matchMedia('(pointer:coarse)').matches, mobileUA = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent || '');
      if (coarse || mobileUA) { const portrait = innerHeight >= innerWidth, scr = portrait ? Math.max(screen.width, screen.height) : Math.min(screen.width, screen.height); if (scr - innerHeight < 32) inset = 48; }
    }
    inset = Math.round(inset);
    if (inset !== last) { last = inset; root.style.setProperty('--extra-inset', inset > 0 ? inset + 'px' : '0px'); }
  }
  addEventListener('resize', detect); addEventListener('orientationchange', detect); detect();
})();

document.addEventListener('visibilitychange', () => {
  $('#liveDot').classList.toggle('paused', document.hidden);
  if (!document.hidden) { Poll.tick(); Poll.run('overview'); renderNow(); }
});
addEventListener('scroll', () => $('#topbar').classList.toggle('scrolled', scrollY > 4), { passive: true });
addEventListener('resize', () => placeThumb($('#pageSeg')));

/* =====================================================================
   BOOT
   ===================================================================== */
applyUIAll();
dmScan(document);
Hero.build();
Object.keys(LAYOUT_DEFAULT).forEach(applyLayout);
setVmTab(S.vmTab);
API.get('/api/vms?probe=1').then(j => setVmTab(j && j.installed)).catch(() => {});
if (MODS.monthly) MODS.monthly.el.hidden = true;
setStatus();
route();
setHash(false);
requestAnimationFrame(() => placeThumb($('#pageSeg')));
Poll.run('overview');
Promise.all([loadUI(), loadConfig().catch(() => null)]).then(() => { syncSettings(); Poll.run('monthly'); Poll.run('weather'); });
Poll.run('pstats'); Poll.run('traffic');
seedSparks().catch(() => {});
setInterval(() => Poll.tick(), 1000);
/* 启动 5 分钟后：始终没有数据源的版块自动隐藏一次（每台设备只做一次，可撤销 / 在设置中恢复） */
const seen = { thermal: false, energy: false, containers: false };
function noteSeen(o) {
  if (!o) return;
  if (cpuTemp(o) != null || mbTemp(o) != null || diskTemps(o).length || ((o.sensors || {}).fans || []).length) seen.thermal = true;
  if (pwrOk(o)) seen.energy = true;
  if (dockerOk(S.docker || o.docker)) seen.containers = true;
}
function autoHideEmpty() {
  if (document.hidden) { document.addEventListener('visibilitychange', function f() { if (!document.hidden) { document.removeEventListener('visibilitychange', f); setTimeout(autoHideEmpty, 3000); } }); return; }
  if (store.get('autohide', false) || !S.o || S.online === false) return;
  store.set('autohide', true);
  const hid = [['overview', 'thermal'], ['overview', 'energy'], ['docker', 'containers']].filter(([pg, id]) => !seen[id] && !(UI.hidden[pg] || []).includes(id));
  if (!hid.length) return;
  hid.forEach(([pg, id]) => { UI.hidden[pg] = (UI.hidden[pg] || []).concat(id); });
  applyLayout('overview'); applyLayout('docker'); saveUI(); syncSettings();
  toast(`已自动隐藏没有数据的版块：${hid.map(([, id]) => MODS[id].title).join('、')}`, { icon: 'info', ms: 8000, action: '撤销', onAction: () => { hid.forEach(([pg, id]) => { UI.hidden[pg] = UI.hidden[pg].filter(x => x !== id); }); applyLayout('overview'); applyLayout('docker'); saveUI(); syncSettings(); } });
}
setTimeout(autoHideEmpty, 5 * 60 * 1000);


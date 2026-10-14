/* =====================================================================
   STATE — 界面配置（/api/ui）+ 提醒阈值 + 运行时数据
   ===================================================================== */
const LAYOUT_VERSION = 200;
const TH_SPEC = {
  cpuWarn: [85, 30, 100], cpuCrit: [95, 30, 100], memWarn: [85, 30, 100], memCrit: [95, 30, 100],
  capWarn: [85, 30, 100], capCrit: [95, 30, 100], tempWarn: [80, 40, 110], tempCrit: [90, 40, 115],
  diskTempWarn: [50, 30, 80], diskTempCrit: [58, 30, 85],
};
const TH_PAIRS = [['cpuWarn', 'cpuCrit'], ['memWarn', 'memCrit'], ['capWarn', 'capCrit'], ['tempWarn', 'tempCrit'], ['diskTempWarn', 'diskTempCrit']];
/** 与后端 sanitize_thresholds 同规则：非数字 / 越界回落默认，严重 ≥ 警告 */
function sanitizeTH(raw) {
  raw = raw && typeof raw === 'object' ? raw : {};
  const out = {};
  for (const k in TH_SPEC) { const [dv, lo, hi] = TH_SPEC[k]; let v = parseFloat(raw[k]); if (!isFinite(v)) v = dv; out[k] = Math.round(clamp(v, lo, hi) * 10) / 10; }
  TH_PAIRS.forEach(([w, c]) => { if (out[c] < out[w]) out[c] = out[w]; });
  return out;
}
const THEMES = ['light', 'dark', 'auto'], ACCENT_IDS = ['black', 'blue', 'green', 'yellow', 'cyan'];
const RANGES = [['10m', '10分钟'], ['1h', '1小时'], ['6h', '6小时'], ['24h', '24小时'], ['7d', '7天'], ['all', '全部']];
const RANGE_SEC = { '10m': 600, '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, all: 0 };
const RANGE_TXT = { '10m': '过去 10 分钟', '1h': '过去 1 小时', '6h': '过去 6 小时', '24h': '过去 24 小时', '7d': '过去 7 天', all: '保留期内' };
const LAYOUT_DEFAULT = { overview: ['metrics', 'storage', 'trends', 'compute', 'thermal', 'energy', 'network', 'procs', 'monthly', 'host'], docker: ['containers', 'ports', 'apps'], vms: ['vms'] };
/** 旧版 7 套主题 → 新版 外观模式 + 主色 */
const LEGACY_THEME = { dark: ['dark', 'black'], light: ['light', 'black'], blue: ['light', 'blue'], fresh: ['light', 'green'], green: ['light', 'green'], yellow: ['light', 'yellow'], cyan: ['light', 'cyan'] };
function uiDefaults() {
  return { theme: 'light', accent: 'black', dotMatrix: true, motion: true, fontScale: 1, range: '24h', layoutVersion: LAYOUT_VERSION, layout: { overview: [], docker: [], vms: [] }, hidden: { overview: [], docker: [], vms: [] }, thresholds: sanitizeTH({}), ignored: [] };
}
/** 已忽略的健康提醒：[{key, fp, label, level, at}]；key 标识问题，fp 是状态指纹（变化即重新提醒） */
const cleanIgnored = a => Array.isArray(a) ? a.filter(x => x && typeof x.key === 'string' && x.key.length <= 120 && typeof x.fp === 'string' && x.fp.length <= 400)
  .map(x => ({ key: x.key, fp: x.fp, label: String(x.label || x.key).slice(0, 160), level: ['warn', 'crit', 'info'].includes(x.level) ? x.level : 'warn', at: +x.at || 0 }))
  .filter((x, i, arr) => arr.findIndex(y => y.key === x.key) === i).slice(-100) : [];
const cleanIds = (a, page) => Array.isArray(a) ? a.filter((id, i) => typeof id === 'string' && LAYOUT_DEFAULT[page].includes(id) && a.indexOf(id) === i) : [];
/** 把任意来源（服务器 / 本地缓存 / 旧版）的界面配置规整为新格式 */
function normalizeUI(raw, base) {
  const u = Object.assign(uiDefaults(), base || {}); raw = raw && typeof raw === 'object' ? raw : {};
  const isNew = +raw.layoutVersion >= LAYOUT_VERSION;
  if (isNew && THEMES.includes(raw.theme)) u.theme = raw.theme;
  else if (LEGACY_THEME[raw.theme]) { u.theme = LEGACY_THEME[raw.theme][0]; if (!ACCENT_IDS.includes(raw.accent)) u.accent = LEGACY_THEME[raw.theme][1]; }
  else if (THEMES.includes(raw.theme)) u.theme = raw.theme;
  if (ACCENT_IDS.includes(raw.accent)) u.accent = raw.accent;
  if (typeof raw.dotMatrix === 'boolean') u.dotMatrix = raw.dotMatrix;
  if (typeof raw.motion === 'boolean') u.motion = raw.motion;
  const fs = parseFloat(raw.fontScale); if (isFinite(fs) && fs >= 0.8 && fs <= 1.5) u.fontScale = fs;
  if (RANGE_SEC[raw.range] !== undefined) u.range = raw.range;
  if (raw.thresholds) u.thresholds = sanitizeTH(raw.thresholds);
  if (raw.ignored) u.ignored = cleanIgnored(raw.ignored);
  // 旧版布局（panelOrder / hiddenPanels / hiddenMods）结构不同，直接忽略
  if (isNew) {
    const pick = o => { const r = {}; Object.keys(LAYOUT_DEFAULT).forEach(p => { r[p] = cleanIds(o[p], p); }); return r; };
    if (raw.layout && typeof raw.layout === 'object') u.layout = pick(raw.layout);
    if (raw.hidden && typeof raw.hidden === 'object') u.hidden = pick(raw.hidden);
  }
  u.layoutVersion = LAYOUT_VERSION;
  return u;
}
/** 本地缓存仅用于首屏快速呈现；服务器 ui.json 为准。无新版缓存时尝试沿用旧版本地主题 / 字号 */
const UI = normalizeUI(store.get('ui', null) || (() => { try { return { theme: localStorage.getItem('fnmonitor-theme') || undefined, fontScale: localStorage.getItem('fnmonitor-font-scale') || undefined }; } catch (e) { return {}; } })());
const TH = UI.thresholds;
function setThresholds(t) { Object.assign(TH, sanitizeTH(t)); UI.thresholds = TH; }

const lv3 = (v, w, c) => !ok(v) ? '' : v >= c ? 'crit' : v >= w ? 'warn' : '';
const LV = {
  cpu: v => lv3(v, TH.cpuWarn, TH.cpuCrit),
  mem: v => lv3(v, TH.memWarn, TH.memCrit),
  cap: v => lv3(v, TH.capWarn, TH.capCrit),
  temp: v => lv3(v, TH.tempWarn, TH.tempCrit),
  dtemp: v => lv3(v, TH.diskTempWarn, TH.diskTempCrit),
};
const LV_TXT = { warn: '偏高', crit: '严重' };

const GiB = 1024 ** 3;
const S = {
  page: 'overview', sheet: null, editing: false, ticks: 0,
  o: null, cfg: null, docker: null, vms: null, vmTab: store.get('vmTab', false), procs: null, apps: null, ports: null, traffic: null, pstats: null, monthly: null, weather: null,
  online: null, fails: 0, lastOk: 0, cpuHigh: 0,
  live: { cpu: [], mem: [], temp: [], tempMb: [], power: [], rx: [], tx: [], dr: [], dw: [], fan: [], disk: [], load: [] },
  ctrBuf: {}, vmBuf: {}, diskBuf: {}, visible: new Set(),
};
const BUF_N = 60;
const push = (a, v, n = BUF_N) => { a.push(v); if (a.length > n) a.shift(); };

/* ---- 派生数据（只用后端真实字段） ---- */
const VIRT = /^(lo|docker|veth|br-|virbr|tun|tap|vnet|lxc|kube|wg|vxlan)/;
const nets = o => (o && o.net || []).filter(n => !VIRT.test(n.iface || ''));
const sumRx = o => nets(o).reduce((a, n) => a + (+n.rx_rate || 0), 0);
const sumTx = o => nets(o).reduce((a, n) => a + (+n.tx_rate || 0), 0);
const disksOf = o => (o && o.disks_detail) || [];
const ioOf = (o, name) => ((o && o.diskio) || []).find(z => z.name === name) || null;
const isSleep = d => !!(d && (d.standby || d.state === 'standby'));
const diskKind = d => /^nvme/.test(d.name) ? 'nvme' : /solid state/i.test(d.rpm || '') ? 'ssd' : /rpm/i.test(d.rpm || '') ? 'hdd' : 'disk';
const KIND_TXT = { nvme: 'NVMe', ssd: 'SSD', hdd: 'HDD', disk: '硬盘' };
const diskIcon = d => diskKind(d) === 'hdd' || diskKind(d) === 'disk' ? 'hdd' : 'ssd';
/** SMART：通过 / 失败 / 未知；计数字段仅在后端提供时使用 */
const SMART_KEYS = [['reallocated_sectors', '重映射扇区'], ['reallocated_events', '重映射事件'], ['pending_sectors', '待映射扇区'], ['uncorrectable', '无法纠正错误'], ['udma_crc', 'UDMA CRC 错误']];
function smartState(d) {
  const h = String(d.health || '').toUpperCase();
  if (isSleep(d)) return { lv: 'sleep', text: '休眠中' };
  if (/FAIL/.test(h)) return { lv: 'crit', text: 'SMART 未通过' };
  const bad = SMART_KEYS.filter(([k]) => +d[k] > 0);
  if (bad.length) return { lv: 'warn', text: bad.map(([k, l]) => `${l} ${d[k]}`).join('、') };
  if (/PASS|OK/.test(h)) return { lv: '', text: 'SMART 通过' };
  return { lv: 'none', text: d.health && d.health !== '未知' ? d.health : '未读取到 SMART' };
}
const cpuTemp = o => o && o.temp ? (ok(o.temp.cpu) ? +o.temp.cpu : null) : null;
const mbTemp = o => o && o.temp && ok(o.temp.system) ? +o.temp.system : null;
const diskTemps = o => disksOf(o).filter(d => ok(d.temp) && !isSleep(d));
const memParts = m => { const T = +m.total || 0, app = +m.used || 0, cache = +m.cached || 0, buf = +m.buffers || 0; return { app, cache, buf, free: Math.max(0, T - app - cache - buf) }; };
const gb = b => ok(b) ? (b / GiB).toFixed(1) : '--';
const gbT = b => ok(b) ? (b / GiB >= 10 ? Math.round(b / GiB) : (b / GiB).toFixed(1)) : '--';
const pwrOk = o => !!(o && o.power && o.power.ok && ok(o.power.total));
const dockerOk = d => !!(d && d.available);
const ctrRun = c => c.state === 'running';
const ctrCpu = c => { const v = parseFloat(c.cpu); return isFinite(v) ? v : null; };
/** "123.4MiB / 15.6GiB" → 字节 */
function parseSize(s) {
  const m = String(s || '').trim().match(/^([\d.]+)\s*([KMGTP]?i?B)/i); if (!m) return null;
  const u = m[2].toUpperCase(), n = parseFloat(m[1]), k = u.includes('I') ? 1024 : 1000;
  return n * Math.pow(k, 'BKMGTP'.indexOf(u[0] === 'B' ? 'B' : u[0]));
}
const ctrMem = c => parseSize(String(c.mem_usage || '').split('/')[0]);
/** docker Ports 字符串 → 去重的 "宿主端口→容器端口" 列表 */
function ctrPorts(p) {
  const arr = Array.isArray(p) ? p : String(p || '').split(',');
  const out = [];
  arr.forEach(x => { x = String(x).trim(); if (!x) return; const m = x.match(/:(\d+(?:-\d+)?)->(\d+(?:-\d+)?)\/(\w+)/); const t = m ? `${m[1]}→${m[2]}${m[3] === 'udp' ? '/udp' : ''}` : x.replace(/^0\.0\.0\.0:|^:::/, ''); if (!out.includes(t)) out.push(t); });
  return out;
}
const ZH_UNIT = { second: '秒', seconds: '秒', minute: '分钟', minutes: '分钟', hour: '小时', hours: '小时', day: '天', days: '天', week: '周', weeks: '周', month: '个月', months: '个月', year: '年', years: '年' };
/** docker 状态里残留的英文时长转中文（如「已停止 10hours」） */
const zhDur = s => String(s || '').replace(/(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b/gi, (_, n, u) => n + ' ' + ZH_UNIT[u.toLowerCase()]);
const zhStatus = s => zhDur(String(s || '').replace(/^Up\s+/i, '运行 ').replace(/^Exited \((\d+)\)\s*/i, '已停止（退出码 $1）').replace(/\s+ago$/i, '前').replace(/About an? (hour|minute)/i, '约 1 $1s').replace(/Less than a second/i, '不到 1 秒').replace(/\s*\((healthy|unhealthy|Paused)\)/i, (m, h) => ({ healthy: ' · 健康', unhealthy: ' · 不健康', paused: ' · 已暂停' })[h.toLowerCase()]));
const hostPort = p => { const m = String(p || '').match(/:(\d+)->\d+\/tcp/); return m ? m[1] : null; };

/* =====================================================================
   农历（沿用旧版已校验算法，原样保留）
   ===================================================================== */
function lunarInfo(y, m, d){
  const LUNAR_DAYS = [0x04bd8,0x04ae0,0x0a570,0x054d5,0x0d260,0x0d950,0x16554,0x056a0,0x09ad0,0x055d2,
    0x04ae0,0x0a5b6,0x0a4d0,0x0d250,0x1d255,0x0b540,0x0d6a0,0x0ada2,0x095b0,0x14977,
    0x04970,0x0a4b0,0x0b4b5,0x06a50,0x06d40,0x1ab54,0x02b60,0x09570,0x052f2,0x04970,
    0x06566,0x0d4a0,0x0ea50,0x06e95,0x05ad0,0x02b60,0x186e3,0x092e0,0x1c8d7,0x0c950,
    0x0d4a0,0x1d8a6,0x0b550,0x056a0,0x1a5b4,0x025d0,0x092d0,0x0d2b2,0x0a950,0x0b557,
    0x06ca0,0x0b550,0x15355,0x04da0,0x0a5b0,0x14573,0x052b0,0x0a9a8,0x0e950,0x06aa0,
    0x0aea6,0x0ab50,0x04b60,0x0aae4,0x0a570,0x05260,0x0f263,0x0d950,0x05b57,0x056a0,
    0x096d0,0x04dd5,0x04ad0,0x0a4d0,0x0d4d4,0x0d250,0x0d558,0x0b540,0x0b6a0,0x195a6,
    0x095b0,0x049b0,0x0a974,0x0a4b0,0x0b27a,0x06a50,0x06d40,0x0af46,0x0ab60,0x09570,
    0x04af5,0x04970,0x064b0,0x074a3,0x0ea50,0x06b58,0x055c0,0x0ab60,0x096d5,0x092e0,
    0x0c960,0x0d954,0x0d4a0,0x0da50,0x07552,0x056a0,0x0abb7,0x025d0,0x092d0,0x0cab5,
    0x0a950,0x0b4a0,0x0baa4,0x0ad50,0x055d9,0x04ba0,0x0a5b0,0x15176,0x052b0,0x0a930,
    0x07954,0x06aa0,0x0ad50,0x05b52,0x04b60,0x0a6e6,0x0a4e0,0x0d260,0x0ea65,0x0d530,
    0x05aa0,0x076a3,0x096d0,0x04afb,0x04ad0,0x0a4d0,0x1d0b6,0x0d250,0x0d520,0x0dd45,
    0x0b5a0,0x056d0,0x055b2,0x049b0,0x0a577,0x0a4b0,0x0aa50,0x1b255,0x06d20,0x0ada0];
  const LUNAR_MONTHS = ["正","二","三","四","五","六","七","八","九","十","冬","腊"];
  const LUNAR_DAYS2 = ["初一","初二","初三","初四","初五","初六","初七","初八","初九","初十",
    "十一","十二","十三","十四","十五","十六","十七","十八","十九","二十",
    "廿一","廿二","廿三","廿四","廿五","廿六","廿七","廿八","廿九","三十"];
  const GAN = ["甲","乙","丙","丁","戊","己","庚","辛","壬","癸"];
  const ZHI = ["子","丑","寅","卯","辰","巳","午","未","申","酉","戌","亥"];
  function lYearDays(y){ let s = 348; for (let i=0x8000; i>0x8; i>>=1) s += (LUNAR_DAYS[y-1900] & i) ? 1 : 0; return s + leapDays(y); }
  function leapMonth(y){ return LUNAR_DAYS[y-1900] & 0xf; }
  function leapDays(y){ if (leapMonth(y)) return (LUNAR_DAYS[y-1900] & 0x10000) ? 30 : 29; return 0; }
  function lMonthDays(y, m){ return (LUNAR_DAYS[y-1900] & (0x10000 >> m)) ? 30 : 29; }
  function lLeap(y){ return leapMonth(y); }
  const base = Date.UTC(1900, 0, 31);
  let offset = Math.floor((Date.UTC(y, m-1, d) - base) / 86400000);
  let yc = 1900, mc = 0, sub = 0;
  // 注意：循环变量在每次迭代末尾自增，回补时必须用「本次实际扣减的天数」，
  // 不能用自增后年份/月份的天数（相邻年/月天数不同会导致日期错位）
  for (yc = 1900; yc < 2101 && offset > 0; yc++){ sub = lYearDays(yc); offset -= sub; }
  if (offset < 0){ offset += sub; yc--; }
  const leap = leapMonth(yc);
  let isLeap = false;
  for (mc = 1; mc < 13 && offset > 0; mc++){
    if (leap > 0 && mc === (leap + 1) && !isLeap){ --mc; isLeap = true; sub = leapDays(yc); }
    else { sub = lMonthDays(yc, mc); if (isLeap) isLeap = false; }
    offset -= sub;
  }
  if (offset === 0 && leap > 0 && mc === (leap + 1)){ if (isLeap){ isLeap = false; } else { isLeap = true; --mc; } }
  if (offset < 0){ offset += sub; --mc; }
  const gzY = GAN[(yc - 4 + 1000) % 10] + ZHI[(yc - 4 + 1000) % 12];
  const md = LUNAR_MONTHS[mc - 1] + "月";
  const dd = LUNAR_DAYS2[offset];
  // 日干支：以 1949-10-01 甲子日（Unix 第 -7397 天）为锚点，常数 +17，正模避免负数
  const dayNum = Math.floor(Date.UTC(y,m-1,d) / 86400000);
  const gzD = GAN[((dayNum + 17) % 10 + 10) % 10] + ZHI[((dayNum + 17) % 12 + 12) % 12];
  return { y: yc, m: mc, d: offset + 1, monthName: md, dayName: dd, gzY: gzY, gzD: gzD, isLeap: isLeap };
}
function lunarText(now) {
  const li = lunarInfo(now.getFullYear(), now.getMonth() + 1, now.getDate());
  return (li.isLeap ? "闰" : "") + li.monthName + li.dayName + " · " + li.gzY + "年 " + li.gzD;
}

/* =====================================================================
   DATA LAYER — 真实接口；后台标签页暂停；断线重试；401 跳登录页
   ===================================================================== */
const loginURL = () => location.pathname.replace(/[^/]*$/, '') + 'login.html';
const API = {
  async req(url, opt) {
    const r = await fetch(url, Object.assign({ cache: 'no-store', credentials: 'same-origin' }, opt || {}));
    if (r.status === 401) { if (!S.authGone) { S.authGone = true; toast('登录已过期，正在跳转登录页', { icon: 'lock' }); setTimeout(() => { location.href = loginURL(); }, 900); } throw new Error('未登录'); }
    if (r.status === 403) {
      let j = null; try { j = await r.json(); } catch (e) { /* HTML 说明页 */ }
      // CSRF 拒绝时显示服务端说明（含被拒的闸门），便于在手机 app 里直接截图定位
      const err = String((j && (j.error === 'csrf_rejected' && j.message || j.error || j.message)) || '');
      if (err === 'old_password_invalid') { const e = new Error('当前口令不正确'); e.status = 403; throw e; }
      // 访问策略拦截（未开启口令时通过公网域名访问）：整页说明，而不是零散的报错
      if (!opt || !opt.method || /域名|口令|host/i.test(err) || !j) blockedPage(err);
      const e = new Error(err || '访问被拒绝'); e.status = 403; throw e;
    }
    if (!r.ok) { let j = null; try { j = await r.json(); } catch (e) { /* 非 JSON */ } const e = new Error((j && j.error) || 'HTTP ' + r.status); e.status = r.status; e.body = j; throw e; }
    return r.json();
  },
  get(url) { return this.req(url); },
  /** 所有写操作统一走这里：POST + JSON + CSRF 标记头 */
  post(url, body) { return this.req(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FNM-Request': '1' }, body: JSON.stringify(body || {}) }); },
};
/** 轮询调度：一个 1s 心跳驱动所有任务；document.hidden 时整体暂停 */
const Poll = {
  jobs: {},
  add(name, fn, every, when) { this.jobs[name] = { fn, every, when, last: 0, busy: false }; },
  async run(name) {
    const j = this.jobs[name]; if (!j || j.busy) return;
    j.busy = true; j.last = Date.now();
    try { await j.fn(); } catch (e) { /* 各任务自行处理错误状态 */ }
    j.busy = false;
  },
  tick() {
    if (document.hidden) return;
    const now = Date.now();
    for (const k in this.jobs) { const j = this.jobs[k]; const ev = typeof j.every === 'function' ? j.every() : j.every; if (!j.busy && (!j.when || j.when()) && now - j.last >= ev) this.run(k); }
  },
  runAll(names) { names.forEach(n => this.run(n)); },
};
/** 历史缓存：metric|range → {at, data}，60 秒内复用；同一请求在途时合并 */
const Hist = {
  c: {}, inflight: {},
  async get(metric, range, maxAge = 60000) {
    const k = metric + '|' + range, c = this.c[k];
    if (c && Date.now() - c.at < maxAge) return c.data;
    if (this.inflight[k]) return this.inflight[k];
    const p = API.get(`/api/history?metric=${encodeURIComponent(metric)}&range=${encodeURIComponent(range)}`).then(d => { this.c[k] = { at: Date.now(), data: d }; return d; }).finally(() => { delete this.inflight[k]; });
    this.inflight[k] = p;
    return p;
  },
  peek(metric, range) { const c = this.c[metric + '|' + range]; return c ? c.data : null; },
  clear() { this.c = {}; },
};
/** 实时缓冲与历史拼接：只追加比历史最后一点更新的实时点 */
function mergeLive(hist, live) {
  const out = (hist || []).slice(); const lastT = out.length ? out[out.length - 1][0] : 0;
  (live || []).forEach(p => { if (p[0] > lastT + 1) out.push(p); });
  return out;
}
/** 速率（或功率）历史按自然日积分：Σ v × dt，dt 上限为 3 倍中位采样间隔（跳过停机断档） */
function dailyIntegral(pts, scale) {
  const days = {}; if (!pts || pts.length < 2) return days;
  const cap = medianDt(pts) * 3;
  for (let i = 1; i < pts.length; i++) {
    const dt = pts[i][0] - pts[i - 1][0]; if (dt <= 0 || dt > cap) continue;
    const d = new Date(pts[i][0] * 1000), key = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
    days[key] = (days[key] || 0) + (+pts[i - 1][1] || 0) * dt * scale;
  }
  return days;
}
const dayKey = d => d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
/** 最近 n 天（含今天）的日序列 */
function lastDays(n) { const out = [], now = new Date(); for (let i = n - 1; i >= 0; i--) { const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i); out.push(d); } return out; }


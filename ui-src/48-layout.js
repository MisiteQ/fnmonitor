/* =====================================================================
   LAYOUT EDIT MODE — 拖拽排序 + 显示开关，保存到 ui.json（layout / hidden）
   ===================================================================== */
const orderOf = page => { const saved = UI.layout[page] || []; return saved.filter(id => LAYOUT_DEFAULT[page].includes(id)).concat(LAYOUT_DEFAULT[page].filter(id => !saved.includes(id))); };
function applyLayout(page) {
  const host = $('#layout-' + page), hid = UI.hidden[page] || [];
  orderOf(page).forEach(id => { const m = MODS[id]; if (!m) return; host.append(m.el); const off = hid.includes(id); m.el.classList.toggle('off', off); m.r.vis.setAttribute('aria-checked', String(!off)); setText(m.r.visL, off ? '已隐藏' : '显示'); });
  renumber(page);
}
function renumber(page) {
  let n = 0;
  $$('#layout-' + page + ' > .module').forEach(el => { const m = MODS[el.dataset.mod]; if (!m.headless && !el.classList.contains('off') && !el.hidden) n++; setText(m.r.idx, el.classList.contains('off') ? '—' : String(n).padStart(2, '0')); });
}
function saveLayout(page) { UI.layout[page] = $$('#layout-' + page + ' > .module').map(e => e.dataset.mod); saveUI(); renumber(page); }
function resetLayout(page) { UI.layout[page] = []; UI.hidden[page] = []; saveUI(); applyLayout(page); }
function setEditing(on) {
  if (on && S.page === 'settings') { toast('设置页没有可编辑的布局', { icon: 'info' }); return; }
  if (on) closeSheet();
  S.editing = on; document.body.classList.toggle('editing', on);
  $('#editbar').classList.toggle('on', on); $('#btnEdit').setAttribute('aria-pressed', String(on));
  if (!on) { renderNow(); syncSettings(); }
  scrollTo({ top: on ? 0 : scrollY, behavior: 'auto' });
}
$('#btnEdit').onclick = () => setEditing(!S.editing);
$('#editDone').onclick = () => { setEditing(false); toast('布局已保存'); };
$('#editReset').onclick = () => { resetLayout(S.page); toast('已恢复默认布局'); };
function flip(els, mutate) {
  const first = new Map(els.map(e => [e, e.offsetTop]));
  mutate();
  els.forEach(e => {
    const dy = first.get(e) - e.offsetTop; if (!dy || reduced()) return;
    e.style.transition = 'none'; e.style.transform = `translateY(${dy}px)`;
    requestAnimationFrame(() => requestAnimationFrame(() => { e.style.transition = 'transform 280ms var(--ease)'; e.style.transform = ''; }));
  });
}
document.addEventListener('pointerdown', e => {
  const grip = e.target.closest('.grip'); if (!grip || !S.editing) return;
  const el = grip.closest('.module'), host = el.parentElement, page = host.dataset.layout;
  e.preventDefault();
  const hostTop = () => host.getBoundingClientRect().top;
  const grabY = e.clientY - hostTop() - el.offsetTop;
  el.classList.add('dragging');
  const move = ev => {
    const y = ev.clientY - hostTop();
    const sibs = $$(':scope > .module', host).filter(c => c !== el && !c.hidden);
    let before = null;
    for (const c of sibs) { if (y < c.offsetTop + c.offsetHeight / 2) { before = c; break; } }
    if (el.nextElementSibling !== before) flip(sibs, () => host.insertBefore(el, before));
    el.style.transition = 'none';
    el.style.transform = `translateY(${y - grabY - el.offsetTop}px)`;
    if (ev.clientY < 90) scrollBy(0, -12); else if (ev.clientY > innerHeight - 90) scrollBy(0, 12);
  };
  const up = () => {
    removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
    el.classList.remove('dragging'); el.style.transition = 'transform 240ms var(--ease)'; el.style.transform = '';
    setTimeout(() => { el.style.transition = ''; }, 260);
    saveLayout(page);
  };
  // 监听挂在 window：拖动中节点被移动会释放 pointer capture，挂在手柄上会丢事件
  addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up);
});
document.addEventListener('keydown', e => {
  const grip = e.target.closest && e.target.closest('.grip'); if (!grip || !S.editing || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
  e.preventDefault(); e.stopPropagation();
  const el = grip.closest('.module'), host = el.parentElement, sibs = $$(':scope > .module', host).filter(c => c !== el);
  flip(sibs, () => { if (e.key === 'ArrowUp' && el.previousElementSibling) host.insertBefore(el, el.previousElementSibling); if (e.key === 'ArrowDown' && el.nextElementSibling) host.insertBefore(el.nextElementSibling, el); });
  saveLayout(host.dataset.layout); grip.focus();
}, true);
document.addEventListener('click', e => {
  const sw = e.target.closest('.mod-editbar .switch'); if (!sw) return;
  const el = sw.closest('.module'), page = el.parentElement.dataset.layout, id = el.dataset.mod, hid = UI.hidden[page] || [];
  const off = !hid.includes(id); UI.hidden[page] = off ? hid.concat(id) : hid.filter(x => x !== id);
  el.classList.toggle('off', off); sw.setAttribute('aria-checked', String(!off)); setText(MODS[id].r.visL, off ? '已隐藏' : '显示');
  saveUI(); renumber(page);
});

/* =====================================================================
   COMMAND PALETTE  ( / )
   ===================================================================== */
const Pal = {
  items: [], sel: 0, shown: [],
  build() {
    const it = [], add = (g, label, icon, run, hint = '', kw = '') => it.push({ g, label: String(label), icon, run, hint: String(hint), kw: String(kw) });
    const PI = { overview: ['总览', 'home'], docker: ['容器 · 端口', 'box'], vms: ['虚拟机', 'vm'], settings: ['设置', 'gear'] };
    navPages().forEach((p, k) => add('页面', PI[p][0], PI[p][1], () => go(p), String(k + 1), p));
    [['health', '健康检查', 'shield'], ['cpu', '处理器', 'cpu'], ['mem', '内存', 'mem'], ['storage', '存储', 'storage'], ['net', '网络', 'net'], ['temp', '温度与散热', 'temp'], ['power', '能耗', 'bolt']].forEach(([k, l, i]) => add('详情', l, i, () => { if (S.page !== 'overview') go('overview'); openSheet(k); }, '', k));
    disksOf(S.o).forEach(d => add('硬盘', `${d.name} · ${vendorModel(d)}`, diskIcon(d), () => { if (S.page !== 'overview') go('overview'); openSheet('disk/' + encodeURIComponent(d.name)); }, isSleep(d) ? '休眠中' : smartState(d).lv === 'warn' ? '需留意' : '', (d.serial || '') + ' ' + KIND_TXT[diskKind(d)]));
    Docker.list().forEach(c => add('容器', c.name, 'box', () => { if (S.page !== 'docker') go('docker'); openSheet('ctr/' + encodeURIComponent(c.name)); }, ctrRun(c) ? '运行中' : '未运行', c.image + ' ' + c.ports));
    VMs.list().forEach(v => add('虚拟机', vmName(v), 'vm', () => { if (S.page !== 'vms') go('vms'); openSheet('vm/' + v.uuid); }, v.state_zh || '', v.name + ' ' + (v.ips || []).join(' ')));
    SET_SCHEMA.forEach(g => (g.items || []).forEach(x => add('设置', x.label, g.icon, () => focusSetting(x.k), g.title, (x.help || '') + ' ' + (typeof x.hint === 'string' ? x.hint : ''))));
    SET_SCHEMA.filter(g => g.custom).forEach(g => add('设置', g.title, g.icon, () => focusGroup(g.id), '分组', { auth: '密码 登录 口令 安全', update: '升级 版本 更新', thresholds: '阈值 告警 提醒 温度 cpu 内存 容量', data: '导出 csv 报告 目录', about: '关于 作者 github' }[g.id] || ''));
    add('操作', '切换浅色 / 深色', 'moon', toggleTheme, 'T'); add('操作', '编辑布局', 'layout', () => setEditing(true), 'E'); add('操作', '刷新全部数据', 'refresh', globalRefresh); add('操作', '全屏 / 退出全屏', 'expand', () => S.fsToggle && S.fsToggle(), 'F11'); add('操作', '生成健康报告', 'shield', () => doDownload('/api/report?format=html'), 'HTML'); add('操作', '键盘快捷键', 'key', () => openSheet('keys'), '?');
    this.items = it;
  },
  open() { this.build(); $('#palette').classList.add('on'); const i = $('#palInput'); i.value = ''; this.filter(''); setTimeout(() => i.focus(), 10); },
  close() { $('#palette').classList.remove('on'); },
  filter(q) {
    q = q.trim().toLowerCase();
    const sc = x => { const l = x.label.toLowerCase(); if (!q) return 1; if (l.startsWith(q)) return 3; if (l.includes(q)) return 2; if ((x.kw + ' ' + x.hint + ' ' + x.g).toLowerCase().includes(q)) return 1; return 0; };
    this.shown = q ? this.items.map(x => [x, sc(x)]).filter(p => p[1] > 0).sort((a, b) => b[1] - a[1]).map(p => p[0]).slice(0, 30) : this.items.filter(x => x.g === '页面' || x.g === '详情' || x.g === '操作');
    this.sel = 0; this.draw();
  },
  draw() {
    const L = $('#palList');
    if (!this.shown.length) { L.innerHTML = `<div class="empty" style="padding:32px"><b>没有找到相关内容</b><span>试试硬盘名、容器名或「电价」</span></div>`; return; }
    let g = '', html = '';
    this.shown.forEach((x, i) => { if (x.g !== g) { g = x.g; html += `<div class="palette-g">${esc(g)}</div>`; } html += `<button class="palette-item ${i === this.sel ? 'on' : ''}" type="button" data-i="${i}">${ic(x.icon)}<span>${esc(x.label)}</span><small>${esc(x.hint)}</small></button>`; });
    L.innerHTML = html;
    const on = L.querySelector('.on'); on && on.scrollIntoView({ block: 'nearest' });
  },
  run(i) { const x = this.shown[i]; if (!x) return; this.close(); x.run(); },
};
$('#palInput').addEventListener('input', e => Pal.filter(e.target.value));
$('#palInput').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); Pal.sel = clamp(Pal.sel + (e.key === 'ArrowDown' ? 1 : -1), 0, Pal.shown.length - 1); Pal.draw(); }
  if (e.key === 'Enter') { e.preventDefault(); Pal.run(Pal.sel); }
});
$('#palList').addEventListener('click', e => { const b = e.target.closest('[data-i]'); if (b) Pal.run(+b.dataset.i); });
$('#palette').addEventListener('click', e => { if (e.target.id === 'palette') Pal.close(); });
$('#btnSearch').onclick = () => Pal.open();

/* =====================================================================
   ROUTING  #/page[/sheet]
   ===================================================================== */
const PAGES = ['overview', 'docker', 'vms', 'settings'];
const PAGE_TITLE = { overview: '总览', docker: '容器', vms: '虚拟机', settings: '设置' };
/** 导航里实际显示的页面（本机没有 virsh 时不显示「虚拟机」），数字快捷键按此顺序 */
const navPages = () => PAGES.filter(p => p !== 'vms' || S.vmTab);
/** 显示 / 隐藏「虚拟机」导航；结果记在本地，下次打开不闪烁 */
function setVmTab(on) {
  on = !!on; if (S.vmTab === on && $('#pageSeg [data-page="vms"]').hidden === !on) return;
  S.vmTab = on; store.set('vmTab', on);
  $$('#pageSeg [data-page="vms"], #bottomNav [data-page="vms"]').forEach(b => { b.hidden = !on; });
  placeThumb($('#pageSeg'));
}
function parseHash() {
  const h = location.hash.replace(/^#\/?/, ''), [p, ...rest] = h.split('/');
  return { page: PAGES.includes(p) ? p : 'overview', sheet: rest.join('/') || null };
}
function setHash(push) {
  const h = '#/' + S.page + (S.sheet ? '/' + S.sheet : '');
  if (location.hash === h) return;
  push ? history.pushState(null, '', h) : history.replaceState(null, '', h);
}
function showPage(p) {
  const changed = S.page !== p; S.page = p;
  PAGES.forEach(x => { const el = $('#page-' + x); el.hidden = x !== p; if (x === p && changed) { el.classList.remove('page-enter'); void el.offsetWidth; el.classList.add('page-enter'); } });
  $$('#pageSeg button, #bottomNav button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.page === p)));
  placeThumb($('#pageSeg'));
  $('#btnEdit').style.visibility = p === 'settings' ? 'hidden' : '';
  if (p === 'settings') { if (S.editing) setEditing(false); buildSettings(); syncSettings(); }
  if (changed) { scrollTo(0, 0); renderNow(); onPageChange(p); }
  document.title = PAGE_TITLE[p] + ' · 飞牛监控';
}
function go(p) { if (S.sheet) closeSheet(true); showPage(p); setHash(false); }
function route() {
  const { page, sheet } = parseHash();
  showPage(page);
  if (sheet) openSheet(sheet, true); else closeSheet(true);
}
addEventListener('popstate', route);
addEventListener('hashchange', route);
$$('#pageSeg button, #bottomNav button').forEach(b => b.addEventListener('click', () => go(b.dataset.page)));
document.addEventListener('click', e => {
  if (S.editing) return;
  const t = e.target.closest('[data-sheet]'); if (!t || e.target.closest('[data-act]') || e.target.closest('a[href]')) return;
  openSheet(t.dataset.sheet);
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target; if (t.matches && t.matches('[data-sheet][role="button"]')) { e.preventDefault(); openSheet(t.dataset.sheet); }
});

/* =====================================================================
   KEYBOARD
   ===================================================================== */
function toggleTheme() { const dark = document.documentElement.dataset.theme === 'dark'; UI.theme = dark ? 'light' : 'dark'; applyUI('theme'); saveUI(); syncSettings(); }
$('#btnTheme').onclick = toggleTheme;
$('#btnKeys').onclick = () => openSheet('keys');
document.addEventListener('keydown', e => {
  const typing = e.target.closest && e.target.closest('input, textarea, select, [contenteditable]');
  if (e.key === 'Escape') {
    if (pop) return closePop();
    if ($('#palette').classList.contains('on')) return Pal.close();
    if (S.sheet) return closeSheet();
    if (S.editing) return setEditing(false);
    if (typing) e.target.blur();
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if ($('#palette').classList.contains('on')) return;
  const k = e.key;
  if (/^[1-9]$/.test(k) && navPages()[+k - 1]) go(navPages()[+k - 1]);
  else if (k === '/') { e.preventDefault(); Pal.open(); }
  else if (k === 'e' || k === 'E') setEditing(!S.editing);
  else if (k === 't' || k === 'T') toggleTheme();
  else if (k === '?') openSheet('keys');
  else if ((k === 'r' || k === 'R') && S.page === 'overview' && MODS.trends) { const i = RANGES.findIndex(o => o[0] === UI.range), n = RANGES[(i + 1) % RANGES.length][0]; MODS.trends.seg.set(n); UI.range = n; saveUI(); MODS.trends.load(); toast('趋势范围 · ' + RANGES.find(o => o[0] === n)[1], { key: 'range', icon: 'trend' }); }
});


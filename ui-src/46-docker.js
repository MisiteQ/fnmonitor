/* =====================================================================
   DOCKER PAGE — /api/docker（容器）· /api/ports · /api/apps
   ===================================================================== */
const Docker = {
  filter: 'all', q: '', pending: {},
  PEND: { stop: '正在停止…', start: '正在启动…', restart: '正在重启…' },
  DONE: { stop: '已停止', start: '已启动', restart: '已重启' },
  VERB: { stop: '停止', start: '启动', restart: '重启' },
  list() { const d = S.docker || (S.o && S.o.docker); return dockerOk(d) ? d.containers || [] : []; },
  find(a) { const cs = this.list(); return cs.find(c => c.name === a) || cs.find(c => c.id === a) || null; },
  setFilter(f) { this.filter = f; const m = MODS.containers; if (m && m.seg) m.seg.set(f); this.apply(); },
  async refreshUntil(id, a) {
    for (const ms of [700, 1500, 3000, 5000]) {
      await new Promise(r => setTimeout(r, ms));
      try { S.docker = await API.get('/api/docker'); } catch (e) { continue; }
      const c = (S.docker.containers || []).find(z => z.id === id);
      if (!c || (a === 'stop' ? !ctrRun(c) : ctrRun(c))) return;
    }
  },
  /** 停止 / 重启需二次确认（按钮变为「确认？」3 秒），启动直接执行；完成后可撤销 */
  async act(id, name, a, btn) {
    if (!id || this.pending[id] || !this.VERB[a]) return;
    if ((a === 'stop' || a === 'restart') && btn && btn.dataset.armed !== '1') {
      btn.dataset.armed = '1'; btn.classList.add('armed'); btn._old = btn.innerHTML; btn.innerHTML = `${ic(a === 'stop' ? 'stop' : 'restart')}确认${this.VERB[a]}？`;
      btn.setAttribute('aria-label', `确认${this.VERB[a]} ${name}`);
      clearTimeout(btn._t); btn._t = setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ''; btn.classList.remove('armed'); btn.innerHTML = btn._old; } }, 3000);
      return;
    }
    this.pending[id] = a; renderNow();
    try {
      const j = await API.post('/api/docker/action', { id, action: a });
      if (!j.ok) throw new Error(j.error || '未知错误');
      await this.refreshUntil(id, a);
      const undo = a === 'stop' ? 'start' : a === 'start' ? 'stop' : null;
      delete this.pending[id]; renderNow();
      toast(`${name} ${this.DONE[a]}`, undo ? { action: '撤销', onAction: () => this.act(id, name, undo) } : {});
    } catch (e) {
      delete this.pending[id]; renderNow();
      toast(`${name} ${this.VERB[a]}失败：${String(e.message).slice(0, 80)}`, { icon: 'alert', ms: 6000 });
    }
  },
  apply() {
    const m = MODS.containers; if (!m || !m.rows) return;
    const q = this.q.trim().toLowerCase(); let shown = 0;
    this.list().forEach(c => {
      const row = m.rows[c.id]; if (!row) return;
      const okQ = !q || (c.name + ' ' + c.image + ' ' + c.ports).toLowerCase().includes(q);
      const okF = this.filter === 'all' || (this.filter === 'running' ? ctrRun(c) : !ctrRun(c));
      row.hidden = !(okQ && okF); if (!row.hidden) shown++;
    });
    const any = this.list().length;
    m.x.empty.hidden = !any || shown > 0; m.x.head.hidden = !any || shown === 0;
    setHTML(m.x.emptyT, q ? `没有匹配「${esc(this.q)}」的容器` : this.filter === 'exited' ? '没有未运行的容器' : '没有运行中的容器');
  },
};
const actBtns = c => {
  const ids = `data-id="${esc(c.id)}" data-name="${esc(c.name)}"`;
  return ctrRun(c)
    ? `<button class="act" type="button" data-act="restart" ${ids} title="重启" aria-label="重启 ${esc(c.name)}">${ic('restart')}</button><button class="act danger" type="button" data-act="stop" ${ids} title="停止" aria-label="停止 ${esc(c.name)}">${ic('stop')}</button>`
    : `<button class="act go" type="button" data-act="start" ${ids} title="启动" aria-label="启动 ${esc(c.name)}">${ic('play')}</button>`;
};
/** 容器实时网速（后端相邻两次 docker 采样差分，约 30 秒均值）：↓ 下行蓝 · ↑ 上行梅紫红 */
const ctrRateOk = c => ctrRun(c) && ok(c.rx_rate) && ok(c.tx_rate);
const ctrNetTxt = c => !ctrRun(c) ? '—' : c.net_host ? '宿主网络' : ctrRateOk(c) ? `↓ ${F.rateStr(c.rx_rate)} · ↑ ${F.rateStr(c.tx_rate)}` : '采样中';
function ctrNetHTML(c) {
  if (!ctrRun(c)) return '<span class="faint">—</span>';
  if (c.net_host) return '<span class="faint" title="host 网络模式与宿主机共用网卡，Docker 不单独统计流量">宿主网络</span>';
  if (!ctrRateOk(c)) return '<span class="faint" title="需要两次采样（约 30 秒）才能算出速率">采样中…</span>';
  return `<b><i class="ar dn">↓</i>${esc(F.rateStr(c.rx_rate))}</b><small><i class="ar up">↑</i>${esc(F.rateStr(c.tx_rate))}</small>`;
}
/** 每次拿到新的容器数据时记录 CPU，用于行内迷你趋势 */
function feedDocker(d) {
  if (!dockerOk(d)) return;
  const now = Date.now(); if (S._ctrFed && now - S._ctrFed < 4000) return; S._ctrFed = now;
  (d.containers || []).forEach(c => { const b = S.ctrBuf[c.id] || (S.ctrBuf[c.id] = []); push(b, ctrRun(c) ? (ctrCpu(c) || 0) : 0, 36); });
}

defineModule({
  id: 'containers', page: 'docker', kicker: 'CONTAINERS', title: '容器', big: true,
  actions: m => {
    const f = mount(`<label class="field">${ic('search')}<input type="search" placeholder="搜索名称、镜像或端口" aria-label="搜索容器"></label>`);
    f.querySelector('input').addEventListener('input', e => { Docker.q = e.target.value; Docker.apply(); });
    m.seg = makeSeg([['all', '全部'], ['running', '运行中'], ['exited', '未运行']], Docker.filter, v => { Docker.filter = v; Docker.apply(); }, 'sm');
    return [f, m.seg];
  },
  build(body, m) {
    body.innerHTML = `<div class="stat-cells dk-sum">
        <div class="stat-cell"><div class="eyebrow">运行中</div><div class="dnum"><span class="dm" data-ref="sRun"></span></div><div class="sub" data-ref="sRunT"></div></div>
        <div class="stat-cell"><div class="eyebrow">容器 CPU</div><div class="dnum"><span class="dm" data-ref="sCpu"></span><span class="unit">%</span></div><div class="sub" data-ref="sCpuT"></div></div>
        <div class="stat-cell"><div class="eyebrow">容器内存</div><div class="dnum"><span class="dm" data-ref="sMem"></span><span class="unit" data-ref="sMemU"></span></div><div class="sub" data-ref="sMemT"></div></div>
        <div class="stat-cell"><div class="eyebrow">镜像</div><div class="dnum"><span class="dm" data-ref="sImg"></span></div><div class="sub">本地镜像</div></div>
      </div>
      <div class="card ctr-list"><div class="ctr ctr-head" data-ref="head" hidden><span>容器</span><span>端口</span><span>CPU · 近几分钟</span><span>内存</span><span class="ctr-net">网络 · 30 秒均值</span><span class="ctr-up">运行时长</span><span></span></div><div data-ref="list"></div>
      <div class="empty" data-ref="empty" hidden><svg class="art" viewBox="0 0 120 84" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M60 10 88 25v30L60 70 32 55V25L60 10Z" stroke-dasharray="3 4"/><path d="M32 25l28 15 28-15M60 40v30"/><circle cx="92" cy="62" r="9"/><path d="m98.5 68.5 7 7"/></svg><b data-ref="emptyT">没有匹配的容器</b><span>换个关键词，或查看全部容器。</span><button class="btn" type="button" data-ref="clear">清除筛选</button></div>
      <div data-ref="na"></div></div>`;
    m.x = collect(body); m.rows = {};
    m.x.clear.addEventListener('click', () => { Docker.q = ''; $('input', m.r.actions).value = ''; Docker.setFilter('all'); });
    m.x.list.addEventListener('click', e => {
      const a = e.target.closest('[data-act]');
      if (a) { e.stopPropagation(); Docker.act(a.dataset.id, a.dataset.name, a.dataset.act, a); return; }
      const row = e.target.closest('.ctr[data-name]'); if (row) openSheet('ctr/' + encodeURIComponent(row.dataset.name));
    });
    m.x.list.addEventListener('keydown', e => { const row = e.target.closest('.ctr[data-name]'); if (e.key === 'Enter' && row && e.target === row) openSheet('ctr/' + encodeURIComponent(row.dataset.name)); });
  },
  update(o, m) {
    const d = S.docker || (o && o.docker); if (!d) return;
    const x = m.x, avail = dockerOk(d), cs = avail ? d.containers || [] : [], run = cs.filter(ctrRun);
    setHTML(x.na, avail ? (cs.length ? '' : `<div class="empty">${ic('box')}<b>还没有容器</b><span>在 Docker 中创建的容器会出现在这里。</span></div>`) : `<div class="empty">${ic('box')}<b>Docker 不可用</b><span>${esc(d.error || '未检测到 docker 命令')}</span></div>`);
    const sig = cs.map(c => c.id + c.name).join();
    if (m.sig !== sig) {
      m.sig = sig; x.list.innerHTML = ''; m.rows = {};
      cs.forEach(c => {
        const row = mount(`<div class="ctr" data-name="${esc(c.name)}" tabindex="0" role="button" aria-label="容器 ${esc(c.name)}">
          <div class="ctr-name"><span class="sdot" data-ref="dot"></span><div class="t"><b>${esc(c.name)}</b><small title="${esc(c.image)}">${esc(c.image)}</small></div></div>
          <div class="ports">${ctrPorts(c.ports).length ? ctrPorts(c.ports).slice(0, 4).map(p => `<span class="port">${esc(p)}</span>`).join('') + (ctrPorts(c.ports).length > 4 ? `<span class="port">+${ctrPorts(c.ports).length - 4}</span>` : '') : '<span class="faint" style="font-size:12px">无端口映射</span>'}</div>
          <div class="ctr-cpu"><div class="spk" data-ref="sp"></div><b data-ref="cpu"></b></div>
          <div class="ctr-mem"><b data-ref="mem"></b><small data-ref="memp"></small></div>
          <div class="ctr-net" data-ref="net"></div>
          <div class="ctr-up" data-ref="up"></div>
          <div class="acts" data-ref="acts"></div>
        </div>`);
        row._sp = makeSpark(); row._r.sp.append(row._sp);
        m.rows[c.id] = row; x.list.append(row);
      });
    }
    cs.forEach(c => {
      const row = m.rows[c.id]; if (!row) return; const r = row._r, pend = Docker.pending[c.id], on = ctrRun(c), cpu = ctrCpu(c), mem = ctrMem(c), bb = S.ctrBuf[c.id] || [];
      setCls(row, 'ctr', (on ? '' : 'off') + (pend ? ' pending' : ''));
      setCls(r.dot, 'sdot', pend ? 'busy' : on ? '' : 'off');
      setText(r.cpu, on && cpu != null ? F.d1(cpu) + '%' : '—');
      setText(r.mem, on && mem != null ? F.bytesStr(mem) : '—'); setText(r.memp, on && c.mem_percent ? c.mem_percent + '%' : '');
      row._sp.set([{ values: on && bb.length > 1 ? bb : [0, 0] }], { min: 0, max: Math.max(5, ...bb), live: on ? 'ink' : 'none' });
      setHTML(r.net, ctrNetHTML(c));
      setHTML(r.up, pend ? `<span class="pending-t">${Docker.PEND[pend]}</span>` : on ? esc(zhDur(c.uptime || c.status).replace(/^运行\s*/, '')) : `<span class="faint">${esc(zhDur(c.uptime) || c.state || '已停止')}</span>`);
      setHTML(r.acts, pend ? '<span class="spin" aria-label="处理中"></span>' : actBtns(c));
    });
    const cpuSum = run.reduce((a, c) => a + (ctrCpu(c) || 0), 0), memSum = run.reduce((a, c) => a + (ctrMem(c) || 0), 0), T = o && o.mem && o.mem.total;
    dot(x.sRun).set(avail ? `${run.length}/${cs.length}` : '--'); setText(x.sRunT, !avail ? 'Docker 不可用' : cs.length - run.length ? `${cs.length - run.length} 个未运行` : cs.length ? '全部运行中' : '暂无容器');
    dnum(x.sCpu, avail ? cpuSum : null, F.d1); setText(x.sCpuT, avail && o && o.cpu && o.cpu.cores ? `约 ${(cpuSum / 100).toFixed(1)} 核 · 共 ${o.cpu.cores} 核` : '--');
    dnumU(x.sMem, x.sMemU, avail ? memSum : null, F.bytes); setText(x.sMemT, avail && T ? `占物理内存 ${F.int(memSum / T * 100)}%` : '--');
    dnum(x.sImg, avail && d.images >= 0 ? d.images : null);
    setHTML(m.r.dek, avail ? `<b>${run.length}</b> 个运行中 · <b>${cs.length - run.length}</b> 个未运行${d.images >= 0 ? ` · ${esc(d.images)} 个镜像` : ''}　合计 CPU <b>${F.d1(cpuSum)}%</b> · 内存 <b>${F.bytesStr(memSum)}</b>` : esc(d.error || 'Docker 不可用'));
    Docker.apply();
  },
});

const portsOf = v => String(v == null ? '' : v).split(/[^\d]+/).filter(Boolean);
defineModule({
  id: 'ports', page: 'docker', kicker: 'PORTS', title: '端口占用', dek: '飞牛应用、Docker 映射与系统监听端口',
  actions: m => {
    const f = mount(`<label class="field">${ic('plug')}<input inputmode="numeric" placeholder="查询端口，如 8080" aria-label="查询端口"></label>`);
    f.querySelector('input').addEventListener('input', e => { m.q = e.target.value.trim(); m.apply(); });
    return f;
  },
  build(body, m) {
    m.q = '';
    body.innerHTML = `<div class="grid g-3">
      <div class="card pad port-col"><h4>飞牛应用<span data-ref="nA"></span></h4><div class="rows" data-ref="apps"></div></div>
      <div class="card pad port-col"><h4>Docker 映射<span data-ref="nD"></span></h4><div class="rows" data-ref="dock"></div></div>
      <div class="card pad port-col"><h4>系统监听端口<span data-ref="nL"></span></h4><div class="lp-grid" data-ref="listen"></div></div>
    </div><div class="port-verdict" data-ref="verdict"></div>`;
    m.x = collect(body);
    m.apply = () => {
      const q = m.q, isNum = /^\d+$/.test(q), p = S.ports || {};
      const hit = el => { const v = el.dataset.p || ''; return isNum ? portsOf(v).includes(q) : (el.textContent + ' ' + (el.dataset.n || '')).toLowerCase().includes(q.toLowerCase()); };
      $$('[data-p]', m.el).forEach(el => { const h = q && hit(el); el.classList.toggle('hit', !!h && el.classList.contains('lp')); el.classList.toggle('dim', !!q && !h); $$('.port', el).forEach(pp => pp.classList.toggle('hit', !!h && isNum && portsOf(pp.textContent).includes(q))); });
      let v = '';
      if (isNum) {
        const n = +q, l = (p.listeners || []).find(x => +x.port === n), d = (p.docker || []).find(x => (x.ports || []).some(s => portsOf(s).includes(q))), a = (p.apps || []).find(x => portsOf(x.port).includes(q));
        if (n < 1 || n > 65535) v = `<span class="st-warn">${ic('alert', 'i-sm')} 端口范围为 1 – 65535</span>`;
        else if (l || d || a) v = `<span class="st-warn">${ic('alert', 'i-sm')} ${n} 已被占用</span><span class="muted"> · ${[a && `飞牛应用「${esc(a.name)}」`, d && `容器 ${esc(d.name)}`, l && `进程 ${esc(l.process || '未知')}（${esc(l.proto || 'tcp')}）`].filter(Boolean).join('、')}</span>`;
        else v = `<span class="st-ok">${ic('check-c', 'i-sm')} ${n} 当前未被监听，可以使用</span>`;
      }
      setHTML(m.x.verdict, v);
    };
  },
  update(o, m) {
    const p = S.ports || (o && o.ports); if (!p) return;
    const sig = JSON.stringify([p.apps, p.docker, p.listeners]); if (m.sig === sig) return; m.sig = sig;
    const apps = p.apps || [], dock = p.docker || [], lis = p.listeners || [];
    setText(m.x.nA, apps.length + ' 个'); setText(m.x.nD, dock.length + ' 个'); setText(m.x.nL, lis.length + ' 个');
    setHTML(m.x.apps, apps.length ? apps.map(a => `<div class="row-i" data-p="${esc(a.port)}" style="grid-template-columns:minmax(0,1fr) auto auto"><span style="min-width:0"><span class="ell">${esc(a.name)}</span><small class="mono faint ell">${esc(a.appid)}${a.default_port && a.port_source === 'runtime' ? ` · 默认 ${esc(a.default_port)}` : ''}</small></span><span class="port">${esc(a.port)}</span>${a.running ? '<span class="pill ok"><span class="dot"></span>运行</span>' : '<span class="pill">未运行</span>'}</div>`).join('') : emptyHTML('未扫描到应用中心端口'));
    setHTML(m.x.dock, dock.length ? dock.map(d => `<div class="row-i" data-p="${esc((d.ports || []).join(','))}" data-n="${esc(d.name)}" style="grid-template-columns:minmax(0,1fr) auto"><span style="display:flex;align-items:center;gap:8px;min-width:0"><span class="sdot ${d.state === 'running' ? '' : 'off'}"></span><span class="ell">${esc(d.name)}</span></span><span class="ports" style="justify-content:flex-end">${ctrPorts(d.ports).map(x => `<span class="port">${esc(x)}</span>`).join('')}</span></div>`).join('') : emptyHTML('暂无带端口映射的容器'));
    setHTML(m.x.listen, lis.length ? lis.map(l => `<span class="lp" data-p="${esc(l.port)}" title="${esc((l.addr || '') + (l.pid ? ' · PID ' + l.pid : ''))}"><b>${esc(l.port)}</b><small>${esc(l.proto || 'tcp')} · ${esc(l.process || '未知进程')}</small></span>`).join('') : emptyHTML('未读取到监听端口'));
    m.apply();
    setHTML(m.r.dek, `共 ${lis.length} 个监听端口${dock.length ? `，${dock.length} 个容器做了端口映射` : ''}。输入端口号即可判断是否空闲。`);
  },
});

const APP_ICO = { photos: 'photo', media: 'film', music: 'music' };
defineModule({
  id: 'apps', page: 'docker', kicker: 'APPS', title: '内置应用', dek: '相册、影视与音乐的数据统计',
  actions: m => { const b = mount(`<button class="link-btn" type="button" title="重新扫描应用数据">${ic('refresh', 'i-sm')}立即更新</button>`); b.onclick = async () => { b.disabled = true; b.classList.add('spinning'); try { S.apps = await API.get('/api/apps?refresh=1'); runUpdate(m); toast('应用统计已更新'); } catch (e) { toast('更新失败：' + e.message, { icon: 'alert' }); } b.disabled = false; b.classList.remove('spinning'); }; return b; },
  build(body, m) { m.x = { body }; body.innerHTML = `<div class="card pad">${emptyHTML('正在读取内置应用…')}</div>`; },
  update(o, m) {
    const list = (S.apps && S.apps.apps) || (o && o.apps); if (!list) return;
    const sig = JSON.stringify(list); if (m.sig === sig) return; m.sig = sig;
    m.x.body.innerHTML = list.length ? `<div class="grid g-3">${list.map(a => `<div class="card app-card ${a.installed ? '' : 'off'}"><div class="app-top"><span class="app-ico">${ic(APP_ICO[a.id] || 'box')}</span><div style="flex:1;min-width:0"><b style="font-size:16px;font-weight:600">${esc(a.name)}</b><div class="sub mono ell">${esc(a.appid)}</div></div>${a.installed ? '<span class="pill ok"><span class="dot"></span>已安装</span>' : '<span class="pill">未安装</span>'}</div>
        <div class="app-stats"><div><div class="eyebrow">数据占用</div><div class="num">${a.installed ? esc(F.bytes(a.data_size)[0]) + `<span class="unit" style="font-size:12px">${esc(F.bytes(a.data_size)[1])}</span>` : '--'}</div></div><div><div class="eyebrow">媒体条目</div><div class="num">${a.media_count != null ? esc(a.media_count >= 10000 ? (a.media_count / 10000).toFixed(1) : Number(a.media_count).toLocaleString()) + (a.media_count >= 10000 ? '<span class="unit" style="font-size:12px">万</span>' : '') : '--'}</div></div><div><div class="eyebrow">媒体占用</div><div class="num">${a.media_count != null ? esc(F.bytes(a.media_size)[0]) + `<span class="unit" style="font-size:12px">${esc(F.bytes(a.media_size)[1])}</span>` : '--'}</div></div></div>
        ${a.note ? `<p class="sub" style="margin:16px 0 0">统计来源：<span class="mono">${esc(a.note)}</span></p>` : !a.installed ? '<p class="sub" style="margin:16px 0 0">未在本机检测到该应用。</p>' : a.media_count == null ? '<p class="sub" style="margin:16px 0 0">未找到可读取的媒体库数据库。</p>' : ''}</div>`).join('')}</div>` : `<div class="card pad">${emptyHTML('暂无内置应用数据')}</div>`;
    const ins = list.filter(a => a.installed);
    setHTML(m.r.dek, ins.length ? `${ins.map(a => esc(a.name)).join('、')} 已安装${ins.some(a => a.media_count != null) ? `，媒体共 ${F.bytesStr(ins.reduce((s, a) => s + (+a.media_size || 0), 0))}` : ''}。统计每 10 分钟后台更新一次。` : '本机未安装相册 / 影视 / 音乐。');
  },
});


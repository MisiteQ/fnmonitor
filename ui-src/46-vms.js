/* =====================================================================
   VMS PAGE — /api/vms（libvirt 虚拟机）· 操作 POST /api/vm/action
   ===================================================================== */
const vmOk = d => !!(d && d.available);
const vmRun = v => v.state === 'running' || v.state === 'blocked';
const vmName = v => v.title || v.name;
/** 内存读数：客户机自报的已用（需 balloon 驱动）优先，否则用宿主上 QEMU 进程的常驻内存 */
const vmMemUsed = v => ok(v.mem_guest_used) ? +v.mem_guest_used : ok(v.mem_rss) ? +v.mem_rss : null;
const vmSpec = v => `${v.vcpus || '?'} 核 · ${F.bytesStr(v.mem_max)}`;
/** 副标题：飞牛的内部名是随机串（如 q1nrg738），有系统信息时优先显示系统，内部名放到详情里 */
const vmSub = v => (v.os || (v.title ? v.name : '')) ? `${v.os || v.name} · ${vmSpec(v)}` : vmSpec(v);
const VMs = {
  filter: 'all', q: '', pending: {},
  PEND: { start: '正在开机…', shutdown: '正在关机…', reboot: '正在重启…', suspend: '正在挂起…', resume: '正在恢复…', destroy: '正在强制关机…' },
  DONE: { start: '已开机', shutdown: '已关机', reboot: '已发送重启', suspend: '已挂起', resume: '已恢复', destroy: '已强制关机' },
  VERB: { start: '开机', shutdown: '关机', reboot: '重启', suspend: '挂起', resume: '恢复', destroy: '强制关机' },
  ICON: { start: 'play', shutdown: 'power', reboot: 'restart', suspend: 'pause', resume: 'play', destroy: 'bolt' },
  /** 需二次确认的操作；撤销对应的反向操作 */
  ARM: ['shutdown', 'reboot', 'suspend', 'destroy'],
  UNDO: { start: 'shutdown', shutdown: 'start', destroy: 'start', suspend: 'resume', resume: 'suspend' },
  list() { return vmOk(S.vms) ? S.vms.vms || [] : []; },
  find(u) { return this.list().find(v => v.uuid === u) || null; },
  setFilter(f) { this.filter = f; const m = MODS.vms; if (m && m.seg) m.seg.set(f); this.apply(); },
  reached(a, v) { return !v ? a === 'destroy' : a === 'start' || a === 'resume' ? vmRun(v) : a === 'suspend' ? v.state === 'paused' : a === 'shutdown' || a === 'destroy' ? !v.running : true; },
  /** 操作后轮询到目标状态；ACPI 关机要等客户机自己关，最多等约 1 分钟 */
  async refreshUntil(uuid, a) {
    const waits = a === 'shutdown' ? [1500, 3000, 5000, 8000, 12000, 15000, 15000] : a === 'reboot' ? [1500] : [800, 1500, 3000, 5000];
    for (const ms of waits) {
      await new Promise(r => setTimeout(r, ms));
      try { S.vms = await API.get('/api/vms'); feedVms(S.vms); } catch (e) { continue; }
      if (this.reached(a, this.find(uuid))) return true;
    }
    return false;
  },
  async act(uuid, name, a, btn) {
    if (!uuid || this.pending[uuid] || !this.VERB[a]) return;
    if (this.ARM.includes(a) && btn && btn.dataset.armed !== '1') {
      btn.dataset.armed = '1'; btn.classList.add('armed'); btn._old = btn.innerHTML; btn.innerHTML = `${ic(this.ICON[a])}确认${this.VERB[a]}？`;
      btn.setAttribute('aria-label', `确认${this.VERB[a]} ${name}`);
      clearTimeout(btn._t); btn._t = setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ''; btn.classList.remove('armed'); btn.innerHTML = btn._old; } }, 3000);
      return;
    }
    this.pending[uuid] = a; renderNow();
    try {
      const j = await API.post('/api/vm/action', { uuid, action: a });
      if (!j.ok) throw new Error(j.error || '未知错误');
      const done = await this.refreshUntil(uuid, a);
      delete this.pending[uuid]; renderNow();
      if (!done && a === 'shutdown') { toast(`已向 ${name} 发送关机信号，但它仍在运行：客户机可能未响应 ACPI，可在详情中强制关机`, { icon: 'alert', ms: 8000, action: '详情', onAction: () => openSheet('vm/' + uuid) }); return; }
      const undo = this.UNDO[a];
      toast(`${name} ${this.DONE[a]}`, undo ? { action: '撤销', onAction: () => this.act(uuid, name, undo) } : {});
    } catch (e) {
      delete this.pending[uuid]; renderNow();
      toast(`${name} ${this.VERB[a]}失败：${String(e.message).slice(0, 80)}`, { icon: 'alert', ms: 6000 });
    }
  },
  apply() {
    const m = MODS.vms; if (!m || !m.rows) return;
    const q = this.q.trim().toLowerCase(); let shown = 0;
    this.list().forEach(v => {
      const row = m.rows[v.uuid]; if (!row) return;
      const okQ = !q || [v.title, v.name, v.os, v.description, (v.ips || []).join(' ')].join(' ').toLowerCase().includes(q);
      const okF = this.filter === 'all' || (this.filter === 'running' ? v.running : !v.running);
      row.hidden = !(okQ && okF); if (!row.hidden) shown++;
    });
    const any = this.list().length;
    m.x.empty.hidden = !any || shown > 0; m.x.head.hidden = !any || shown === 0;
    setHTML(m.x.emptyT, q ? `没有匹配「${esc(this.q)}」的虚拟机` : this.filter === 'stopped' ? '没有未运行的虚拟机' : '没有运行中的虚拟机');
  },
};
/** 某状态下可用的操作：[行内按钮, 详情页按钮] */
function vmActs(v) {
  if (vmRun(v)) return [['reboot', 'shutdown'], ['reboot', 'shutdown', 'suspend', 'destroy']];
  if (v.state === 'paused') return [['resume'], ['resume', 'destroy']];
  if (v.state === 'shutdown') return [[], ['destroy']];
  if (v.state === 'shutoff' || v.state === 'crashed') return [['start'], ['start']];
  return [[], []];
}
const vmActBtns = v => {
  const ids = `data-id="${esc(v.uuid)}" data-name="${esc(vmName(v))}"`;
  return vmActs(v)[0].map(a => `<button class="act${a === 'shutdown' ? ' danger' : a === 'start' || a === 'resume' ? ' go' : ''}" type="button" data-act="${a}" ${ids} title="${VMs.VERB[a]}" aria-label="${VMs.VERB[a]} ${esc(vmName(v))}">${ic(VMs.ICON[a])}</button>`).join('');
};
const vmRateOk = v => vmRun(v) && ok(v.rx_rate) && ok(v.rd_rate);
function vmIoHTML(v, a, b, la, lb) {
  if (!vmRun(v)) return '<span class="faint">—</span>';
  if (!vmRateOk(v)) return '<span class="faint" title="需要两次采样才能算出速率">采样中…</span>';
  return `<b>${la}${esc(F.rateStr(v[a]))}</b><small>${lb}${esc(F.rateStr(v[b]))}</small>`;
}
/** 没有网卡的虚拟机（飞牛未开启 OVS 桥接时常见）不显示 0 B/s，直接说明无网卡 */
const vmNoNic = v => !(v.nics || []).length;
const vmNetHTML = v => vmRun(v) && vmNoNic(v) ? '<span class="faint">无网卡</span>' : vmIoHTML(v, 'rx_rate', 'tx_rate', '<i class="ar dn">↓</i>', '<i class="ar up">↑</i>');
const vmDiskHTML = v => vmIoHTML(v, 'rd_rate', 'wr_rate', '<i class="ar">读</i>', '<i class="ar">写</i>');
const vmIpsHTML = (v, max = 2) => {
  const ips = v.ips || [];
  if (!ips.length) return `<span class="faint" style="font-size:12px">${vmNoNic(v) ? '无网卡' : vmRun(v) ? '未获取到 IP' : '—'}</span>`;
  return ips.slice(0, max).map(ip => `<span class="port">${esc(ip)}</span>`).join('') + (ips.length > max ? `<span class="port">+${ips.length - max}</span>` : '');
};
const vmUpHTML = v => VMs.pending[v.uuid] ? `<span class="pending-t">${VMs.PEND[VMs.pending[v.uuid]]}</span>` : vmRun(v) ? esc(ok(v.uptime) ? F.dur(v.uptime) : '运行中') : `<span class="faint">${esc(v.state_zh || v.state)}</span>`;
/** 每次拿到新的采样时记录 CPU，用于行内迷你趋势 */
function feedVms(d) {
  if (!vmOk(d) || S._vmFed === d.sampled_at) return; S._vmFed = d.sampled_at;
  (d.vms || []).forEach(v => { const b = S.vmBuf[v.uuid] || (S.vmBuf[v.uuid] = []); push(b, vmRun(v) ? (+v.cpu || 0) : 0, 36); });
}

defineModule({
  id: 'vms', page: 'vms', kicker: 'VIRTUAL MACHINES', title: '虚拟机', big: true,
  actions: m => {
    const f = mount(`<label class="field">${ic('search')}<input type="search" placeholder="搜索名称、备注或 IP" aria-label="搜索虚拟机"></label>`);
    f.querySelector('input').addEventListener('input', e => { VMs.q = e.target.value; VMs.apply(); });
    m.seg = makeSeg([['all', '全部'], ['running', '运行中'], ['stopped', '未运行']], VMs.filter, v => { VMs.filter = v; VMs.apply(); }, 'sm');
    return [f, m.seg];
  },
  build(body, m) {
    body.innerHTML = `<div class="stat-cells dk-sum">
        <div class="stat-cell"><div class="eyebrow">运行中</div><div class="dnum"><span class="dm" data-ref="sRun"></span></div><div class="sub" data-ref="sRunT"></div></div>
        <div class="stat-cell"><div class="eyebrow">虚拟机 CPU</div><div class="dnum"><span class="dm" data-ref="sCpu"></span><span class="unit">%</span></div><div class="sub" data-ref="sCpuT"></div></div>
        <div class="stat-cell"><div class="eyebrow">已分配 vCPU</div><div class="dnum"><span class="dm" data-ref="sVcpu"></span><span class="unit">核</span></div><div class="sub" data-ref="sVcpuT"></div></div>
        <div class="stat-cell"><div class="eyebrow">已分配内存</div><div class="dnum"><span class="dm" data-ref="sMem"></span><span class="unit" data-ref="sMemU"></span></div><div class="sub" data-ref="sMemT"></div></div>
      </div>
      <div class="card ctr-list"><div class="ctr vm ctr-head" data-ref="head" hidden><span>虚拟机</span><span>IP 地址</span><span>CPU · 近几分钟</span><span>内存</span><span class="vm-io">磁盘读写</span><span class="ctr-net">网络</span><span class="ctr-up">运行时长</span><span></span></div><div data-ref="list"></div>
      <div class="empty" data-ref="empty" hidden><svg class="art" viewBox="0 0 120 84" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="26" y="12" width="68" height="44" rx="6" stroke-dasharray="3 4"/><path d="M50 70h20M60 56v14"/><circle cx="92" cy="62" r="9"/><path d="m98.5 68.5 7 7"/></svg><b data-ref="emptyT">没有匹配的虚拟机</b><span>换个关键词，或查看全部虚拟机。</span><button class="btn" type="button" data-ref="clear">清除筛选</button></div>
      <div data-ref="na"></div></div>
      <p class="sub vm-note">关机发送 ACPI 信号，由客户机自行关闭；客户机无响应时可在详情中「强制关机」（相当于直接断电）。内存优先显示客户机自报的已用量（需 virtio balloon 驱动），否则显示宿主上的实际占用。</p>`;
    m.x = collect(body); m.rows = {};
    m.x.clear.addEventListener('click', () => { VMs.q = ''; $('input', m.r.actions).value = ''; VMs.setFilter('all'); });
    m.x.list.addEventListener('click', e => {
      const a = e.target.closest('[data-act]');
      if (a) { e.stopPropagation(); VMs.act(a.dataset.id, a.dataset.name, a.dataset.act, a); return; }
      const row = e.target.closest('.ctr[data-uuid]'); if (row) openSheet('vm/' + row.dataset.uuid);
    });
    m.x.list.addEventListener('keydown', e => { const row = e.target.closest('.ctr[data-uuid]'); if (e.key === 'Enter' && row && e.target === row) openSheet('vm/' + row.dataset.uuid); });
  },
  update(o, m) {
    const d = S.vms; const x = m.x;
    if (!d) { setHTML(x.na, `<div class="empty"><span class="spin"></span><span>正在读取虚拟机…</span></div>`); return; }
    const avail = vmOk(d), vs = avail ? d.vms || [] : [], run = vs.filter(v => v.running);
    setHTML(x.na, avail ? (vs.length ? '' : `<div class="empty">${ic('vm')}<b>还没有虚拟机</b><span>在飞牛「虚拟机」中创建的虚拟机会出现在这里。</span></div>`)
      : `<div class="empty">${ic('vm')}<b>${d.installed ? '无法连接虚拟机服务' : '未检测到虚拟机功能'}</b><span>${esc(d.error || '')}</span></div>`);
    const sig = vs.map(v => v.uuid + v.name + v.title).join();
    if (m.sig !== sig) {
      m.sig = sig; x.list.innerHTML = ''; m.rows = {};
      vs.forEach(v => {
        const row = mount(`<div class="ctr vm" data-uuid="${esc(v.uuid)}" tabindex="0" role="button" aria-label="虚拟机 ${esc(vmName(v))}">
          <div class="ctr-name"><span class="sdot" data-ref="dot"></span><div class="t"><b title="${esc(vmName(v))}">${esc(vmName(v))}</b><small data-ref="spec"></small></div></div>
          <div class="ports" data-ref="ips"></div>
          <div class="ctr-cpu"><div class="spk" data-ref="sp"></div><b data-ref="cpu"></b></div>
          <div class="ctr-mem"><b data-ref="mem"></b><small data-ref="memp"></small></div>
          <div class="ctr-net vm-io" data-ref="io"></div>
          <div class="ctr-net" data-ref="net"></div>
          <div class="ctr-up" data-ref="up"></div>
          <div class="acts" data-ref="acts"></div>
        </div>`);
        row._sp = makeSpark(); row._r.sp.append(row._sp);
        m.rows[v.uuid] = row; x.list.append(row);
      });
    }
    vs.forEach(v => {
      const row = m.rows[v.uuid]; if (!row) return; const r = row._r, pend = VMs.pending[v.uuid], on = vmRun(v), mu = vmMemUsed(v), bb = S.vmBuf[v.uuid] || [];
      setCls(row, 'ctr vm', (v.running ? '' : 'off') + (pend ? ' pending' : ''));
      setCls(r.dot, 'sdot', pend || v.state === 'shutdown' ? 'busy' : on ? '' : 'off');
      setText(r.spec, vmSub(v));
      setHTML(r.ips, vmIpsHTML(v));
      setText(r.cpu, on && ok(v.cpu) ? F.d1(v.cpu) + '%' : '—');
      setText(r.mem, on && mu != null ? F.bytesStr(mu) : '—'); setText(r.memp, on && ok(v.mem) ? '共 ' + F.bytesStr(v.mem) : '');
      row._sp.set([{ values: on && bb.length > 1 ? bb : [0, 0] }], { min: 0, max: Math.max(5, ...bb), live: on ? 'ink' : 'none' });
      setHTML(r.io, vmDiskHTML(v)); setHTML(r.net, vmNetHTML(v));
      setHTML(r.up, vmUpHTML(v));
      setHTML(r.acts, pend ? '<span class="spin" aria-label="处理中"></span>' : vmActBtns(v));
    });
    const cores = run.reduce((a, v) => a + (+v.cpu_cores || 0), 0), vcpu = run.reduce((a, v) => a + (+v.vcpus || 0), 0), mem = run.reduce((a, v) => a + (+v.mem || 0), 0);
    const hc = o && o.cpu && o.cpu.cores, T = o && o.mem && o.mem.total;
    dot(x.sRun).set(avail ? `${run.length}/${vs.length}` : '--'); setText(x.sRunT, !avail ? '虚拟机服务不可用' : vs.length - run.length ? `${vs.length - run.length} 个未运行` : vs.length ? '全部运行中' : '暂无虚拟机');
    dnum(x.sCpu, avail && hc ? cores / hc * 100 : null, F.d1); setText(x.sCpuT, avail ? `约 ${cores.toFixed(1)} 核${hc ? ` · 共 ${hc} 核` : ''}` : '--');
    dnum(x.sVcpu, avail ? vcpu : null); setText(x.sVcpuT, avail && hc ? `宿主 ${hc} 核${vcpu > hc ? ' · 已超分' : ''}` : '运行中的虚拟机');
    dnumU(x.sMem, x.sMemU, avail ? mem : null, F.bytes); setText(x.sMemT, avail && T ? `占物理内存 ${F.int(mem / T * 100)}%` : '--');
    setHTML(m.r.dek, avail ? `<b>${run.length}</b> 个运行中 · <b>${vs.length - run.length}</b> 个未运行　KVM 硬件加速${d.kvm ? '可用' : '<span class="st-warn">不可用</span>'}` : esc(d.error || '虚拟机服务不可用'));
    VMs.apply();
  },
});

SHEETS.vm = {
  icon: () => 'vm', title: u => { const v = VMs.find(u); return v ? vmName(v) : '虚拟机'; },
  sub: (o, u) => { const v = VMs.find(u); return v ? vmSub(v) : ''; },
  build(b, u, ctx) {
    if (!VMs.find(u)) {
      // 直接打开链接时虚拟机列表可能还没加载：先显示读取中，列表到达后重新打开
      if (!S.vms) { ctx.retry = true; b.innerHTML = `<div class="empty"><span class="spin"></span><span>正在读取虚拟机…</span></div>`; Poll.run('vms'); return; }
      b.innerHTML = `<div class="empty"><b>未找到该虚拟机</b><span>它可能已被删除，或虚拟机服务当前不可用。</span></div>`; return;
    }
    b.innerHTML = `<div class="sheet-hero"><div><div class="eyebrow">CPU</div><div class="dnum"><span class="dm" data-ref="cpu"></span><span class="unit">%</span></div></div><div style="text-align:right"><div class="eyebrow" data-ref="memL">内存</div><div class="dnum sec"><span class="dm" data-ref="mem"></span><span class="unit" data-ref="memU"></span></div></div></div>
      <div class="ctr-acts" data-ref="acts"></div>
      <div class="card chart-card"><div class="chart-head"><div class="chart-title">CPU · 近几分钟</div><span data-ref="state"></span></div><div style="height:64px" data-ref="sp"></div></div>
      ${sec('实时读写', '<span class="faint">相邻两次采样均值</span>')}<div class="stat-cells ctr-rates"><div class="stat-cell"><div class="eyebrow">磁盘读</div><div class="dnum"><span class="dm" data-ref="rd"></span><span class="unit" data-ref="rdU"></span></div></div><div class="stat-cell"><div class="eyebrow">磁盘写</div><div class="dnum"><span class="dm" data-ref="wr"></span><span class="unit" data-ref="wrU"></span></div></div><div class="stat-cell"><div class="eyebrow"><i class="ar dn">↓</i>网络接收</div><div class="dnum"><span class="dm" data-ref="rx"></span><span class="unit" data-ref="rxU"></span></div></div><div class="stat-cell"><div class="eyebrow"><i class="ar up">↑</i>网络发送</div><div class="dnum"><span class="dm" data-ref="tx"></span><span class="unit" data-ref="txU"></span></div></div></div>
      ${sec('网卡')}<div data-ref="nics"></div>
      ${sec('磁盘')}<div data-ref="disks"></div>
      ${sec('详情')}<div data-ref="kv"></div>`;
    const x = collect(b), sp = makeSpark(); x.sp.append(sp);
    x.acts.addEventListener('click', e => { const btn = e.target.closest('[data-act]'); if (btn) VMs.act(btn.dataset.id, btn.dataset.name, btn.dataset.act, btn); });
    return () => {
      const v = VMs.find(u); if (!v) return;
      const pend = VMs.pending[v.uuid], on = vmRun(v), mu = vmMemUsed(v), rOk = vmRateOk(v);
      dnum(x.cpu, on ? v.cpu : null, F.d1); dnumU(x.mem, x.memU, on ? mu : null, F.bytes);
      setText(x.memL, on && ok(v.mem_guest_used) ? '内存 · 客户机已用' : on && ok(v.mem_rss) ? '内存 · 宿主占用' : '内存');
      setHTML(x.state, pend ? `<span class="pill">${VMs.PEND[pend]}</span>` : on ? '<span class="pill ok"><span class="dot"></span>运行中</span>' : `<span class="pill">${esc(v.state_zh || v.state)}</span>`);
      const ids = `data-id="${esc(v.uuid)}" data-name="${esc(vmName(v))}"`;
      setHTML(x.acts, pend ? `<span class="spin"></span><span class="pending-t">${VMs.PEND[pend]}</span>` : vmActs(v)[1].map(a => `<button class="btn${a === 'start' || a === 'resume' ? ' primary' : a === 'destroy' || a === 'shutdown' ? ' danger' : ''}" type="button" data-act="${a}" ${ids}>${ic(VMs.ICON[a])}${VMs.VERB[a]}</button>`).join('') || '<span class="faint">当前状态没有可用操作</span>');
      const bb = S.vmBuf[v.uuid]; sp.set([{ values: on && bb && bb.length > 1 ? bb : [0, 0] }], { min: 0, max: Math.max(5, ...(bb || [0])), live: on ? 'ink' : 'none' });
      dnumU(x.rd, x.rdU, rOk ? +v.rd_rate : null, F.rate); dnumU(x.wr, x.wrU, rOk ? +v.wr_rate : null, F.rate);
      dnumU(x.rx, x.rxU, rOk && !vmNoNic(v) ? +v.rx_rate : null, F.rate); dnumU(x.tx, x.txU, rOk && !vmNoNic(v) ? +v.tx_rate : null, F.rate);
      const nics = v.nics || [], disks = v.disks || [];
      setHTML(x.nics, nics.length ? rowsCard(nics.map(n => `<div class="row-i" style="grid-template-columns:minmax(0,1fr) auto"><div style="min-width:0"><b class="mono" style="font-weight:600">${esc(n.mac || '—')}</b><div class="sub">${esc([n.type === 'bridge' ? '桥接' : n.type === 'network' ? '虚拟网络' : n.type === 'direct' ? '直通 macvtap' : n.type, n.source, n.model, n.target].filter(Boolean).join(' · '))}</div></div><span class="ports" style="justify-content:flex-end">${(n.ips || []).length ? n.ips.map(ip => `<span class="port">${esc(ip)}</span>`).join('') : `<span class="faint" style="font-size:12px">${on ? '未获取到 IP' : '—'}</span>`}</span></div>`).join(''))
        : `<div class="card pad">${emptyHTML('没有网卡')}</div>`);
      setHTML(x.disks, disks.length ? rowsCard(disks.map(d => `<div class="row-i" style="grid-template-columns:minmax(0,1fr) auto"><div style="min-width:0"><b style="font-weight:600">${esc(d.dev || '—')}</b> <span class="faint">${esc([d.device === 'cdrom' ? '光驱' : d.device === 'disk' ? '' : d.device, d.bus, d.readonly ? '只读' : ''].filter(Boolean).join(' · '))}</span><div class="cmd-line" title="${esc(d.path)}">${esc(d.path || '（无介质）')}</div></div><span class="tnum" style="text-align:right">${ok(d.capacity) ? `<b>${esc(F.bytesStr(d.capacity))}</b>${ok(d.allocation) ? `<div class="sub">实占 ${esc(F.bytesStr(d.allocation))}</div>` : ''}` : '<span class="faint">—</span>'}</span></div>`).join(''))
        : `<div class="card pad">${emptyHTML('没有磁盘')}</div>`);
      const gfx = (v.graphics || []).filter(g => g.port || g.socket).map(g => `${esc(String(g.type).toUpperCase())} ${g.port ? `:${esc(g.port)}${g.listen ? ` · ${esc(g.listen)}` : ''}` : '· 本机套接字'}`).join('<br>');
      setHTML(x.kv, kvHTML([
        ['内部名称', `<span class="mono">${esc(v.name)}</span>`], ['UUID', `<span class="mono">${esc(v.uuid)}</span>`],
        ['状态', esc(v.state_zh || v.state)], ['已运行', on && ok(v.uptime) ? esc(F.dur(v.uptime)) : '<span class="faint">—</span>'],
        ['vCPU', esc(v.vcpus || '--') + ' 核' + (on && ok(v.cpu_cores) ? `<div class="sub">当前约 ${F.d2(v.cpu_cores)} 核</div>` : '')],
        ['内存', esc(F.bytesStr(v.mem_max)) + (on && ok(v.mem) && v.mem !== v.mem_max ? `<div class="sub">当前分配 ${esc(F.bytesStr(v.mem))}</div>` : '')],
        ['客户机已用', on && ok(v.mem_guest_used) ? esc(F.bytesStr(v.mem_guest_used)) : '<span class="faint">需 virtio balloon 驱动</span>'],
        ['宿主占用', on && ok(v.mem_rss) ? esc(F.bytesStr(v.mem_rss)) : '<span class="faint">—</span>'],
        ['系统', dash(v.os)], ['架构 / 机型', dash([v.arch, v.machine].filter(Boolean).join(' · '))], ['固件', dash(v.firmware)],
        ['开机自启', v.autostart ? '已开启' : '未开启'],
        ['控制台', gfx || `<span class="faint">${on ? '未启用' : '—'}</span>`],
        ['Guest Agent', v.agent ? (v.agent_connected ? '<span class="st-ok">已连接</span>' : '未连接') : '<span class="faint">未配置</span>'],
        ['累计读写', on && ok(v.rd_bytes) ? `磁盘 读 ${F.bytesStr(v.rd_bytes)} · 写 ${F.bytesStr(v.wr_bytes)}<div class="sub">${vmNoNic(v) ? '' : `网络 ↓ ${F.bytesStr(v.rx_bytes)} · ↑ ${F.bytesStr(v.tx_bytes)} · `}自本次开机</div>` : '<span class="faint">—</span>'],
      ].concat(v.description ? [['备注', esc(v.description)]] : [])));
    };
  },
};

/* =====================================================================
   TOAST & POPOVER
   ===================================================================== */
const toasts = {};
function toast(msg, opt = {}) {
  const host = $('#toasts');
  if (opt.key && toasts[opt.key] && toasts[opt.key].isConnected) {
    const t = toasts[opt.key]; setText(t.querySelector('span'), msg); clearTimeout(t._tm); t._tm = setTimeout(() => dismiss(t), opt.ms || 2400); return t;
  }
  const t = mount(`<div class="toast" role="status">${ic(opt.icon || 'check', opt.icon && opt.icon !== 'check' ? '' : 'ok')}<span>${esc(msg)}</span>${opt.action ? `<button type="button">${esc(opt.action)}</button>` : ''}</div>`);
  if (opt.action) t.querySelector('button').onclick = () => { dismiss(t); opt.onAction && opt.onAction(); };
  host.append(t); if (opt.key) toasts[opt.key] = t;
  t._tm = setTimeout(() => dismiss(t), opt.ms || (opt.action ? 5000 : 2400));
  return t;
}
function dismiss(t) { if (!t.isConnected) return; t.classList.add('out'); setTimeout(() => t.remove(), 260); }

let pop = null;
function closePop() { if (pop) { pop.btn.setAttribute('aria-expanded', 'false'); pop.el.remove(); pop = null; } }
function openPop(btn, title, text) {
  if (pop && pop.btn === btn) return closePop();
  closePop();
  const el = mount(`<div class="popover" role="tooltip"><b>${esc(title)}</b>${esc(text)}</div>`);
  document.body.append(el);
  const r = btn.getBoundingClientRect(), w = 280;
  el.style.left = clamp(r.left + r.width / 2 - w / 2 + scrollX, 12 + scrollX, scrollX + innerWidth - w - 12) + 'px';
  el.style.top = (r.bottom + 10 + scrollY) + 'px';
  btn.setAttribute('aria-expanded', 'true');
  pop = { el, btn };
}
document.addEventListener('click', e => { if (pop && !pop.el.contains(e.target) && !pop.btn.contains(e.target)) closePop(); });


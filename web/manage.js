/* ushi - Projects page: every project with its sessions and Beautified documents;
 * rename, delete (to the Trash) and open the folder. A project or session link opens it
 * in the main window. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `${res.status} ${res.statusText}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}
function toast(text, level = 'info', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.innerHTML = `<span>${esc(text)}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, ms);
}
const fail = e => toast(e.message || String(e), 'error', 7000);

// Theme: same switch and storage as the main window
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
try { applyTheme(localStorage.getItem('vr-theme')); } catch (_) { /* */ }
$('#themeBtn').onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const t = dark ? 'light' : 'dark';
  applyTheme(t);
  try { localStorage.setItem('vr-theme', t); } catch (_) { /* */ }
};

const ICON = {
  folder: '<svg viewBox="0 0 20 20" class="ico"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h3.2l1.6 1.8h6.2A1.5 1.5 0 0 1 17 8.3v6.2a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5z"/></svg>',
  edit: '<svg viewBox="0 0 20 20" class="ico"><path d="M13.5 3.5l3 3L7 16H4v-3z"/><path d="M11.5 5.5l3 3"/></svg>',
  trash: '<svg viewBox="0 0 20 20" class="ico"><path d="M4 6h12M8 6V4.5h4V6M6 6l.7 10h6.6L14 6"/></svg>',
  doc: '<svg viewBox="0 0 20 20" class="ico"><path d="M6 3h6l3 3v11H6z"/><path d="M12 3v3h3M8.5 10h4M8.5 13h4"/></svg>',
};

const SHOWN = 4;        // sessions listed before "Show all"
const st = { projects: [], q: '', renaming: null, expanded: new Set() };

const fmtDate = d => {
  if (!d) return '';
  const t = new Date(`${d}T00:00:00`);
  return isNaN(t) ? d : t.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' });
};
const hm = t => (t || '').slice(0, 5);
const span = (a, b) => (!a ? '' : a === b || !b ? fmtDate(a) : `${fmtDate(a)} – ${fmtDate(b)}`);
const mainUrl = (p, sid) => `/?${new URLSearchParams(sid ? { project: p, session: sid } : { project: p })}`;

function sessionRow(p, s) {
  const time = s.start ? `${hm(s.start)}${s.end && s.end !== s.start ? `–${hm(s.end)}` : ''}` : '';
  const docs = s.versions.map(v => `<a class="pm-doc ${v.mode === 'final' ? 'final' : ''}" href="${esc(v.url)}" target="_blank" rel="noopener"
      title="${esc(v.title || 'Beautified transcript')}${v.created ? ` · built ${esc(v.created.replace('T', ' ').slice(0, 16))}` : ''}">${ICON.doc}v${v.v}${v.mode === 'final' ? ' final' : ''}</a>`).join('');
  return `<div class="pm-sess">
      <a class="pm-when" href="${mainUrl(p.name, s.sid)}" title="Open this session in the main window">
        <span class="pm-date">${esc(fmtDate(s.date) || s.sid)}</span><span class="pm-time">${esc(time)}</span></a>
      <span class="pm-lines">${s.lines} line${s.lines === 1 ? '' : 's'}</span>
      <span class="pm-docs">${docs || '<span class="pm-none">not beautified</span>'}</span>
    </div>`;
}

function card(p) {
  const n = p.sessions.length;
  const all = st.expanded.has(p.name) ? p.sessions : p.sessions.slice(0, SHOWN);
  const head = st.renaming === p.name
    ? `<form class="pm-rename" data-rename="${esc(p.name)}">
         <input class="field" name="name" value="${esc(p.name)}" spellcheck="false" autocomplete="off">
         <button class="primary-btn small" type="submit">Rename</button>
         <button class="ghost-btn small" type="button" data-cancel>Cancel</button>
       </form>`
    : `<div class="pm-name-row"><a class="pm-name" href="${mainUrl(p.name)}" title="Open in the main window">${esc(p.name)}</a>${p.live ? '<span class="pm-live">● Recording</span>' : ''}</div>`;
  return `<article class="pm-proj panel" data-p="${esc(p.name)}">
    <div class="pm-head">
      <div class="pm-names">
        ${head}
        ${p.title ? `<a class="pm-title" href="${esc(p.title_url)}" target="_blank" rel="noopener" title="The latest Beautified transcript">${esc(p.title)}</a>` : ''}
        <div class="pm-meta">${n} session${n === 1 ? '' : 's'}${p.first ? ` · ${esc(span(p.first, p.last))}` : ''}</div>
      </div>
      <div class="pm-actions">
        <button class="ghost-btn small" data-open title="Show the project folder">${ICON.folder}Open</button>
        <button class="ghost-btn small" data-edit title="Rename the project folder">${ICON.edit}Rename</button>
        <button class="ghost-btn small pm-del" data-del title="Move the project folder to the Trash">${ICON.trash}Delete</button>
      </div>
    </div>
    ${n ? `<div class="pm-sessions">${all.map(s => sessionRow(p, s)).join('')}
      ${n > SHOWN ? `<button class="link-btn pm-more" data-more>${st.expanded.has(p.name) ? 'Show fewer' : `Show all ${n} sessions`}</button>` : ''}</div>` : ''}
  </article>`;
}

function render() {
  const q = st.q.trim().toLowerCase();
  const list = st.projects.filter(p => !q || p.name.toLowerCase().includes(q) || (p.title || '').toLowerCase().includes(q)
    || p.sessions.some(s => s.versions.some(v => (v.title || '').toLowerCase().includes(q))));
  $('#sub').textContent = `${st.projects.length} project${st.projects.length === 1 ? '' : 's'}`;
  $('#list').innerHTML = list.length ? list.map(card).join('')
    : `<div class="empty"><h3>${st.projects.length ? 'Nothing matches' : 'No projects yet'}</h3><p>${st.projects.length ? 'Try another name or title.' : 'Create one from the project menu of the main window.'}</p></div>`;
  const inp = $('.pm-rename input');
  if (inp) { inp.focus(); inp.select(); }
}

async function load() {
  try {
    st.projects = await api('GET', '/api/projects/overview');
    render();
  } catch (e) { fail(e); }
}

async function rename(old, name) {
  name = name.trim().replace(/\s+/g, '-');
  if (!name || name === old) { st.renaming = null; render(); return; }
  try {
    const r = await api('POST', `/api/projects/${encodeURIComponent(old)}/rename`, { name });
    st.renaming = null;
    if (st.expanded.delete(old)) st.expanded.add(r.name);
    toast(`Renamed to “${r.name}”`, 'ok');
    await load();
  } catch (e) { fail(e); }
}

function closeModal() { $('#modal').classList.add('hidden'); }
$('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });

function confirmDelete(p) {
  const docs = p.sessions.reduce((a, s) => a + s.versions.length, 0);
  $('#modalBody').innerHTML = `
    <div class="modal-head"><h2>Delete “${esc(p.name)}”?</h2>
      <p>The project folder goes to the Trash with everything in it: ${p.sessions.length} session${p.sessions.length === 1 ? '' : 's'},
      screenshots, frames${docs ? ` and ${docs} Beautified document${docs === 1 ? '' : 's'}` : ''}. You can put it back from there.</p></div>
    <div class="modal-foot"><button class="ghost-btn" data-close>Cancel</button><button class="danger-btn" id="delGo">Move to Trash</button></div>`;
  $('#modal').classList.remove('hidden');
  $('[data-close]', $('#modalBody')).onclick = closeModal;
  const go = $('#delGo');
  go.focus();
  go.onclick = async () => {
    go.disabled = true;
    try {
      const r = await api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`);
      closeModal();
      toast(`“${p.name}” moved to ${r.trash}`, 'ok', 6000);
      await load();
    } catch (e) { go.disabled = false; fail(e); }
  };
}

$('#list').addEventListener('click', e => {
  const box = e.target.closest('[data-p]'); if (!box) return;
  const p = st.projects.find(x => x.name === box.dataset.p); if (!p) return;
  if (e.target.closest('[data-open]')) api('POST', '/api/reveal', { url: p.url }).catch(fail);
  else if (e.target.closest('[data-edit]')) { st.renaming = p.name; render(); }
  else if (e.target.closest('[data-cancel]')) { st.renaming = null; render(); }
  else if (e.target.closest('[data-del]')) confirmDelete(p);
  else if (e.target.closest('[data-more]')) { st.expanded.has(p.name) ? st.expanded.delete(p.name) : st.expanded.add(p.name); render(); }
});
$('#list').addEventListener('submit', e => {
  const f = e.target.closest('[data-rename]'); if (!f) return;
  e.preventDefault();
  rename(f.dataset.rename, f.elements.name.value);
});
$('#list').addEventListener('keydown', e => {
  if (e.key === 'Escape' && e.target.closest('.pm-rename')) { st.renaming = null; render(); }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
$('#search').addEventListener('input', e => { st.q = e.target.value; render(); });
// Back from the main window or another app: titles may be new
document.addEventListener('visibilitychange', () => { if (!document.hidden && !st.renaming) load(); });

load();

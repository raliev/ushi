/* ushi - Batch page: the queue of videos (batch.py runs it inside server.py).
 * Add: paste links (one per line, or CSV rows url,project,auto_frames,beautify; YouTube, any site yt-dlp knows, .m3u8,
 * or a whole "Copy as cURL" command for a stream behind a login; local video / audio files: Choose files, read
 * in place, never copied) → set up each
 * video → tasks. Each task: download → transcribe → beautify; a stage that failed or was
 * interrupted is "on hold" with its reason and a Restart button. */
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

const st = { tasks: [], open: new Set(), draft: null, text: '', rows: [] };

// ── Links / CSV rows / cURL commands (parsed by batch.py: /api/batch/parse) ──
const NAME_RE = /^[A-Za-z0-9._ -]+$/;
const host = u => { try { return new URL(u).host; } catch (_) { return ''; } };
// YouTube has a thumbnail per id; a stream has none we could load (its CDN wants the site's Referer)
const thumbHtml = (kind, id, cls = 'b-thumb', audio = false) => (kind === 'file'
  ? `<div class="${cls} b-thumb-stream" title="Local ${audio ? 'audio' : 'video'} file">${audio ? '♪' : '▶'}</div>`
  : kind === 'stream'
    ? `<div class="${cls} b-thumb-stream" title="Video stream">▶</div>`
    : `<img class="${cls}" src="https://i.ytimg.com/vi/${encodeURIComponent(id)}/mqdefault.jpg" alt="" loading="lazy">`);
const fmtSize = b => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`);
const srcLabel = r => (r.kind === 'file'
  ? `${r.url}${r.size ? ` · ${fmtSize(r.size)}` : ''}`
  : r.kind === 'stream'
    ? `${host(r.url)}${r.header_names && r.header_names.length ? ` · ${r.header_names.length} header${r.header_names.length > 1 ? 's' : ''} from cURL` : ''}`
    : r.video_id);

let parseSeq = 0;
async function parseText(text) {
  const seq = ++parseSeq;
  const rows = text.trim() ? (await api('POST', '/api/batch/parse', { text })).rows : [];
  return seq === parseSeq ? rows : null;        // null: a newer edit is being parsed
}

// ── Add panel: step 1 (links) / step 2 (set up each video) ─────────────────
function renderAdd() {
  const el = $('#addPanel'), d = st.draft;
  if (!d) {
    el.innerHTML = `
      <div class="b-step"><span class="b-num">1</span><h2>Add videos</h2></div>
      <p class="b-help">One link per line: YouTube, another video site yt-dlp knows (VK Video, Vimeo, Rutube…), or a video stream (<span class="mono">.m3u8</span>). CSV rows work too: <span class="mono">url,project,auto_frames,beautify</span></p>
      <p class="b-help">Video or audio files on this computer: <b>Choose files…</b> (or paste their full paths, one per line). They are read where they are, nothing is copied: keep them there until the transcription is done.</p>
      <p class="b-help">A video behind a login: open it in the browser, DevTools → Network → filter <span class="mono">m3u8</span> → right-click the request → Copy → <b>Copy as cURL (bash)</b>, and paste the whole command here. Its headers (Referer, cookies) go along with the download. Prefer <span class="mono">playlist.m3u8</span> over <span class="mono">480p/video.m3u8</span>: it has every quality, the best one is taken.</p>
      <textarea class="field b-urls" id="urls" rows="9" spellcheck="false" placeholder="https://www.youtube.com/watch?v=…&#10;https://…/playlist.m3u8&#10;curl 'https://…/playlist.m3u8' -H 'referer: …'">${esc(st.text)}</textarea>
      <div class="b-foot"><button class="ghost-btn" id="pickBtn" title="Pick video / audio files on this computer: they are transcribed where they are, not copied">Choose files…</button>
        <span class="b-help" id="urlCount"></span><div class="spacer"></div>
        <button class="primary-btn" id="nextBtn">Next: set up</button></div>`;
    const ta = $('#urls');
    let timer = 0;
    const show = () => {
      const rows = st.rows, bad = rows.filter(r => r.error);
      $('#urlCount').innerHTML = rows.length ? `${rows.length} video${rows.length > 1 ? 's' : ''}${bad.length ? ` · <span class="b-bad" title="${esc(bad.map(r => r.error).join('\n'))}">${bad.length} not usable: ${esc(bad[0].error)}</span>` : ''}` : '';
      $('#nextBtn').disabled = !rows.length || bad.length > 0;
    };
    const count = (now = false) => {
      st.text = ta.value;
      $('#nextBtn').disabled = true;
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const rows = await parseText(ta.value);
          if (rows) { st.rows = rows; show(); }
        } catch (e) { fail(e); }
      }, now ? 0 : 250);
    };
    ta.oninput = () => count(); count(true);
    ta.onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !$('#nextBtn').disabled) $('#nextBtn').click(); };
    $('#nextBtn').onclick = () => { st.draft = st.rows.map(r => ({ ...r })); renderAdd(); };
    $('#pickBtn').onclick = async () => {
      const b = $('#pickBtn'); b.disabled = true;
      try {
        const { paths } = await api('POST', '/api/batch/pick');
        if (paths.length) {
          const have = ta.value.replace(/\s+$/, '');
          ta.value = (have ? `${have}\n` : '') + paths.join('\n') + '\n';
          count(true);
        }
      } catch (e) { fail(e); }
      b.disabled = false;
    };
    return;
  }
  const allOn = k => d.every(r => r[k] || (k === 'auto_frames' && r.audio_only));
  el.innerHTML = `
    <div class="b-step"><span class="b-num">2</span><h2>Set up ${d.length} video${d.length > 1 ? 's' : ''}</h2></div>
    <p class="b-help">Each video gets its own project. Name it now: the video id is only a placeholder.</p>
    <div class="b-cfg-head">
      <span>Video / project</span>
      <label class="switch" title="Auto frames for all"><input type="checkbox" id="allAf" ${allOn('auto_frames') ? 'checked' : ''}><span class="knob"></span>Frames</label>
      <label class="switch" title="Beautify for all"><input type="checkbox" id="allBf" ${allOn('beautify') ? 'checked' : ''}><span class="knob"></span>Beautify</label>
    </div>
    <div class="b-cfg">${d.map((r, i) => `
      <div class="b-cfg-row" data-i="${i}">
        ${thumbHtml(r.kind, r.video_id, 'b-thumb', r.audio_only)}
        <div class="b-cfg-main">
          <input class="field b-proj" value="${esc(r.project)}" placeholder="Project name" title="Project (folder) for this video: letters, digits, spaces, . _ -">
          <div class="b-cfg-url mono" title="${esc(r.url)}">${esc(srcLabel(r))}${projectExists(r.project) ? ' · <span class="b-note">project exists: a new session is added</span>' : ''}</div>
        </div>
        <label class="switch" title="${r.audio_only ? 'An audio file has no frames' : 'Auto frames: keep the frames of the video that show something new'}"><input type="checkbox" data-k="auto_frames" ${r.auto_frames ? 'checked' : ''} ${r.audio_only ? 'disabled' : ''}><span class="knob"></span></label>
        <label class="switch" title="Beautify: build the annotated document when the transcript is ready"><input type="checkbox" data-k="beautify" ${r.beautify ? 'checked' : ''}><span class="knob"></span></label>
        <button class="icon-btn b-x" title="Leave this one out">✕</button>
      </div>`).join('')}</div>
    <div class="b-foot"><button class="ghost-btn" id="backBtn">Back</button><div class="spacer"></div>
      <button class="primary-btn" id="addBtn">Add ${d.length} task${d.length > 1 ? 's' : ''}</button></div>`;
  el.querySelectorAll('.b-cfg-row').forEach(row => {
    const r = d[+row.dataset.i];
    const inp = $('.b-proj', row);
    inp.oninput = () => { r.project = inp.value.trim(); inp.classList.toggle('bad', !NAME_RE.test(r.project) || r.project.startsWith('.')); };
    row.querySelectorAll('input[data-k]').forEach(c => { c.onchange = () => { r[c.dataset.k] = c.checked; }; });
    $('.b-x', row).onclick = () => { d.splice(+row.dataset.i, 1); if (!d.length) st.draft = null; renderAdd(); };
  });
  $('#allAf').onchange = e => { d.forEach(r => { r.auto_frames = e.target.checked && !r.audio_only; }); renderAdd(); };
  $('#allBf').onchange = e => { d.forEach(r => { r.beautify = e.target.checked; }); renderAdd(); };
  $('#backBtn').onclick = () => { st.draft = null; renderAdd(); };
  $('#addBtn').onclick = async () => {
    const bad = d.find(r => !NAME_RE.test(r.project) || r.project.startsWith('.'));
    if (bad) { toast(`Project name “${bad.project}”: use letters, digits, spaces, . _ -`, 'error'); return; }
    $('#addBtn').disabled = true;
    try {
      await api('POST', '/api/batch/tasks', { tasks: d.map(r => ({ url: r.url, headers: r.headers, project: r.project, auto_frames: r.auto_frames, beautify: r.beautify })) });
      toast(`${d.length} task${d.length > 1 ? 's' : ''} added`, 'ok');
      st.draft = null; st.text = ''; st.rows = [];
      renderAdd();
    } catch (e) { fail(e); $('#addBtn').disabled = false; }
  };
}

let projects = new Set();
const projectExists = name => projects.has(name);
api('GET', '/api/projects').then(list => { projects = new Set(list.map(p => p.name)); if (st.draft) renderAdd(); }).catch(() => {});

// ── Queue ──────────────────────────────────────────────────────────────────
const STAGE_LABEL = { download: 'Download', recognize: 'Transcribe', beautify: 'Beautify' };
const STATE_LABEL = {
  queued: 'Queued', download: 'Downloading', recognize: 'Transcribing', beautify: 'Beautifying',
  'waiting-recognize': 'Waiting to transcribe', 'waiting-beautify': 'Waiting to beautify', hold: 'On hold', done: 'Done',
};
const ICONS = {
  done: '<svg viewBox="0 0 20 20" class="ico"><path d="M5 10.5l3 3 7-7"/></svg>',
  hold: '<svg viewBox="0 0 20 20" class="ico"><path d="M8 5v10M12 5v10"/></svg>',
  skipped: '<svg viewBox="0 0 20 20" class="ico"><path d="M5 10h10"/></svg>',
};

function stagePill(t, name) {
  const s = t.stages[name];
  if (name === 'download' && t.kind === 'file')
    return `<div class="b-stage done" title="${esc(t.url)}: read where it is, not copied">${ICONS.done}<span>Local file</span></div>`;
  const pct = s.status === 'running' && s.progress != null ? ` ${Math.round(s.progress)}%` : '';
  const icon = s.status === 'running' ? '<span class="spinner tiny"></span>' : (ICONS[s.status] || '<span class="b-dot"></span>');
  const waiting = s.status === 'pending' && t.state === `waiting-${name}`;
  return `<div class="b-stage ${s.status}${waiting ? ' waiting' : ''}" title="${esc(STAGE_LABEL[name])}: ${esc(waiting ? 'waiting for its turn' : s.status)}">
    ${icon}<span>${STAGE_LABEL[name]}${pct}</span>
    ${s.status === 'running' && s.progress != null ? `<i style="width:${Math.max(0, Math.min(100, s.progress))}%"></i>` : ''}</div>`;
}

function activity(t) {
  const name = ['download', 'recognize', 'beautify'].find(n => t.stages[n].status === 'running');
  if (name) {
    const s = t.stages[name];
    const txt = [s.note, s.line].filter(Boolean).join(' · ');
    return `<div class="b-act mono">${esc(txt || 'Starting…')}</div>`;
  }
  if (t.state === 'waiting-recognize') return '<div class="b-act">Downloaded · waiting for the recognizer (one video at a time)</div>';
  if (t.state === 'waiting-beautify') return '<div class="b-act">Transcribed · waiting for Beautify (one at a time)</div>';
  if (t.state === 'queued') return '<div class="b-act">Waiting for a download slot</div>';
  if (t.state === 'done') {
    const r = t.stages.recognize, b = t.stages.beautify;
    return `<div class="b-act">Finished${b.status === 'done' && b.v ? ` · document v${b.v}` : ''}</div>`;
  }
  return '';
}

function holdBox(t) {
  return ['download', 'recognize', 'beautify'].filter(n => t.stages[n].status === 'hold').map(n => {
    const s = t.stages[n];
    const cont = n === 'recognize' && s.position ? ` (continues from ${fmtHms(s.position)})` : '';
    return `<div class="b-hold"><div class="b-hold-head">${ICONS.hold}<b>${STAGE_LABEL[n]} is on hold</b><div class="spacer"></div>
      <button class="ghost-btn small" data-restart="${n}" title="Take it off hold and run the ${STAGE_LABEL[n].toLowerCase()} stage again${esc(cont)}">Restart ${STAGE_LABEL[n].toLowerCase()}${esc(cont)}</button></div>
      <pre class="b-err">${esc(s.error || 'Stopped')}</pre></div>`;
  }).join('');
}

const fmtHms = sec => { sec = Math.floor(+sec || 0); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; return `${h ? `${h}:` : ''}${String(m).padStart(h ? 2 : 1, '0')}:${String(s).padStart(2, '0')}`; };

function taskCard(t) {
  const log = t.stages.download.log || [];
  const open = st.open.has(t.id);
  const sub = t.stages.download.subtitles;
  const opts = [t.auto_frames ? 'auto frames' : '', t.beautify ? 'beautify' : 'no beautify',
    sub ? `subtitles: ${sub.lang}${sub.auto ? ' (auto)' : ''}` : ''].filter(Boolean).join(' · ');
  const sid = t.stages.recognize.sid;
  return `<article class="b-task ${t.state}" data-id="${t.id}">
    ${t.kind === 'stream' || t.kind === 'file' ? `<div class="b-thumb-link">${thumbHtml(t.kind, '', 'b-thumb', t.audio_only)}</div>`
    : `<a class="b-thumb-link" href="${esc(t.url)}" target="_blank" rel="noopener" title="Open on YouTube">${thumbHtml('youtube', t.video_id)}</a>`}
    <div class="b-body">
      <div class="b-row1">
        <div class="b-name" title="${esc(t.title || t.url)}">${esc(t.title || (t.kind === 'stream' ? t.project : t.url))}</div>
        <span class="b-badge ${t.state.startsWith('waiting') ? 'waiting' : t.state}">${esc(STATE_LABEL[t.state] || t.state)}</span>
        <button class="icon-btn b-del" title="Remove from the queue (a running stage is stopped; the project and its files stay)">✕</button>
      </div>
      <div class="b-meta"><a href="/?project=${encodeURIComponent(t.project)}${sid ? `&session=${encodeURIComponent(sid)}` : ''}" target="ushi-main" title="Open the project in the main window"><b>${esc(t.project)}</b></a> · ${esc(opts)} · <span class="mono" title="${esc(t.url)}">${esc(srcLabel(t))}</span></div>
      <div class="b-stages">${['download', 'recognize', 'beautify'].map(n => stagePill(t, n)).join('<span class="b-link"></span>')}</div>
      ${activity(t)}
      ${holdBox(t)}
      ${log.length ? `<button class="link-btn b-logbtn">${open ? 'Hide' : 'Show'} yt-dlp output</button>${open ? `<pre class="b-log mono">${esc(log.join('\n'))}</pre>` : ''}` : ''}
    </div>
  </article>`;
}

function renderList() {
  const list = $('#list'), tasks = st.tasks;
  if (!tasks.length) {
    list.innerHTML = `<div class="empty"><h3>The queue is empty</h3><p>Add YouTube, other video sites, .m3u8 links or local files on the left. Each video is downloaded, transcribed and (optionally) beautified in its own project.</p></div>`;
  } else {
    // keep an open log scrolled to its end
    list.innerHTML = tasks.map(taskCard).join('');
    list.querySelectorAll('.b-log').forEach(p => { p.scrollTop = p.scrollHeight; });
  }
  const n = s => tasks.filter(t => (s === 'waiting' ? t.state.startsWith('waiting') || t.state === 'queued' : t.state === s)).length;
  const parts = [['download', 'downloading'], ['recognize', 'transcribing'], ['beautify', 'beautifying'], ['waiting', 'waiting'], ['hold', 'on hold'], ['done', 'done']]
    .map(([k, l]) => [n(k), l, k]).filter(([c]) => c);
  $('#summary').innerHTML = parts.map(([c, l, k]) => `<span class="b-sum ${k}">${c} ${l}</span>`).join('');
  $('#clearDone').classList.toggle('hidden', !n('done'));
  document.title = `ushi · Batch${tasks.some(t => !['done', 'hold'].includes(t.state)) ? ' · running' : ''}`;
}

$('#list').addEventListener('click', async e => {
  const card = e.target.closest('.b-task'); if (!card) return;
  const id = card.dataset.id;
  if (e.target.closest('.b-logbtn')) {
    if (st.open.has(id)) st.open.delete(id);
    else {
      st.open.add(id);
      try { const full = await api('GET', `/api/batch/tasks/${id}`); upsert(full); } catch (_) { /* the live tail will do */ }
    }
    renderList();
  } else if (e.target.closest('[data-restart]')) {
    const b = e.target.closest('[data-restart]'); b.disabled = true;
    try { upsert(await api('POST', `/api/batch/tasks/${id}/restart`, { stage: b.dataset.restart })); renderList(); }
    catch (err) { fail(err); b.disabled = false; }
  } else if (e.target.closest('.b-del')) {
    const t = st.tasks.find(x => x.id === id);
    const running = t && ['download', 'recognize', 'beautify'].includes(t.state);
    if (running && !confirmDelete(card)) return;
    try { await api('DELETE', `/api/batch/tasks/${id}`); } catch (err) { fail(err); }
  }
});
// Removing a running task stops its stage: ask with a second click instead of a dialog.
function confirmDelete(card) {
  const b = $('.b-del', card);
  if (b.classList.contains('armed')) return true;
  b.classList.add('armed'); b.textContent = 'Stop & remove?';
  setTimeout(() => { b.classList.remove('armed'); b.textContent = '✕'; }, 3500);
  return false;
}
$('#clearDone').onclick = async () => {
  for (const t of st.tasks.filter(x => x.state === 'done')) {
    try { await api('DELETE', `/api/batch/tasks/${t.id}`); } catch (e) { fail(e); break; }
  }
};

function upsert(task) {
  const i = st.tasks.findIndex(t => t.id === task.id);
  if (i >= 0) {
    // the live updates carry only the log's tail: keep the full one we fetched
    const old = st.tasks[i].stages.download.log || [];
    if (st.open.has(task.id) && old.length > (task.stages.download.log || []).length) {
      const tail = task.stages.download.log || [];
      const at = tail.length ? old.lastIndexOf(tail[0]) : -1;
      task.stages.download.log = at >= 0 ? old.slice(0, at).concat(tail) : old;
    }
    st.tasks[i] = task;
  } else st.tasks.push(task);
}

// ── Settings: downloads at once ────────────────────────────────────────────
$('#parInp').onchange = async e => {
  try { e.target.value = (await api('POST', '/api/batch/settings', { parallel: +e.target.value || 3 })).parallel; }
  catch (err) { fail(err); }
};

// ── Live updates ───────────────────────────────────────────────────────────
let ws, retry = 0, renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => {
    renderPending = false;
    // don't re-render under an open text selection in a log / error (copying it)
    if (String(getSelection()).length && $('#list').contains(getSelection().anchorNode)) { setTimeout(scheduleRender, 500); return; }
    renderList();
  });
}
function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { if (retry) load(); retry = 0; };
  ws.onclose = () => setTimeout(connect, Math.min(5000, 500 * 2 ** retry++));
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === 'batch_task') { upsert(m.task); scheduleRender(); }
    else if (m.type === 'batch_removed') { st.tasks = st.tasks.filter(t => t.id !== m.id); st.open.delete(m.id); scheduleRender(); }
  };
}
async function load() {
  try {
    const d = await api('GET', '/api/batch');
    st.tasks = d.tasks;
    $('#parInp').value = d.parallel;
    const y = d.ytdlp, h = d.hints, warn = $('#ytdlpWarn');
    const stale = y && y.age_days > d.ytdlp_stale_days;
    if (!y) warn.innerHTML = `yt-dlp is not installed, downloads will be put on hold: <span class="mono">${esc(h.ytdlp)}</span>`;
    else if (stale) warn.innerHTML = `yt-dlp ${esc(y.version)} is ${y.age_days} days old; YouTube often stops answering old versions. Update it: <span class="mono">${esc(h.ytdlp_update)}</span>`;
    else if (!y.deno) warn.innerHTML = `YouTube downloads need a JavaScript runtime for yt-dlp and none was found: <span class="mono">${esc(h.deno)}</span> (then restart the app)`;
    warn.classList.toggle('hidden', !!y && !stale && y.deno);
    renderList();
  } catch (e) { fail(e); }
}

renderAdd();
load().then(connect);

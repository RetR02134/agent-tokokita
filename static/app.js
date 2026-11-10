const log = document.getElementById('log'), input = document.getElementById('input');
const send = document.getElementById('send'), chips = document.getElementById('chips'), stopBtn = document.getElementById('stop');
let controller = null;

/* ---------- teks: markdown sederhana (aman: semua HTML di-escape) ---------- */
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function inline(s){
  s = esc(s);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  return s;
}
function cells(line){ return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim()); }
function isSep(line){ return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line) && line.includes('-'); }
function renderText(md){
  md = md.replace(/\[([^\]]*)\]\(sandbox:[^)]*\)/g, '$1');
  const lines = md.split('\n'); let out = '', list = false, para = [], i = 0;
  const flush = () => { if (para.length) { out += '<p>' + para.join('<br>') + '</p>'; para = []; } };
  const closeList = () => { if (list) { out += '</ul>'; list = false; } };
  while (i < lines.length) {
    const raw = lines[i];
    if (raw.trim().startsWith('|') && i + 1 < lines.length && isSep(lines[i + 1])) {
      flush(); closeList();
      const head = cells(raw); i += 2; const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(cells(lines[i])); i++; }
      out += '<div class="tbl"><table><thead><tr>' + head.map(h => '<th>' + inline(h) + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + head.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
      continue;
    }
    const h = raw.match(/^\s*#{1,4}\s+(.*)$/);
    const m = raw.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (h) { flush(); closeList(); out += '<p><strong>' + inline(h[1]) + '</strong></p>'; }
    else if (m) { flush(); if (!list) { out += '<ul>'; list = true; } out += '<li>' + inline(m[1]) + '</li>'; }
    else { closeList(); if (raw.trim() === '') flush(); else para.push(inline(raw)); }
    i++;
  }
  closeList(); flush(); return out;
}

/* ---------- grafik: blok ```chart {json}``` digambar sebagai SVG ---------- */
const PAL = ['#1f4e9c', '#1b9aaa', '#e07a1f'];
function fmtVal(v, s){
  const n = Number(v);
  if (s.format === 'rupiah') return 'Rp' + Math.round(n).toLocaleString('id-ID');
  const t = n.toLocaleString('id-ID', {maximumFractionDigits: 2});
  return s.unit ? t + ' ' + s.unit : t;
}
function parseChart(raw){
  let s; try { s = JSON.parse(raw); } catch (e) { return null; }
  if (!s || !['bar', 'line'].includes(s.type) || !Array.isArray(s.labels) || !Array.isArray(s.datasets)) return null;
  if (s.labels.length < 1 || s.labels.length > 20 || s.datasets.length < 1 || s.datasets.length > 3) return null;
  for (const d of s.datasets) {
    if (!Array.isArray(d.data) || d.data.length !== s.labels.length || d.data.some(x => typeof x !== 'number' || !isFinite(x))) return null;
  }
  return s;
}
function cut(t, n){ t = String(t); return t.length > n ? t.slice(0, n - 1) + '…' : t; }
function legend(s, W){
  if (s.datasets.length < 2) return '';
  let x = 10, out = '';
  s.datasets.forEach((d, j) => {
    out += '<rect x="' + x + '" y="6" width="12" height="12" rx="2" fill="' + PAL[j] + '"/><text x="' + (x + 17) + '" y="16" font-size="12" fill="currentColor">' + esc(cut(d.label || ('Seri ' + (j + 1)), 22)) + '</text>';
    x += 30 + Math.min(22, String(d.label || '').length) * 7;
  });
  return out;
}
function barSVG(s){
  const n = s.labels.length, nd = s.datasets.length, W = 640, L = 170, R = 120, rowH = 22 * nd + 12, top = nd > 1 ? 28 : 6;
  const H = top + rowH * n + 6, bw = W - L - R;
  const max = Math.max(1, ...s.datasets.flatMap(d => d.data));
  let out = legend(s, W);
  s.labels.forEach((lb, i) => {
    const y0 = top + i * rowH;
    out += '<text x="' + (L - 8) + '" y="' + (y0 + (nd * 22) / 2 + 3) + '" font-size="14" text-anchor="end" fill="currentColor">' + esc(cut(lb, 26)) + '</text>';
    s.datasets.forEach((d, j) => {
      const v = d.data[i], w = Math.max(2, (Math.max(v, 0) / max) * bw), y = y0 + j * 22;
      out += '<rect x="' + L + '" y="' + y + '" width="' + w + '" height="17" rx="3" fill="' + PAL[j] + '"/>';
      out += '<text x="' + (L + w + 6) + '" y="' + (y + 13) + '" font-size="13" fill="currentColor">' + esc(fmtVal(v, s)) + '</text>';
    });
  });
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="' + esc(s.title || 'Grafik') + '" style="color:var(--ink)">' + out + '</svg>';
}
function niceMax(v){ const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1)))); const f = v / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; }
function lineSVG(s){
  const n = s.labels.length, W = 640, H = 290, l = 84, r = 48, t = s.datasets.length > 1 ? 34 : 16, b = 56;
  const max = niceMax(Math.max(1, ...s.datasets.flatMap(d => d.data))), pw = W - l - r, ph = H - t - b;
  const X = i => l + (n === 1 ? pw / 2 : (i / (n - 1)) * pw), Y = v => t + ph - (v / max) * ph;
  let out = legend(s, W);
  for (let k = 0; k <= 4; k++) {
    const v = (max / 4) * k, y = Y(v);
    out += '<line x1="' + l + '" x2="' + (W - r) + '" y1="' + y + '" y2="' + y + '" stroke="var(--line)"/>';
    out += '<text x="' + (l - 8) + '" y="' + (y + 4) + '" font-size="12" text-anchor="end" fill="currentColor">' + esc(fmtVal(v, s)) + '</text>';
  }
  s.labels.forEach((lb, i) => {
    if (n > 12 && i % 2) return;
    out += '<text x="' + X(i) + '" y="' + (H - b + 18) + '" font-size="12" text-anchor="middle" fill="currentColor">' + esc(cut(lb, 12)) + '</text>';
  });
  s.datasets.forEach((d, j) => {
    out += '<polyline fill="none" stroke="' + PAL[j] + '" stroke-width="2.5" points="' + d.data.map((v, i) => X(i) + ',' + Y(v)).join(' ') + '"/>';
    d.data.forEach((v, i) => {
      out += '<circle cx="' + X(i) + '" cy="' + Y(v) + '" r="4" fill="' + PAL[j] + '"/>';
      if (n <= 8 && s.datasets.length === 1) out += '<text x="' + X(i) + '" y="' + (Y(v) - 9) + '" font-size="12" text-anchor="' + (n > 1 && i === 0 ? 'start' : n > 1 && i === n - 1 ? 'end' : 'middle') + '" fill="currentColor">' + esc(fmtVal(v, s)) + '</text>';
    });
  });
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="' + esc(s.title || 'Grafik') + '" style="color:var(--ink)">' + out + '</svg>';
}
function chartEl(raw){
  const s = parseChart(raw), d = document.createElement('div'); d.className = 'chart';
  if (!s) { d.innerHTML = '<div class="ctitle">Grafik belum bisa ditampilkan.</div>'; return d; }
  d.innerHTML = (s.title ? '<div class="ctitle">' + esc(s.title) + '</div>' : '') + (s.type === 'line' ? lineSVG(s) : barSVG(s)) +
    (s.note ? '<div class="cnote">' + esc(s.note) + '</div>' : '');
  return d;
}

/* ---------- pesan bot: teks + tabel + grafik, diperbarui saat streaming ---------- */
function splitSegments(text){
  const segs = [], re = /```chart[ \t]*\r?\n([\s\S]*?)```/g; let last = 0, m;
  while ((m = re.exec(text))) { if (m.index > last) segs.push({t: 'text', raw: text.slice(last, m.index)}); segs.push({t: 'chart', raw: m[1]}); last = re.lastIndex; }
  let rest = text.slice(last);
  const open = rest.search(/```chart/);
  if (open >= 0) { if (open > 0) segs.push({t: 'text', raw: rest.slice(0, open)}); segs.push({t: 'pending', raw: ''}); }
  else if (rest) segs.push({t: 'text', raw: rest});
  return segs;
}
function paint(box, text){
  const segs = splitSegments(text), old = box._segs || [];
  segs.forEach((sg, i) => {
    const o = old[i];
    if (o && o.t === sg.t && o.raw === sg.raw) return;
    let el;
    if (sg.t === 'text') { el = (o && o.t === 'text') ? o.el : document.createElement('div'); el.className = 'seg'; el.innerHTML = renderText(sg.raw); }
    else if (sg.t === 'chart') el = chartEl(sg.raw);
    else { el = document.createElement('div'); el.className = 'typing'; el.textContent = 'Menyiapkan grafik…'; }
    if (o && o.el !== el) box.replaceChild(el, o.el); else if (!o) box.appendChild(el);
    sg.el = el;
  });
  segs.forEach((sg, i) => { if (!sg.el) sg.el = old[i].el; });
  for (let i = segs.length; i < old.length; i++) old[i].el.remove();
  box._segs = segs;
}
function add(role, text){
  const d = document.createElement('div'); d.className = 'msg ' + role;
  if (role === 'bot') paint(d, text); else d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
function greet(){
  add('bot', 'Halo! Saya Agent Kita, asisten TokoKita. Saya bisa membantu mencari dan membandingkan produk, membuat pesanan, mencatat pembayaran, dan mengecek status pesanan.');
  chips.innerHTML = '';
  ['Ada blender apa saja?', 'Bandingkan KitaMix 600 dan KitaMix 900 dengan grafik', 'Ada sepatu apa saja?', 'Buat grafik harga semua sepatu'].forEach(t => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = t;
    b.onclick = () => { input.value = t; submit(); }; chips.appendChild(b);
  });
}

/* ---------- kirim pesan dan baca stream (SSE) ---------- */
async function submit(){
  const text = input.value.trim(); if (!text || controller) return;
  input.value = ''; chips.innerHTML = ''; add('me', text); setBusy(true);
  const box = document.createElement('div'); box.className = 'msg bot'; log.appendChild(box);
  const status = document.createElement('div'); status.className = 'typing'; status.textContent = 'Sedang mengetik…'; box.appendChild(status);
  let acc = '', started = false, pending = false, failed = null, aborted = false;
  const redraw = () => { pending = false; if (!started) { status.remove(); started = true; } paint(box, acc); log.scrollTop = log.scrollHeight; };
  controller = new AbortController();
  try {
    const r = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}), signal: controller.signal, credentials: 'same-origin'});
    if (!r.ok) { const j = await r.json().catch(() => ({})); box.remove(); add('bot', j.error || 'Terjadi kesalahan.'); }
    else {
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '';
      for (;;) {
        const {value, done} = await reader.read(); if (done) break;
        buf += dec.decode(value, {stream: true});
        let k;
        while ((k = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
          if (!chunk.startsWith('data: ')) continue;   // ': ping' (heartbeat) diabaikan
          let ev; try { ev = JSON.parse(chunk.slice(6)); } catch (e) { continue; }
          if (ev.type === 'status' && !started) status.textContent = ev.text;
          else if (ev.type === 'delta') { acc += ev.text; if (!pending) { pending = true; requestAnimationFrame(redraw); } }
          else if (ev.type === 'error') failed = ev.message;
        }
      }
      if (failed && !acc) { box.remove(); add('bot', failed); }
      else { redraw(); if (!acc) { box.textContent = '(tidak ada balasan)'; } else if (failed) { acc += '\n\n_(jawaban terputus)_'; redraw(); } }
    }
  } catch (e) {
    if (e && e.name === 'AbortError') {
      aborted = true;
      if (acc) { acc += '\n\n_(dihentikan)_'; redraw(); } else { box.remove(); }
    } else { box.remove(); add('bot', 'Koneksi bermasalah. Coba lagi.'); }
  }
  controller = null; setBusy(false); input.focus();
}
function setBusy(b){ send.disabled = b; stopBtn.hidden = !b; }
document.getElementById('form').addEventListener('submit', e => { e.preventDefault(); submit(); });
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });
stopBtn.onclick = () => { if (controller) controller.abort(); };
document.getElementById('reset').onclick = async () => {
  if (controller) controller.abort();
  try { await fetch('/api/reset', {method: 'POST', credentials: 'same-origin'}); } catch (e) {}
  log.innerHTML = ''; greet();
};
greet();

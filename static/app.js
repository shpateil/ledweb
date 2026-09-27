/* app.js — логика панели. без сборщика, es2022, без внешних зависимостей. */
'use strict';

// ошибки не должны молча ломать панель — показываем тост и пишем в консоль
window.__errs = [];
window.addEventListener('error', (e) => {
  window.__errs.push(String(e.message || e.error));
});
window.addEventListener('unhandledrejection', (e) => {
  window.__errs.push('promise: ' + String(e.reason));
});

const $ = (s, r = document) => r.querySelector(s);

// обработчик, который не роняет весь скрипт на отсутствующем элементе.
// раньше было `on('#mic', ...)` и удаление карточки из
// разметки глушило init(): 0 сцен, 0 иконок, мёртвый цвет, настройки не
// открывались. on() вместо молчаливого краша — с явной записью в консоль
function on(sel, evt, fn) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) { console.warn('ledweb: нет элемента для', sel); return false; }
  el.addEventListener(evt, fn);
  return true;
}
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const ACCENTS = ['#3cec80', '#ff2e9a', '#ff5fd0', '#5c9dff', '#f5a524', '#f2f2f4'];

const S = {
  h: 0, s: 1, v: 1,          // hsv — источник правды для цвета
  state: null,              // последний снимок с сервера
  meta: null,
  palette: 'material',
  days: 0x7f,
  timer: null,              // локальный обратный отсчёт
  busy: false,
};

const hsv2rgb = (h, s, v) => {
  h = ((h % 360) + 360) % 360;
  const c = v * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = v - c;
  const t = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
          : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return t.map(u => Math.round((u + m) * 255));
};
const rgb2hsv = (r, g, b) => {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) {
    if (mx === r) h = 60 * (((g - b) / d) % 6);
    else if (mx === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  return { h: (h + 360) % 360, s: mx ? d / mx : 0, v: mx };
};
const hex2rgb = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
// всё, что пришло с сервера (имена пресетов, названия сцен, адреса), идёт
// в разметку через innerHTML. без экранирования имя вида <img onerror=...>
// выполняется. экранируем каждый раз перед вставкой
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// безопасная заливка: цвет приходит с сервера и может не быть валидным css
const safeColor = (hex) => (/^#[0-9a-f]{3,8}$/i.test(hex || '') ? hex : 'transparent');
const rgb2hex = (r, g, b) => '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join('');

let toastT = null;
function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('err', isErr);
  el.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { el.hidden = true; }, 2600);
}

/* ── сеть ──────────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `http ${res.status}`);
  return data;
}

let sendT = null;
function apply(action, payload, { quiet = false } = {}) {
  // оптимистично рисуем мгновенно, отправляем с дебаунсом
  paint(action, payload);
  clearTimeout(sendT);
  sendT = setTimeout(async () => {
    try {
      const snap = await api('/api/apply', { method: 'POST', body: { action, ...payload } });
      ingest(snap);
      if (!quiet) $('#latency').textContent = snap.last_command_ms != null ? `${snap.last_command_ms} мс` : '';
    } catch (e) {
      toast(e.message, true);
    }
  }, 40);
}

function paint(action, p) {
  if (action === 'color') {
    S.h = p.h ?? S.h; S.s = p.s ?? S.s; S.v = p.v ?? S.v;
  }
  render();
}

/* ── снимок состояния ──────────────────────────────────────────── */
// метка последнего снимка. нужна клиентскому сторожу ниже и в ingest() выше
var lastSnapAt = Date.now();

function ingest(snap) {
  lastSnapAt = Date.now();
  if (!snap) return;
  const wasOff = S.state && !S.state.power;
  S.state = snap;
  if (snap.color && (snap.color.hex !== lastSentHex || wasOff)) {
    const [r, g, b] = hex2rgb(snap.color.hex);
    const hsv = rgb2hsv(r, g, b);
    S.h = hsv.h; S.s = hsv.s; S.v = Math.max(hsv.v, .02);
  }
  const dot = $('#dot');
  dot.className = 'dot ' + (snap.last_error ? 'err' : snap.connected ? (snap.power ? 'on' : 'off') : 'err');
  renderLinkWarn(snap);
  $('#devname').textContent = snap.name || 'лента';
  $('#s-mac').textContent = snap.mac || '';
  $('#effect-now').textContent = snap.effect_name || '—';
  render();
}

// при обрыве связи панель обязана об этом говорить. раньше висел только
// индикатор-точка, и демон с connected:false выглядел как живой
function renderLinkWarn(snap) {
  const box = $('#link-warn');
  if (!box) return;
  const txt = $('#link-warn-text');
  if (snap.connected) {
    if (!box.hidden) {
      box.hidden = true;
      toast('связь восстановлена');
    }
    return;
  }
  if (txt) txt.textContent = snap.last_error
    ? 'лента не на связи: ' + snap.last_error
    : 'лента не на связи, ждём подключения';
  box.hidden = false;
}

let lastSentHex = null;

function render() {
  const [r, g, b] = hsv2rgb(S.h, S.s, S.v);
  const hex = rgb2hex(r, g, b);
  lastSentHex = hex;
  document.documentElement.style.setProperty('--led-rgb', `${r} ${g} ${b}`);
  $('#swatch-fill').style.background = `rgb(${r} ${g} ${b})`;
  $('#hex').textContent = hex;
  $('#rgb').textContent = `${r} ${g} ${b}`;
  $('#hue-cursor').style.left = `${(S.h / 360) * 100}%`;
  drawSV();

  if (S.state) {
    $('#power').setAttribute('aria-pressed', String(!!S.state.power));
    $('#power-label').textContent = S.state.power ? 'выключить' : 'включить';
    if (document.activeElement !== $('#bright')) $('#bright').value = S.state.brightness;
    $('#bval').textContent = S.state.brightness;
    if (document.activeElement !== $('#speed')) $('#speed').value = S.state.effect_speed;
    $('#sval').textContent = S.state.effect_speed;
      $$('.eff').forEach(el => el.classList.toggle('on', +el.dataset.v === S.state.effect));
  }
}

/* ── sv-плашка ─────────────────────────────────────────────────── */
const svCanvas = $('#sv');
let svCtx = null;
function drawSV() {
  if (!svCtx) svCtx = svCanvas.getContext('2d');
  const w = svCanvas.clientWidth || 600, h = 220;
  if (svCanvas.width !== w) { svCanvas.width = w; svCanvas.height = h; }
  // базовый белый столб — рисуем в градиент от текущего hue
  const base = svCtx.createLinearGradient(0, 0, w, 0);
  base.addColorStop(0, '#fff');
  base.addColorStop(1, `hsl(${S.h} 100% 50%)`);
  svCtx.fillStyle = base;
  svCtx.fillRect(0, 0, w, h);
  const sh = svCtx.createLinearGradient(0, 0, 0, h);
  sh.addColorStop(0, 'rgba(0,0,0,0)');
  sh.addColorStop(1, '#000');
  svCtx.fillStyle = sh;
  svCtx.fillRect(0, 0, w, h);
  // курсор
  const cx = S.s * w, cy = (1 - S.v) * h;
  svCtx.beginPath();
  svCtx.arc(cx, cy, 8, 0, Math.PI * 2);
  svCtx.strokeStyle = S.v > .55 ? '#000' : '#fff';
  svCtx.lineWidth = 2.5;
  svCtx.stroke();
  svCtx.beginPath();
  svCtx.arc(cx, cy, 3, 0, Math.PI * 2);
  svCtx.fillStyle = `hsl(${S.h} 100% ${S.v * 100}%)`;
  svCtx.fill();
}

function pickSV(ev) {
  const rect = svCanvas.getBoundingClientRect();
  const x = Math.min(Math.max((ev.clientX - rect.left) / rect.width, 0), 1);
  const y = Math.min(Math.max((ev.clientY - rect.top) / rect.height, 0), 1);
  S.s = x; S.v = 1 - y;
  if (S.v < .02) S.v = .02;
  apply('color', { h: S.h, s: S.s, v: S.v });
}

let svDrag = false;
svCanvas.addEventListener('pointerdown', e => { svDrag = true; svCanvas.setPointerCapture(e.pointerId); pickSV(e); });
svCanvas.addEventListener('pointermove', e => { if (svDrag) pickSV(e); });
svCanvas.addEventListener('pointerup', e => { svDrag = false; svCanvas.releasePointerCapture(e.pointerId); });

const hueEl = $('#hue');
function pickHue(e) {
  const rect = hueEl.getBoundingClientRect();
  S.h = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1) * 360;
  apply('color', { h: S.h, s: S.s, v: S.v });
}
let hueDrag = false;
hueEl.addEventListener('pointerdown', e => { hueDrag = true; hueEl.setPointerCapture(e.pointerId); pickHue(e); });
hueEl.addEventListener('pointermove', e => { if (hueDrag) pickHue(e); });
hueEl.addEventListener('pointerup', e => { hueDrag = false; hueEl.releasePointerCapture(e.pointerId); });
hueEl.addEventListener('keydown', e => {
  const step = e.shiftKey ? 10 : 2;
  if (e.key === 'ArrowLeft') { S.h = (S.h - step + 360) % 360; apply('color', { h: S.h, s: S.s, v: S.v }); e.preventDefault(); }
  if (e.key === 'ArrowRight') { S.h = (S.h + step) % 360; apply('color', { h: S.h, s: S.s, v: S.v }); e.preventDefault(); }
});

/* колесо мыши меняет яркость только там, где это ожидаемо: над hsv-колесом
   и над плашкой состояния. раньше preventDefault висел на document, и
   страница вообще не прокручивалась мышью */
document.addEventListener('wheel', e => {
  if (!$('#sheet').hidden) return;
  if (e.target.closest('input, select, .sheet, .scroller, .rule, .fx-grid, .palette, .tabs')) return;
  if (!e.target.closest('#sv, .hero, .panel')) return;
  const cur = +(S.state?.brightness ?? 100);
  const next = Math.max(0, Math.min(100, cur - Math.sign(e.deltaY) * 5));
  e.preventDefault();
  apply('brightness', { value: next });
}, { passive: false });

/* ── вкл/выкл ──────────────────────────────────────────────────── */
on('#power', 'click', () => {
  const on = !S.state?.power;
  apply('power', { on });
});

/* ── слайдеры ──────────────────────────────────────────────────── */
const live = (el, fn) => {
  let t = null;
  el.addEventListener('input', () => {
    fn(+el.value);
    clearTimeout(t);
    t = setTimeout(() => fn(+el.value, true), 180);
  });
};
live($('#bright'), (v, commit) => { if (commit) apply('brightness', { value: v }, { quiet: true }); else { S.state && (S.state.brightness = v); render(); } });
live($('#speed'),  (v, commit) => { if (commit) apply('speed', { value: v }, { quiet: true }); else { S.state && (S.state.effect_speed = v); render(); } });

/* ── сцены ─────────────────────────────────────────────────────── */
function renderScenes() {
  const box = $('#scenes');
  box.innerHTML = '';
  (S.meta?.scenes || []).forEach(sc => {
    const hex = sc.color;
    const b = document.createElement('button');
    b.className = 'scene';
    b.innerHTML = `<div class="row">
        <span class="leddot" style="background:${safeColor(hex)}"></span>
        <i data-lucide="${esc(sc.icon)}"></i></div>
      <b>${esc(sc.name)}</b>
      <span class="meta">${esc(sc.brightness)}%${sc.effect ? ' · эффект' : ''}</span>`;
    b.addEventListener('click', async () => {
      try { ingest(await api('/api/scene', { method: 'POST', body: { id: sc.id } })); toast(`сцена: ${sc.name}`); }
      catch (e) { toast(e.message, true); }
    });
    box.appendChild(b);
  });
  refreshIcons();
}

/* ── палитры ────────────────────────────────────────────────────── */
let paletteToken = 0;   // защита от гонки: поздний await не должен дописывать
                        // пресеты в уже перерисованную вкладку
async function renderPalette() {
  const mine = ++paletteToken;
  const box = $('#palette');
  const tab = S.palette;
  box.innerHTML = '';
  if (tab === 'saved') {
    const presets = await api('/api/presets').catch(() => []);
    if (mine !== paletteToken) return;      // вкладку уже переключили
    presets.forEach(p => {
      const b = document.createElement('button');
      b.className = 'pal';
      b.innerHTML = `<i style="background:${safeColor(p.hex)}"></i><span>${esc(p.name)}</span>`;
      b.addEventListener('click', () => setHex(p.hex));
      if (!p.builtin) {
        b.addEventListener('contextmenu', e => {
          e.preventDefault();
          api('/api/presets?index=' + presets.indexOf(p), { method: 'DELETE' })
            .then(renderPalette).catch(() => {});
        });
      }
      box.appendChild(b);
    });
    if (!presets.length) box.innerHTML = '<p class="empty">пока пусто</p>';
    return;
  }
  const pal = S.meta?.palettes?.[S.palette] || [];
  pal.forEach(item => {
    const b = document.createElement('button');
    b.className = 'pal';
    b.innerHTML = `<i style="background:${safeColor(item.hex)}"></i><span>${esc(item.name)}</span>`;
    b.addEventListener('click', () => setHex(item.hex));
    b.addEventListener('contextmenu', e => {   // долгий/правый клик — сохранить
      e.preventDefault();
      // hex2rgb возвращает объект {r,g,b}, а не массив пар: спредить его
      // в fromEntries нельзя, оттуда и падение «Iterator value 0 is not an entry»
      api('/api/presets', { method: 'POST', body: { name: item.name, ...hex2rgb(item.hex), brightness: S.state?.brightness ?? 100 } })
        .then(() => { toast(`сохранил: ${item.name}`); if (S.palette === 'saved') renderPalette(); })
        .catch(err => toast(err.message, true));
    });
    box.appendChild(b);
  });
  if (!pal.length) box.innerHTML = '<p class="empty">палитра пуста</p>';
}

$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.toggle('on', x === t));
  S.palette = t.dataset.pal;
  renderPalette();
}));

function setHex(hex) {
  const [r, g, b] = hex2rgb(hex);
  const hsv = rgb2hsv(r, g, b);
  S.h = hsv.h; S.s = hsv.s; S.v = Math.max(hsv.v, .02);
  apply('color', { h: S.h, s: S.s, v: S.v });
}

/* сохранить текущий цвет в «мои» */
on('#swatch', 'contextmenu', e => {
  e.preventDefault();
  // hsv2rgb отдаёт массив [r,g,b], hex2rgb — объект. не путать: в коде выше
  // стоит распаковка массива, здесь нужен объект по именам
  const [r, g, b] = hsv2rgb(S.h, S.s, S.v);
  const name = '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join('');
  api('/api/presets', { method: 'POST', body: { name, r, g, b, brightness: S.state?.brightness ?? 100 } })
    .then(() => { toast('сохранил в мои'); if (S.palette === 'saved') renderPalette(); })
    .catch(err => toast(err.message, true));
});

/* ── эффекты ───────────────────────────────────────────────────── */
function renderEffects() {
  const box = $('#effects');
  box.innerHTML = '';
  (S.meta?.effects || []).forEach(e => {
    const b = document.createElement('button');
    b.className = 'eff';
    b.dataset.v = e.value;
    b.innerHTML = `${esc(e.name)}<span class="code">0x${e.value.toString(16)}</span>`;
    b.addEventListener('click', () => apply('effect', { value: e.value }));
    box.appendChild(b);
  });
}

/* ── расписание ────────────────────────────────────────────────── */
function renderDays() {
  const box = $('#s-days');
  box.innerHTML = '';
  const names = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  names.forEach((n, i) => {
    const bit = 1 << i;
    const b = document.createElement('button');
    b.className = 'day' + (S.days & bit ? ' on' : '');
    b.textContent = n;
    b.addEventListener('click', () => { S.days ^= bit; renderDays(); });
    box.appendChild(b);
  });
}

async function renderRules() {
  const box = $('#rules');
  const rules = await api('/api/rules').catch(() => []);
  box.innerHTML = '';
  if (!rules.length) { box.innerHTML = '<p class="empty">правил нет</p>'; return; }
  const dayNames = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  rules.forEach(rule => {
    const a = rule.action || {};
    let what = '';
    if (a.kind === 'color') what = `цвет ${a.hex || ''}`;
    else if (a.kind === 'brightness') what = `яркость ${a.value}`;
    else if (a.kind === 'power') what = a.on ? 'включить' : 'выключить';
    else if (a.kind === 'scene') what = `сцена ${a.name || a.id}`;
    else if (a.kind === 'effect') what = `эффект ${a.name || a.value}`;
    const days = [...rule.days.toString(2).padStart(7, '0')].reverse()
      .map((bit, i) => bit === '1' ? dayNames[i] : null).filter(Boolean).join(' ');
    const el = document.createElement('div');
    el.className = 'rule';
    el.innerHTML = `<time>${esc(rule.time)}</time>
      <span class="what">${esc(what)}${days ? ' · ' + esc(days) : ''}</span>
      <button class="kill"><i data-lucide="trash-2"></i></button>`;
    $('.kill', el).addEventListener('click', () =>
      api('/api/rules?id=' + rule.id, { method: 'DELETE' }).then(renderRules));
    box.appendChild(el);
  });
  refreshIcons();
}

on('#s-on', 'change', () => {
  const kind = $('#s-on').value;
  $('#s-color').classList.toggle('hidden', kind !== 'color');
  $('#s-val').classList.toggle('hidden', kind !== 'brightness');
  $('#s-scene').classList.toggle('hidden', kind !== 'scene');
  $('#s-effect').classList.toggle('hidden', kind !== 'effect');
});

on('#s-add', 'click', async () => {
  const kind = $('#s-on').value;
  const action = { kind };
  if (kind === 'color') { action.hex = $('#s-color').value; action.rgb = hex2rgb(action.hex); }
  if (kind === 'brightness') action.value = +$('#s-val').value;
  if (kind === 'power') action.on = true;
  if (kind === 'scene') { action.id = $('#s-scene').value; action.name = $('#s-scene').selectedOptions[0]?.textContent; }
  if (kind === 'effect') { action.value = +$('#s-effect').value; action.name = $('#s-effect').selectedOptions[0]?.textContent; }
  if (!S.days) { toast('выбери дни', true); return; }
  try {
    await api('/api/rules', { method: 'POST', body: { time: $('#s-time').value, action, days: S.days } });
    toast('правило добавлено');
    renderRules();
  } catch (e) { toast(e.message, true); }
});

/* ── софт-эффекты (считает демон) ──────────────────────────────── */
function renderFx() {
  const box = $('#fx-grid');
  if (!box) return;
  box.innerHTML = '';
  const cur = S.meta?.fx_state?.name || 'static';
  const on = S.meta?.fx_state?.running === true;   // без ?. был TypeError
  (S.meta?.fx || []).forEach(f => {
    const b = document.createElement('button');
    b.className = 'eff fx' + (cur === f.key && on ? ' on' : '');
    b.innerHTML = `${esc(f.name)}<span class="code">${f.fps ? f.fps + ' к/с' : 'без цикла'}</span>`;
    b.addEventListener('click', () => {
      const speed = +$('#fx-speed').value / 100;
      if (f.key === 'static') {
        api('/api/fx/stop', { method: 'POST' })
          .then(() => { toast('эффекты выключены'); refreshMeta(); })
          .catch(e => toast(e.message, true));
        return;
      }
      api('/api/fx', { method: 'POST', body: { key: f.key, speed } })
        .then(() => { toast(`${f.name} запущен`); refreshMeta(); })
        .catch(e => toast(e.message, true));
    });
    box.appendChild(b);
  });
  const s = S.meta?.fx_state;
  if ($('#fx-now')) {
    $('#fx-now').textContent = s && s.running ? `${s.label} ${s.speed.toFixed(1)}x` : 'выключены';
  }
}

async function refreshMeta() {
  S.meta = await api('/api/meta').catch(() => S.meta);
  renderFx();
}

on('#fx-speed', 'input', (e) => {
  $('#fx-speed-val').textContent = (e.target.value / 100).toFixed(1);
});
on('#fx-speed', 'change', async (e) => {
  const s = S.meta?.fx_state;
  if (!s?.running) return;
  // скорость у движка задаётся при старте, поэтому перезапускаем эффект
  const key = s.name;
  await api('/api/fx', { method: 'POST', body: { key, speed: e.target.value / 100 } })
    .then(() => refreshMeta())
    .catch(err => toast(err.message, true));
});

/* ── таймер (живёт в демоне) ────────────────────────────────────── */
on('#t-on', 'click', async () => {
  const min = +$('#t-min').value;
  if (!(min > 0)) { toast('введи минуты', true); return; }
  try {
    const t = await api('/api/timer', { method: 'POST', body: { minutes: min } });
    S.timer = Date.now() + t.left * 1000;
    $('#t-off').hidden = false;
    tick();
    toast(`выключу через ${min} мин`);
  } catch (e) { toast(e.message, true); }
});

on('#t-off', 'click', async () => {
  try {
    await api('/api/timer', { method: 'DELETE' });
    S.timer = null;
    $('#t-off').hidden = true;
    $('#t-left').textContent = '';
    toast('таймер снят');
  } catch (e) { toast(e.message, true); }
});

function tick() {
  if (!S.timer) { $('#t-left').textContent = ''; return; }
  const left = S.timer - Date.now();
  if (left <= 0) {
    S.timer = null;
    $('#t-left').textContent = 'таймер сработал';
    $('#t-off').hidden = true;
    return;
  }
  const m = Math.floor(left / 60000), s = Math.floor((left % 60000) / 1000);
  $('#t-left').textContent = `осталось ${m}:${String(s).padStart(2, '0')}`;
  setTimeout(tick, 1000);
}

/* ── настройки ─────────────────────────────────────────────────── */
function openSheet(open) {
  $('#sheet').hidden = !open;
  $('#scrim').hidden = !open;
}
on('#btn-reconnect', 'click', async () => {
  const b = $('#btn-reconnect');
  if (b) { b.disabled = true; b.textContent = 'подключаю...'; }
  try {
    const snap = await api('/api/reconnect', { method: 'POST' });
    toast('переподключаюсь');
    if (snap) ingest(snap);
  } catch (e) {
    toast(e.message, true);
  } finally {
    // текст вернёт renderLinkWarn, когда придёт свежий снимок состояния
    setTimeout(() => { if (b) { b.disabled = false; } }, 2500);
  }
});

on('#btn-settings', 'click', () => openSheet(true));
on('#sheet-close', 'click', () => openSheet(false));
on('#scrim', 'click', () => openSheet(false));

$('#accent-picks').innerHTML = ACCENTS.map(c =>
  `<button class="acc-pick" data-c="${c}" style="background:${c}" title="${c}"></button>`).join('');
on('#accent-picks', 'click', e => {
  const b = e.target.closest('.acc-pick');
  if (!b) return;
  const c = b.dataset.c;
  document.documentElement.style.setProperty('--ac', c);
  localStorage.setItem('ledweb.accent', c);
  $$('.acc-pick').forEach(x => x.classList.toggle('on', x === b));
});

on('#btn-scan', 'click', async () => {
  const box = $('#scan-list');
  box.innerHTML = '<p class="muted tiny">ищу…</p>';
  try {
    const { devices } = await api('/api/scan');
    box.innerHTML = '';
    if (!devices.length) { box.innerHTML = '<p class="muted tiny">ничего не нашлось</p>'; return; }
    devices.forEach(d => {
      const el = document.createElement('div');
      el.className = 'scan-item';
      el.innerHTML = `<span class="mono">${esc(d.address)}</span><span class="muted">${esc(d.name || '')}</span>`;
      const b = document.createElement('button');
      b.className = 'btn';
      b.textContent = 'выбрать';
      b.addEventListener('click', () => {
        $('#s-mac').textContent = d.address;
        api('/api/settings', { method: 'POST', body: { mac: d.address } })
          .then(() => toast('сохранено, перезапусти сервис')).catch(e => toast(e.message, true));
      });
      el.appendChild(b);
      box.appendChild(el);
    });
  } catch (e) { box.innerHTML = `<p class="muted tiny">${e.message}</p>`; }
});

on('#s-variant', 'change', e =>
  api('/api/settings', { method: 'POST', body: { variant: e.target.value } })
    .then(() => toast('сохранено, перезапусти сервис')).catch(err => toast(err.message, true)));

$$('[data-test]').forEach(b => b.addEventListener('click', async () => {
  const t = b.dataset.test;
  const body = t === 'on' ? { action: 'power', on: true }
    : t === 'off' ? { action: 'power', on: false }
    : { action: 'schedule', on: true, hour: 0, minute: 0, days: 0x7f };
  try { ingest(await api('/api/apply', { method: 'POST', body })); toast('отправлено'); }
  catch (e) { toast(e.message, true); }
}));

/* ── горячие клавиши ───────────────────────────────────────────── */
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea')) return;
  const st = S.state;
  const k = e.key;

  if (k === ' ') { e.preventDefault(); apply('power', { on: !st?.power }); return; }
  if (k >= '1' && k <= '9') {
    const idx = +k - 1;
    api('/api/presets').then(ps => { if (ps[idx]) setHex(ps[idx].hex); });
    return;
  }
  if (k === '[' || k === ']') {
    const cur = st?.brightness ?? 100;
    apply('brightness', { value: Math.max(0, Math.min(100, cur + (k === ']' ? 5 : -5))) });
    return;
  }
  if (k === 'ArrowLeft' || k === 'ArrowRight') {
    e.preventDefault();
    const step = e.shiftKey ? 10 : 3;
    if (e.shiftKey) S.s = Math.max(0, Math.min(1, S.s + (k === 'ArrowRight' ? step : -step) / 100));
    else S.h = (S.h + (k === 'ArrowRight' ? step : -step) + 360) % 360;
    apply('color', { h: S.h, s: S.s, v: S.v });
    return;
  }
  if (k === 'ArrowUp' || k === 'ArrowDown') {
    e.preventDefault();
    const cur = st?.brightness ?? 100;
    apply('brightness', { value: Math.max(0, Math.min(100, cur + (k === 'ArrowUp' ? 5 : -5))) });
    return;
  }
  if (k === 'e') { $('#effects').scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
  if (k === 's') { $('#scenes').scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
  if (k === 't') { $('#t-min').focus(); toast('введи минуты и жми «запустить»'); return; }
  if (k === '?') { openSheet($('#sheet').hidden); return; }
  if (k === 'Escape') { openSheet(false); }
});

/* ── lucide ────────────────────────────────────────────────────── */
function refreshIcons() {
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

/* ── sse ───────────────────────────────────────────────────────── */
function connectStream() {
  const es = new EventSource('/api/stream');
  es.addEventListener('state', ev => { try { ingest(JSON.parse(ev.data)); } catch {} });
  es.addEventListener('fx', () => refreshMeta());
  es.addEventListener('timer', ev => {
    try {
      const t = JSON.parse(ev.data);
      if (t.armed) {
        S.timer = Date.now() + t.left * 1000;
        $('#t-off').hidden = false;
        tick();
      } else {
        // условие проверялось ПОСЛЕ обнуления S.timer, поэтому было всегда
        // истинным и «таймер сработал» всплывало при любом снятии таймера
        const had = S.timer !== null;
        S.timer = null;
        $('#t-off').hidden = true;
        $('#t-left').textContent = (had && t.left === 0) ? 'таймер сработал' : '';
      }
    } catch {}
  });
  es.onerror = () => { es.close(); setTimeout(connectStream, 2500); };
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) es.close(); else connectStream();
  });
}

/* страховка на клиенте: если по sse давно не было снимка — спросим api
   напрямую. иначе панель залипнет с зелёной точкой, когда лента отвалилась,
   а демон молчит. раньше было ровно это: демон висел в dbus, фронт получал
   снимок раз в сутки и показывал «всё ок» */
setInterval(async () => {
  if (Date.now() - lastSnapAt < 15000) return;
  try {
    const snap = await api('/api/state');
    lastSnapAt = Date.now();
    ingest(snap);
  } catch {
    lastSnapAt = Date.now();   // демон целиком мёртв, sse сам переподключится
  }
}, 5000);

/* ── старт ─────────────────────────────────────────────────────── */
(async function init() {
  const savedAccent = localStorage.getItem('ledweb.accent');
  if (savedAccent) document.documentElement.style.setProperty('--ac', savedAccent);

  try {
    S.meta = await api('/api/meta');
    $('#s-variant').innerHTML = S.meta.variants.map(v =>
      `<option value="${esc(v)}"${v === 'generic' ? ' selected' : ''}>${esc(v)}</option>`).join('');
    $('#s-scene').innerHTML = S.meta.scenes.map(s =>
      `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
    $('#s-effect').innerHTML = S.meta.effects.map(e =>
      `<option value="${esc(e.value)}">${esc(e.name)}</option>`).join('');
    renderScenes();
    renderEffects();
    renderFx();
    renderDays();
  } catch (e) { toast('сервер не отвечает: ' + e.message, true); }

  renderPalette();
  renderRules();
  // подхватываем таймер демона: он мог остаться от прошлой сессии
  api('/api/timer').then(t => {
    if (t.armed) {
      S.timer = Date.now() + t.left * 1000;
      $('#t-off').hidden = false;
      tick();
    }
  }).catch(() => {});
  try { ingest(await api('/api/state')); } catch {}
  connectStream();
  refreshIcons();
  setTimeout(refreshIcons, 300);
  window.addEventListener('resize', drawSV);
  tick();
})();

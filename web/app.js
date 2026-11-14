// Interface d'ModelForge : le chat avec l'agent (à gauche) et les commandes de
// la visionneuse 3D (à droite).

import { Viewer } from '/viewer.js';

const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const viewer = new Viewer($('viewport'));
window.viewer = viewer;   // pratique pour déboguer depuis la console
const state = { pid: null, ws: null, busy: false, live: null, activity: null, shownVersion: null };

// ---- mise en forme -------------------------------------------------------

const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const inline = (s) => escapeHtml(s)
  .replace(/`([^`]+)`/g, '<code>$1</code>')
  .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');

// Markdown minimal : paragraphes, titres, listes, gras, code en ligne.
function markdown(text) {
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^#{1,6}\s+(.*)/))) {
      close(); out.push(`<h4>${inline(m[1])}</h4>`);
    } else if (line.trim()) {
      close(); out.push(`<p>${inline(line)}</p>`);
    } else {
      close();
    }
  }
  close();
  return out.join('');
}

function length(m) {
  const fr = (v, d) => v.toLocaleString('fr-FR', { maximumFractionDigits: d });
  if (m >= 100) return `${fr(m, 0)} m`;
  if (m >= 1) return `${fr(m, 2)} m`;
  if (m >= 0.01) return `${fr(m * 100, 1)} cm`;
  return `${fr(m * 1000, 1)} mm`;
}

// ---- messages --------------------------------------------------------------

function push(node) {
  $('welcome').hidden = true;
  const box = $('messages');
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.appendChild(node);
  if (stick) box.scrollTop = box.scrollHeight;
  return node;
}

function scrollDown() {
  const box = $('messages');
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
}

function settleActivity(ok = true) {
  if (state.activity) {
    state.activity.classList.add(ok ? 'done' : 'failed');
    state.activity = null;
  }
}

// Les questions en attente restent le dernier élément du fil, même si
// l'agent ajoute une phrase après les avoir posées.
function keepQuestionsLast() {
  const open = document.querySelector('.questions:not(.answered)');
  if (open && open.nextElementSibling) { $('messages').appendChild(open); scrollDown(); }
}

function startActivity(label) {
  state.activity = el('div', 'activity');
  state.activity.append(el('span', 'dot'), el('span', '', label));
  return push(state.activity);
}

function dropPassing() {
  if (state.activity?.dataset.passing) { state.activity.remove(); state.activity = null; }
}

function lockQuestions() {
  document.querySelectorAll('.questions:not(.answered)').forEach((q) => q.classList.add('answered'));
}

function showQuestions(questions, live) {
  const card = el('div', 'questions');
  const fields = questions.map((q) => {
    const block = el('div');
    block.appendChild(el('div', 'q-title', q.question));
    const opts = el('div', 'opts');
    for (const option of q.options || []) {
      const button = el('button', 'opt', option);
      button.type = 'button';
      button.onclick = () => {
        if (!q.multiple) opts.querySelectorAll('.on').forEach((b) => b !== button && b.classList.remove('on'));
        button.classList.toggle('on');
      };
      opts.appendChild(button);
    }
    block.appendChild(opts);
    const free = el('input');
    free.type = 'text';
    free.placeholder = 'Autre réponse ou précision…';
    block.appendChild(free);
    card.appendChild(block);
    return { q, opts, free };
  });
  const submit = el('button', 'submit', 'Envoyer mes réponses');
  submit.type = 'button';
  submit.onclick = () => {
    const lines = [];
    for (const { q, opts, free } of fields) {
      const picked = [...opts.querySelectorAll('.on')].map((b) => b.textContent);
      if (free.value.trim()) picked.push(free.value.trim());
      lines.push(`${q.question}\n→ ${picked.length ? picked.join(' ; ') : 'Fais au mieux.'}`);
    }
    send(lines.join('\n\n'));
  };
  card.appendChild(submit);
  if (!live) card.classList.add('answered');
  push(card);
}

function render(event, live = true) {
  switch (event.type) {
    case 'user':
      settleActivity();
      lockQuestions();
      state.live = null;
      {
        const bubble = el('div', 'msg user');
        if (event.piece) bubble.appendChild(el('span', 'piece', `Pièce désignée : ${event.piece}`));
        if (event.images?.length) {
          const refs = el('div', 'refs');
          for (const name of event.images) {
            const img = el('img');
            img.src = `/files/${state.pid}/refs/${name}`;
            img.alt = 'Image de référence';
            refs.appendChild(img);
          }
          bubble.appendChild(refs);
        }
        bubble.appendChild(document.createTextNode(event.text));
        push(bubble);
      }
      break;
    case 'text_delta':
      dropPassing();
      settleActivity();
      if (!state.live) { state.live = push(el('div', 'msg bot')); state.liveText = ''; }
      state.liveText += event.text;
      state.live.innerHTML = markdown(state.liveText);
      scrollDown();
      break;
    case 'text': {
      dropPassing();
      settleActivity();
      const node = state.live || push(el('div', 'msg bot'));
      node.innerHTML = markdown(event.text);
      state.live = null;
      scrollDown();
      break;
    }
    case 'working':
      // Activité passagère (réflexion, écriture du code) : une seule ligne,
      // remplacée par l'étape suivante.
      state.live = null;
      if (state.activity?.dataset.passing) state.activity.lastChild.textContent = event.label;
      else { settleActivity(); startActivity(event.label).dataset.passing = '1'; }
      break;
    case 'tool_start':
      state.live = null;
      if (state.activity?.dataset.passing) { state.activity.remove(); state.activity = null; }
      settleActivity();
      if (event.tool === 'poser_questions') break;
      startActivity(event.label);
      if (!live) settleActivity();
      break;
    case 'tool_end':
      settleActivity(event.ok);
      break;
    case 'questions':
      settleActivity();
      showQuestions(event.questions, live);
      break;
    case 'model':
      settleActivity();
      if (live) {
        stopPeek(false);
        showModel(event.report, event.version);
        refreshHistory();
        if (event.previews?.length) {
          const strip = el('div', 'thumbs');
          for (const preview of event.previews) {
            const img = el('img');
            img.src = `/files/${state.pid}/previews/${preview.file}?v=${event.version}`;
            img.title = preview.label;
            img.alt = preview.label;
            strip.appendChild(img);
          }
          push(strip);
        }
      }
      break;
    case 'photo': {
      const link = el('a', 'photo');
      link.href = `/files/${state.pid}/photos/${event.file}`;
      link.target = '_blank';
      link.title = 'Ouvrir en grand';
      const img = el('img');
      img.src = link.href;
      img.alt = 'Rendu réaliste du modèle';
      img.onload = scrollDown;
      link.append(img, el('span', '', 'Rendu réaliste · clique pour l’ouvrir en grand'));
      push(link);
      break;
    }
    case 'restored':
      settleActivity();
      push(el('div', 'note', `Retour à la version « ${event.label} ».`));
      if (live) { stopPeek(false); showModel(event.report, event.version); refreshHistory(); }
      break;
    case 'title':
      refreshProjects();
      break;
    case 'effort':
      paintEffort(event.value);
      break;
    case 'open_project':
      refreshProjects().then(() => openProject(event.id));
      break;
    case 'error':
      settleActivity(false);
      push(el('div', 'error', event.message));
      break;
    case 'busy':
      setBusy(event.value);
      break;
    case 'done':
      dropPassing();
      settleActivity();
      state.live = null;
      keepQuestionsLast();
      break;
  }
}

function setBusy(value) {
  state.busy = value;
  $('send').hidden = value;
  $('stop').hidden = !value;
  if (!value) { dropPassing(); settleActivity(); }
}

// ---- modèle ----------------------------------------------------------------

async function showModel(report, version) {
  if (!report || !version || version === state.shownVersion) return;
  state.shownVersion = version;
  state.report = report;
  state.currentVersion = version;
  const pid = state.pid;
  try {
    await viewer.load(`/files/${pid}/model.glb?v=${version}`);
  } catch (error) {
    push(el('div', 'error', `Impossible d'afficher le modèle : ${error.message}`));
    return;
  }
  if (pid !== state.pid) return;
  $('empty').hidden = true;
  $('side').hidden = false;
  $('help').hidden = false;
  $('download').hidden = false;
  $('photo').hidden = false;
  $('history-button').hidden = false;
  $('download').href = `/files/${pid}/model.glb?v=${version}`;
  $('download-stl').hidden = false;
  $('download-stl').href = `/api/projects/${pid}/stl`;
  $('download').download = 'modele.glb';

  const s = report.size;
  $('dims').hidden = false;
  $('dims').innerHTML =
    `<span>Longueur</span> <b>${length(s.longueur_x)}</b> · <span>Largeur</span> <b>${length(s.largeur_y)}</b> · ` +
    `<span>Hauteur</span> <b>${length(s.hauteur_z)}</b>`;

  const box = $('viewpoints');
  box.replaceChildren();
  for (const viewpoint of report.viewpoints || []) {
    if (!viewpoint.direction) continue;
    const button = el('button', '', viewpoint.name);
    button.onclick = () => viewer.goTo(viewpoint);
    box.appendChild(button);
  }
  $('viewpoints-box').hidden = !box.children.length;
}

function resetStage() {
  state.shownVersion = null;
  if (viewer.model) { viewer.scene.remove(viewer.model); viewer.model = null; }
  viewer.ground.visible = false;
  Object.assign(viewer, { mixer: null, duration: 0, playing: false, time: 0, animated: [] });
  for (const id of ['side', 'help', 'download', 'download-stl', 'player', 'photo', 'dims', 'history-button', 'history', 'peek']) $(id).hidden = true;
  peek.id = null;
  state.report = null;
  viewer.select(null);
  viewer.pieces = [];
  $('explode').value = 0;
  setExplode();
  $('empty').hidden = false;
}

const HELP = {
  orbit: 'Glisse pour tourner · molette pour zoomer · clic droit pour déplacer · <kbd>←</kbd><kbd>↑</kbd><kbd>↓</kbd><kbd>→</kbd> pour changer d’angle',
  visit: '<kbd>Z</kbd><kbd>Q</kbd><kbd>S</kbd><kbd>D</kbd> avancer et se décaler · glisse ou <kbd>←</kbd><kbd>→</kbd> pour tourner la tête · <kbd>Espace</kbd>/<kbd>C</kbd> monter/descendre · <kbd>Maj</kbd> plus vite · <kbd>Échap</kbd> pour ressortir',
};
viewer.onModeChange = (mode) => {
  document.querySelectorAll('#mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  $('help').innerHTML = HELP[mode];
};
$('help').innerHTML = HELP.orbit;

$('mode').onclick = (e) => e.target.dataset.mode && viewer.setMode(e.target.dataset.mode);
$('views').onclick = (e) => e.target.dataset.view && viewer.setView(e.target.dataset.view);
$('pad').onclick = (e) => e.target.dataset.dir && viewer.rotate(e.target.dataset.dir);

function updateCut() {
  const axis = document.querySelector('#cut-axis .on').dataset.axis;
  const value = Number($('cut').value);
  viewer.setCut(axis, value / 100);
  $('cut-value').textContent = value < 100 ? `${100 - value} % retiré` : '';
}
$('cut').oninput = updateCut;
$('cut-axis').onclick = (e) => {
  if (!e.target.dataset.axis) return;
  document.querySelectorAll('#cut-axis button').forEach((b) => b.classList.toggle('on', b === e.target));
  updateCut();
};

function setExplode() {
  const value = Number($('explode').value);
  viewer.setExplode(value / 100);
  $('explode-value').textContent = value ? `${value} %` : '';
}
$('explode').oninput = setExplode;

// ---- animation -------------------------------------------------------------

viewer.onTime = (time, duration, playing) => {
  $('player').hidden = !duration || !viewer.model;
  if (!duration) return;
  $('play').textContent = playing ? '⏸' : '▶';
  $('timeline').value = Math.round((time / duration) * 1000);
  const fr = (v) => v.toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  $('clock').textContent = `${fr(time)} / ${fr(duration)} s`;
};
$('play').onclick = () => viewer.play();
$('timeline').oninput = () => {
  const time = (Number($('timeline').value) / 1000) * viewer.duration;
  viewer.play(false);
  viewer.seek(time);
};

// ---- soin de l'agent -------------------------------------------------------

function paintEffort(value) {
  document.querySelectorAll('#effort button').forEach((b) => b.classList.toggle('on', b.dataset.effort === value));
}
$('effort').onclick = (e) => {
  const value = e.target.dataset.effort;
  if (!value) return;
  if (state.busy) { push(el('div', 'error', 'L’agent travaille : change ce réglage quand il a fini.')); return; }
  state.ws?.send(JSON.stringify({ type: 'effort', value }));
};

// ---- pièce désignée ------------------------------------------------------

let picked = null;   // pièce cliquée dans la visionneuse, jointe au prochain message

viewer.onSelect = (piece) => {
  picked = piece;
  $('picked').hidden = !piece;
  if (!piece) { input.placeholder = 'Décris ton modèle…'; return; }
  $('picked-name').textContent = piece.name;
  $('picked-size').textContent = `· ${piece.size.map(length).join(' × ')}`;
  input.placeholder = 'Que veux-tu changer sur cette pièce ?';
  input.focus();
};
$('picked-clear').onclick = () => viewer.select(null);

// ---- versions ------------------------------------------------------------

const peek = { id: null };   // ancienne version affichée à la place de l'actuelle

async function refreshHistory() {
  if ($('history').hidden || !state.pid) return;
  const pid = state.pid;
  let data;
  try { data = await api(`/api/projects/${pid}/versions`); } catch { return; }
  if (pid !== state.pid) return;
  const list = $('history-list');
  list.replaceChildren();
  if (!data.versions.length) {
    list.appendChild(el('div', 'history-empty', 'Aucune version pour l’instant.'));
    return;
  }
  const hour = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  data.versions.forEach((version, index) => {
    const row = el('button', version.id === data.current ? 'current' : '');
    row.type = 'button';
    if (version.id === peek.id) row.classList.add('peeking');
    const img = el('img');
    img.src = `/files/${pid}/versions/${version.id}/thumb.jpg`;
    img.alt = '';
    img.onerror = () => img.replaceWith(el('span', 'no-thumb'));
    const text = el('span');
    text.append(`${index + 1}. ${version.label}`);
    text.appendChild(el('small', '', `${hour.format(version.time * 1000)} · ${version.objects} pièces`
      + (version.id === data.current ? ' · version actuelle' : '')));
    row.append(img, text);
    row.onclick = () => (version.id === data.current ? stopPeek() : startPeek(version));
    list.prepend(row);   // la plus récente en haut
  });
}

async function startPeek(version) {
  const pid = state.pid;
  try {
    await viewer.load(`/files/${pid}/versions/${version.id}/model.glb`);
  } catch {
    push(el('div', 'error', 'Cette version ne peut plus être affichée.'));
    return;
  }
  if (pid !== state.pid) return;
  peek.id = version.id;
  state.shownVersion = null;   // l'actuelle devra être rechargée
  $('peek-label').textContent = version.label;
  $('peek').hidden = false;
  refreshHistory();
}

// Quitte l'aperçu d'une ancienne version ; reload : réafficher l'actuelle.
function stopPeek(reload = true) {
  if (!peek.id) return;
  peek.id = null;
  $('peek').hidden = true;
  state.shownVersion = null;
  if (reload && state.report) showModel(state.report, state.currentVersion);
  refreshHistory();
}

$('history-button').onclick = () => {
  $('history').hidden = !$('history').hidden;
  refreshHistory();
};
$('peek-cancel').onclick = () => stopPeek();
$('peek-restore').onclick = async () => {
  if (!peek.id) return;
  if (state.busy) { push(el('div', 'error', 'L’agent travaille : attends qu’il ait fini pour changer de version.')); return; }
  try {
    const response = await fetch(`/api/projects/${state.pid}/versions/${peek.id}/restore`, { method: 'POST' });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.status);
  } catch (error) {
    push(el('div', 'error', `Retour impossible : ${error.message}`));
  }
};

// ---- photo réaliste ------------------------------------------------------

$('photo').onclick = async () => {
  if (!state.pid || $('photo').disabled) return;
  const pid = state.pid;
  $('photo').disabled = true;
  $('photo').textContent = 'Rendu en cours…';
  try {
    const response = await fetch(`/api/projects/${pid}/photo`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(viewer.cameraForBlender()),
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.status);
  } catch (error) {
    if (pid === state.pid) push(el('div', 'error', `Le rendu réaliste a échoué : ${error.message}`));
  }
  $('photo').disabled = false;
  $('photo').textContent = 'Photo réaliste';
};

// ---- images de référence -------------------------------------------------

const attached = [];   // images prêtes à partir avec le prochain message (data URL JPEG)

function paintAttached() {
  const box = $('attached');
  box.replaceChildren();
  attached.forEach((data, index) => {
    const ref = el('div', 'ref');
    const img = el('img');
    img.src = data;
    img.alt = 'Image de référence';
    const remove = el('button', '', '×');
    remove.type = 'button';
    remove.title = 'Retirer cette image';
    remove.onclick = () => { attached.splice(index, 1); paintAttached(); };
    ref.append(img, remove);
    box.appendChild(ref);
  });
  box.hidden = !attached.length;
}

// Réduit l'image (1568 px au plus) et la convertit en JPEG : c'est assez pour
// que l'agent voie les formes et les couleurs, et le message reste léger.
async function attach(files) {
  for (const file of files) {
    if (!file.type.startsWith('image/') || attached.length >= 4) continue;
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, 1568 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      attached.push(canvas.toDataURL('image/jpeg', 0.88));
    } catch {
      push(el('div', 'error', `Image illisible : ${file.name}`));
    }
  }
  paintAttached();
}

$('attach').onclick = () => $('file').click();
$('file').onchange = (e) => { attach([...e.target.files]); e.target.value = ''; };
const chatPanel = document.querySelector('.chat');
chatPanel.addEventListener('paste', (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
  if (files.length) { e.preventDefault(); attach(files); }
});
chatPanel.addEventListener('dragover', (e) => { e.preventDefault(); chatPanel.classList.add('dropping'); });
chatPanel.addEventListener('dragleave', () => chatPanel.classList.remove('dropping'));
chatPanel.addEventListener('drop', (e) => {
  e.preventDefault();
  chatPanel.classList.remove('dropping');
  attach([...(e.dataTransfer?.files || [])]);
});

// ---- Blender en direct ---------------------------------------------------

const live = { on: false, waitingUntil: 0 };

function paintLive() {
  const waiting = !live.on && Date.now() < live.waitingUntil;
  $('live').classList.toggle('on', live.on);
  $('live').classList.toggle('waiting', waiting);
  $('live-label').textContent = live.on ? 'Blender en direct'
    : waiting ? 'Ouverture de Blender…' : 'Voir la fabrication dans Blender';
  $('live').title = live.on
    ? 'Blender est connecté : l’agent construit dedans. Clique pour y afficher ce projet.'
    : 'Ouvre Blender : l’agent y construira le modèle sous tes yeux';
}

async function pollLive() {
  try { live.on = (await api('/api/status')).live; } catch { live.on = false; }
  paintLive();
}
setInterval(pollLive, 3000);

$('live').onclick = async () => {
  if (!state.pid || (!live.on && Date.now() < live.waitingUntil)) return;
  try {
    const result = await api(`/api/projects/${state.pid}/live`, { method: 'POST' });
    if (result.launched) live.waitingUntil = Date.now() + 25000;
  } catch {
    push(el('div', 'error', 'Impossible d’ouvrir Blender.'));
  }
  paintLive();
};

// ---- projets et connexion --------------------------------------------------

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

async function refreshProjects() {
  const projects = await api('/api/projects');
  const select = $('projects');
  select.replaceChildren();
  for (const project of projects) {
    const option = el('option', '', project.title);
    option.value = project.id;
    select.appendChild(option);
  }
  select.value = state.pid || '';
  return projects;
}

function openProject(pid) {
  if (state.ws) { state.ws.onclose = null; state.ws.close(); }
  attached.length = 0;
  paintAttached();
  state.pid = pid;
  location.hash = pid;
  $('projects').value = pid;
  connect();
}

function connect() {
  const pid = state.pid;
  const ws = new WebSocket(`ws://${location.host}/ws/${pid}`);
  state.ws = ws;
  ws.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'history') {
      $('messages').querySelectorAll(':scope > :not(#welcome)').forEach((n) => n.remove());
      $('welcome').hidden = event.items.length > 0;
      state.live = null;
      state.activity = null;
      const lastUser = event.items.map((i) => i.type).lastIndexOf('user');
      event.items.forEach((item, index) => render(item, item.type === 'questions' && index > lastUser));
      setBusy(event.busy);
      paintEffort(event.effort);
      keepQuestionsLast();
      if (event.version) showModel(event.report, event.version); else resetStage();
      $('messages').scrollTop = $('messages').scrollHeight;
      return;
    }
    render(event);
    if (event.type === 'user') refreshProjects();
  };
  ws.onclose = () => {
    if (state.pid === pid) setTimeout(() => state.pid === pid && connect(), 1500);
  };
}

function send(text, images = []) {
  text = text.trim();
  if ((!text && !images.length) || state.busy || state.ws?.readyState !== WebSocket.OPEN) return false;
  const piece = text && picked && !peek.id ? picked : null;
  state.ws.send(JSON.stringify({ type: 'user', text, images, piece }));
  if (piece) viewer.select(null);
  input.placeholder = 'Décris ton modèle…';
  return true;
}

const input = $('input');
const autosize = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; };
input.addEventListener('input', autosize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); }
});
$('composer').onsubmit = (e) => {
  e.preventDefault();
  if (send(input.value, attached)) { input.value = ''; autosize(); attached.length = 0; paintAttached(); }
};
$('stop').onclick = () => state.ws?.send(JSON.stringify({ type: 'stop' }));
document.querySelectorAll('.suggestions button').forEach((b) => {
  b.onclick = () => { input.value = b.textContent; autosize(); input.focus(); };
});

$('projects').onchange = (e) => openProject(e.target.value);
$('new-project').onclick = async () => {
  const { id } = await api('/api/projects', { method: 'POST' });
  state.pid = id;
  await refreshProjects();
  openProject(id);
  input.focus();
};
$('delete-project').onclick = async () => {
  if (!state.pid || !window.confirm('Supprimer ce projet et son modèle ?')) return;
  await api(`/api/projects/${state.pid}`, { method: 'DELETE' });
  state.pid = null;
  start();
};

async function start() {
  const status = await api('/api/status');
  const problems = [];
  if (!status.blender) problems.push(status.error);
  if (!status.claude) problems.push('Claude Code est introuvable : installe-le et connecte-toi avec `claude`.');
  live.on = status.live;
  paintLive();
  $('banner').hidden = !problems.length;
  $('banner').textContent = problems.join(' ');

  let projects = await refreshProjects();
  let pid = location.hash.slice(1);
  if (!projects.some((p) => p.id === pid)) pid = projects[0]?.id;
  if (!pid) {
    pid = (await api('/api/projects', { method: 'POST' })).id;
    state.pid = pid;
    projects = await refreshProjects();
  }
  openProject(pid);
}

start();

// Galerie en ligne de ModelForge : la visionneuse de l'application, sans le chat.
// Les modèles sont listés dans models.json (fabriqué par `python -m modelforge.gallery`).

import { Viewer } from './viewer.js';

const $ = (id) => document.getElementById(id);
const viewer = new Viewer($('viewport'));
const number = (v, digits = 0) => v.toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const length = (m) => (m >= 1 ? `${number(m, 2)} m` : m >= 0.01 ? `${number(m * 100, 1)} cm` : `${number(m * 1000, 1)} mm`);

let shown = null;

async function show(model) {
  if (shown === model.id) return;
  shown = model.id;
  document.querySelectorAll('.card').forEach((card) => card.classList.toggle('on', card.dataset.id === model.id));
  history.replaceState(null, '', `#${model.id}`);
  $('loading').hidden = false;
  $('loading').textContent = 'Chargement du modèle…';
  // Réglages remis à zéro d'un modèle à l'autre.
  viewer.play(false);
  $('explode').value = 0; setExplode();
  $('cut').value = 100; updateCut();
  viewer.setMode('orbit', false);
  try {
    await viewer.load(model.file);
  } catch (error) {
    if (shown === model.id) $('loading').textContent = `Impossible d'afficher le modèle : ${error.message}`;
    return;
  }
  if (shown !== model.id) return;
  $('loading').hidden = true;
  $('side').hidden = false;
  $('side-toggle').hidden = false;
  $('help').hidden = false;

  const s = model.size;
  $('dims').hidden = false;
  $('dims').innerHTML =
    `<span>Longueur</span> <b>${length(s.longueur_x)}</b> · <span>Largeur</span> <b>${length(s.largeur_y)}</b> · ` +
    `<span>Hauteur</span> <b>${length(s.hauteur_z)}</b>`;

  const box = $('viewpoints');
  box.replaceChildren();
  for (const viewpoint of model.viewpoints) {
    const button = document.createElement('button');
    button.textContent = viewpoint.name;
    button.onclick = () => viewer.goTo(viewpoint);
    box.appendChild(button);
  }
  $('viewpoints-box').hidden = !box.children.length;
}

// ---- commandes de la visionneuse ---------------------------------------------

const HELP = {
  orbit: 'Glisser pour tourner · molette pour zoomer · clic droit pour déplacer',
  visit: '<kbd>Z</kbd><kbd>Q</kbd><kbd>S</kbd><kbd>D</kbd> avancer et se décaler · glisser pour tourner la tête · <kbd>Espace</kbd>/<kbd>C</kbd> monter/descendre · <kbd>Échap</kbd> pour ressortir',
};
viewer.onModeChange = (mode) => {
  document.querySelectorAll('#mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  $('help').innerHTML = HELP[mode];
};
$('help').innerHTML = HELP.orbit;
$('side-toggle').onclick = () => $('side').classList.toggle('open');
$('mode').onclick = (e) => e.target.dataset.mode && viewer.setMode(e.target.dataset.mode);
$('views').onclick = (e) => e.target.dataset.view && viewer.setView(e.target.dataset.view);

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

viewer.onTime = (time, duration, playing) => {
  $('player').hidden = !duration || !viewer.model;
  if (!duration) return;
  $('play').textContent = playing ? '⏸' : '▶';
  $('timeline').value = Math.round((time / duration) * 1000);
  $('clock').textContent = `${number(time, 1)} / ${number(duration, 1)} s`;
};
$('play').onclick = () => viewer.play();
$('timeline').oninput = () => {
  const time = (Number($('timeline').value) / 1000) * viewer.duration;
  viewer.play(false);
  viewer.seek(time);
};

// ---- liste des modèles -----------------------------------------------------

const models = await (await fetch('models.json')).json();
for (const model of models) {
  const card = document.createElement('button');
  card.className = 'card';
  card.dataset.id = model.id;
  const thumb = model.thumb ? Object.assign(document.createElement('img'), { src: model.thumb, alt: '' })
    : Object.assign(document.createElement('div'), { className: 'no-thumb' });
  const text = document.createElement('span');
  const title = document.createElement('b');
  title.textContent = model.title;
  const facts = document.createElement('small');
  facts.textContent = `${number(model.objects)} pièces · ${number(model.triangles)} triangles`;
  text.append(title, facts);
  if (model.animation_seconds) {
    const anim = document.createElement('small');
    anim.textContent = `Animation de ${number(model.animation_seconds)} s`;
    text.append(anim);
  }
  card.append(thumb, text);
  card.onclick = () => show(model);
  $('cards').appendChild(card);
}
if (models.length) show(models.find((m) => m.id === location.hash.slice(1)) || models[0]);
else $('loading').textContent = 'Aucun modèle dans la galerie.';

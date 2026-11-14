// Visionneuse 3D : charge le modèle exporté par Blender (.glb) et permet de
// tourner autour, de le couper, et de se déplacer dedans.
//
// Repères : Blender (x = longueur, y = largeur, z = hauteur) devient dans
// three.js (x, z, -y) : la hauteur est l'axe Y de three.js.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const fromBlender = ([x, y, z]) => new THREE.Vector3(x, z, -y);
const toBlender = (v) => [v.x, -v.z, v.y];

// Direction depuis laquelle on regarde le modèle, pour chaque vue.
const VIEWS = {
  front: [1, 0.12, 0], back: [-1, 0.12, 0],
  left: [0, 0.12, -1], right: [0, 0.12, 1],
  top: [0.001, 1, 0], bottom: [0.001, -1, 0],
  iso: [1, 0.55, -0.9],
};

export class Viewer {
  constructor(container) {
    this.container = container;
    this.mode = 'orbit';
    this.model = null;
    this.box = new THREE.Box3(new THREE.Vector3(-1, 0, -1), new THREE.Vector3(1, 1, 1));
    this.size = 2;
    this.keys = new Set();
    this.yaw = 0;
    this.pitch = 0;
    this.flight = null;      // animation de caméra en cours
    this.cut = { axis: 'z', t: 1 };
    this.explode = 0;        // 0 = assemblé, 1 = pièces écartées au maximum
    this.pieces = [];
    this.onModeChange = () => {};
    this.mixer = null;       // animation du modèle (s'il en a une)
    this.duration = 0;
    this.time = 0;
    this.playing = false;
    this.onTime = () => {};
    this.selected = null;    // pièce cliquée : { node, frame }
    this.onSelect = () => {};

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.localClippingEnabled = true;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    // Les ombres ne sont recalculées que quand le modèle change, et l'image
    // n'est redessinée que quand quelque chose bouge : au repos, la
    // visionneuse ne consomme presque rien.
    this.renderer.shadowMap.autoUpdate = false;
    this.dirty = true;
    this.lastView = new THREE.Matrix4();
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.5;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa3b0, 0.35));
    // Soleil avec ombres portées : elles donnent le relief et posent le modèle au sol.
    this.sun = new THREE.DirectionalLight(0xffffff, 2.4);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(4096, 4096);
    this.sun.shadow.radius = 3;   // bord des ombres adouci
    this.scene.add(this.sun, this.sun.target);
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
      new THREE.ShadowMaterial({ opacity: 0.28 }));
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.receiveShadow = true;
    this.ground.visible = false;
    this.scene.add(this.ground);

    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    this.camera.position.set(4, 3, -5);
    // En visite, une lampe suit le visiteur : le soleil n'entre pas dans le modèle.
    this.lamp = new THREE.PointLight(0xffffff, 0, 0, 0);
    this.camera.add(this.lamp);
    this.scene.add(this.camera);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.addEventListener('start', () => { this.flight = null; });

    this.plane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);
    this.grid = null;

    this._bindInput();
    new ResizeObserver(() => this._resize()).observe(container);
    this._resize();
    this.last = performance.now();
    this.renderer.setAnimationLoop(() => this._frame());
  }

  // ---- chargement ------------------------------------------------------

  async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    const root = gltf.scene;
    root.traverse((node) => {
      if (!node.isMesh) return;
      let solid = true;
      for (const material of [].concat(node.material)) {
        material.side = THREE.DoubleSide;        // les parois se voient aussi de l'intérieur
        material.clippingPlanes = [this.plane];
        material.clipShadows = true;
        if (material.transparent) {
          material.depthWrite = false;
          solid = false;
          // Flammes et lumières translucides : elles s'ajoutent à ce qu'il y a derrière.
          if (material.emissive && material.emissive.getHex() !== 0) material.blending = THREE.AdditiveBlending;
        }
      }
      node.castShadow = solid;
      node.receiveShadow = solid;
    });

    this.select(null);
    const first = !this.model;
    const previousSize = this.size;
    if (this.model) {
      this.scene.remove(this.model);
      this.model.traverse((node) => {
        if (node.isMesh) {
          node.geometry.dispose();
          [].concat(node.material).forEach((m) => m.dispose());
        }
      });
    }
    this.model = root;
    this.scene.add(root);

    this.box = new THREE.Box3().setFromObject(root);
    this._findPieces(root);
    this._setupAnimation(root, gltf.animations);
    this._applyExplode();
    if (this.box.isEmpty()) this.box.set(new THREE.Vector3(-1, 0, -1), new THREE.Vector3(1, 1, 1));
    this.size = Math.max(this.box.getSize(new THREE.Vector3()).length(), 0.01);
    this._updateGrid();
    this._updateSun();
    this._applyCut();

    // On ne recadre que si c'est le premier modèle ou si sa taille a nettement
    // changé : sinon l'utilisateur garde son point de vue pendant les retouches.
    const ratio = this.size / previousSize;
    if (first || ratio > 1.6 || ratio < 0.6) {
      this.setMode('orbit');
      this.setView('iso', false);
    } else {
      this._updateClip();
    }
  }

  _updateGrid() {
    if (this.grid) { this.scene.remove(this.grid); this.grid.geometry.dispose(); }
    const span = this.box.getSize(new THREE.Vector3());
    const extent = Math.max(span.x, span.z) * 2.2;
    const cell = Math.pow(10, Math.floor(Math.log10(extent / 12)));
    const divisions = Math.max(2, Math.round(extent / cell));
    this.grid = new THREE.GridHelper(divisions * cell, divisions, 0x7d8794, 0x7d8794);
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.28;
    this.grid.position.y = Math.min(0, this.box.min.y) - this.size * 3e-4;
    this.scene.add(this.grid);
    this.gridCell = cell;
  }

  // Cale le soleil et la zone d'ombres sur la taille du modèle.
  _updateSun() {
    const center = this.box.getCenter(new THREE.Vector3());
    const reach = this.size;
    this.sun.target.position.copy(center);
    this.sun.position.copy(center).add(new THREE.Vector3(0.45, 1, -0.6).normalize().multiplyScalar(reach * 2));
    const camera = this.sun.shadow.camera;
    camera.left = camera.bottom = -reach * 0.75;
    camera.right = camera.top = reach * 0.75;
    camera.near = reach * 0.05;
    camera.far = reach * 5;
    camera.updateProjectionMatrix();
    this.sun.shadow.bias = -0.0004;
    this.sun.shadow.normalBias = reach * 0.0015;
    this.ground.scale.setScalar(reach * 6);
    this.ground.position.set(center.x, Math.min(0, this.box.min.y) - reach * 2e-4, center.z);
    this.ground.visible = true;
    this._changed();
  }

  _updateClip() {
    this.dirty = true;
    const near = this.mode === 'visit' ? this.size / 4000 : this.size / 400;
    this.camera.near = Math.max(near, 0.005);
    this.camera.far = this.size * 40;
    this.camera.updateProjectionMatrix();
  }

  // ---- mode « tourner autour » ----------------------------------------

  setView(name, animate = true) {
    const center = this.box.getCenter(new THREE.Vector3());
    const direction = new THREE.Vector3(...VIEWS[name]).normalize();
    this.setMode('orbit', false);
    // Distance minimale à laquelle les 8 coins de la boîte englobante tiennent
    // dans l'image, vus depuis cette direction.
    const probe = new THREE.PerspectiveCamera();
    probe.position.copy(center).add(direction);
    probe.lookAt(center);
    probe.updateMatrixWorld();
    const right = new THREE.Vector3(), up = new THREE.Vector3(), back = new THREE.Vector3();
    probe.matrixWorld.extractBasis(right, up, back);
    const tanV = Math.tan(THREE.MathUtils.degToRad(this.camera.fov) / 2);
    const tanH = tanV * this.camera.aspect;
    let distance = 0;
    const { min, max } = this.box;
    for (const x of [min.x, max.x]) for (const y of [min.y, max.y]) for (const z of [min.z, max.z]) {
      const d = new THREE.Vector3(x, y, z).sub(center);
      const depth = d.dot(back);
      distance = Math.max(distance, depth + Math.abs(d.dot(right)) / tanH, depth + Math.abs(d.dot(up)) / tanV);
    }
    const position = center.clone().addScaledVector(direction, distance * 1.1);
    this._fly(position, center, animate);
  }

  rotate(direction) {
    const step = THREE.MathUtils.degToRad(15);
    if (this.mode === 'visit') {
      if (direction === 'left') this.yaw += step;
      if (direction === 'right') this.yaw -= step;
      if (direction === 'up') this.pitch += step;
      if (direction === 'down') this.pitch -= step;
      this._clampPitch();
      return;
    }
    this.flight = null;
    const offset = this.camera.position.clone().sub(this.controls.target);
    const spherical = new THREE.Spherical().setFromVector3(offset);
    if (direction === 'left') spherical.theta -= step;
    if (direction === 'right') spherical.theta += step;
    if (direction === 'up') spherical.phi -= step;
    if (direction === 'down') spherical.phi += step;
    spherical.phi = THREE.MathUtils.clamp(spherical.phi, 0.02, Math.PI - 0.02);
    this._fly(this.controls.target.clone().add(new THREE.Vector3().setFromSpherical(spherical)),
      this.controls.target.clone(), true, 0.18);
  }

  _fly(position, target, animate = true, duration = 0.55) {
    this._updateClip();
    if (!animate) {
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update();
      return;
    }
    this.flight = {
      fromPosition: this.camera.position.clone(), toPosition: position,
      fromTarget: this.controls.target.clone(), toTarget: target,
      elapsed: 0, duration,
    };
  }

  // ---- mode « visiter » -------------------------------------------------

  setMode(mode, keepCamera = true) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.flight = null;
    this.keys.clear();
    if (mode === 'visit') {
      this.controls.enabled = false;
      const dir = this.camera.getWorldDirection(new THREE.Vector3());
      this.yaw = Math.atan2(-dir.x, -dir.z);
      this.pitch = Math.asin(THREE.MathUtils.clamp(dir.y, -1, 1));
    } else {
      this.controls.enabled = true;
      // On tourne de nouveau autour du centre du modèle.
      this.controls.target.copy(this.box.getCenter(new THREE.Vector3()));
      if (keepCamera && this.box.containsPoint(this.camera.position)) this.setViewQuiet();
      this.controls.update();
    }
    this.camera.fov = mode === 'visit' ? 65 : 45;
    this.lamp.intensity = mode === 'visit' ? 1.5 : 0;
    this._updateClip();
    this.onModeChange(mode);
  }

  setViewQuiet() {
    const center = this.box.getCenter(new THREE.Vector3());
    this.camera.position.copy(center).addScaledVector(new THREE.Vector3(...VIEWS.iso).normalize(), this.size * 1.1);
  }

  goTo(viewpoint) {
    this.setMode('visit');
    this.camera.position.copy(fromBlender(viewpoint.position));
    const dir = fromBlender(viewpoint.direction).normalize();
    this.yaw = Math.atan2(-dir.x, -dir.z);
    this.pitch = Math.asin(THREE.MathUtils.clamp(dir.y, -1, 1));
    this._updateClip();
  }

  _clampPitch() {
    this.pitch = THREE.MathUtils.clamp(this.pitch, -1.5, 1.5);
  }

  _walk(dt) {
    const forward = new THREE.Vector3(-Math.sin(this.yaw), 0, -Math.cos(this.yaw));
    const right = new THREE.Vector3(-forward.z, 0, forward.x);
    const move = new THREE.Vector3();
    if (this.keys.has('KeyW')) move.add(forward);
    if (this.keys.has('KeyS')) move.sub(forward);
    if (this.keys.has('KeyD')) move.add(right);
    if (this.keys.has('KeyA')) move.sub(right);
    if (this.keys.has('Space') || this.keys.has('KeyE')) move.y += 1;
    if (this.keys.has('KeyC')) move.y -= 1;
    if (move.lengthSq() > 0) {
      const fast = this.keys.has('ShiftLeft') || this.keys.has('ShiftRight') ? 3.5 : 1;
      this.camera.position.addScaledVector(move.normalize(), this._speed() * fast * dt);
    }
    const turn = 1.4 * dt;
    if (this.keys.has('ArrowLeft')) this.yaw += turn;
    if (this.keys.has('ArrowRight')) this.yaw -= turn;
    if (this.keys.has('ArrowUp')) this.pitch += turn;
    if (this.keys.has('ArrowDown')) this.pitch -= turn;
    this._clampPitch();
    this.camera.quaternion.setFromEuler(new THREE.Euler(this.pitch, this.yaw, 0, 'YXZ'));
  }

  // Vitesse de marche : celle d'un piéton pour un objet à taille réelle,
  // proportionnelle au modèle s'il est petit (maquette).
  _speed() {
    return THREE.MathUtils.clamp(this.size * 0.06, 0.02, 2.2);
  }

  // ---- coupe -------------------------------------------------------------

  setCut(axis, t) {
    this.cut = { axis, t };
    this._applyCut();
  }

  _applyCut() {
    this._changed();
    const { axis, t } = this.cut;
    const lo = this.box.min[axis], hi = this.box.max[axis];
    const margin = (hi - lo) * 0.002 + 1e-4;
    // La largeur se coupe depuis la gauche du modèle (-Z dans three.js).
    if (axis === 'z') {
      this.plane.normal.set(0, 0, 1);
      this.plane.constant = -(hi - (hi - lo) * t) + margin;
    } else {
      this.plane.normal.set(axis === 'x' ? -1 : 0, axis === 'y' ? -1 : 0, 0);
      this.plane.constant = lo + (hi - lo) * t + margin;
    }
  }

  // ---- vue éclatée --------------------------------------------------------

  // Chaque pièce s'écarte du centre du modèle, dans la direction où elle se
  // trouve déjà : plus elle est loin du centre, plus elle part loin.
  setExplode(t) {
    this.explode = t;
    this._applyExplode();
  }

  _findPieces(root) {
    this.pieces = [];
    root.updateMatrixWorld(true);
    const center = this.box.getCenter(new THREE.Vector3());
    // Un objet Blender à plusieurs matériaux arrive comme un groupe de
    // morceaux : c'est le groupe entier qui forme la pièce.
    const isSplitObject = (node) => node !== root && !node.isMesh && node.children.length > 0
      && node.children.every((c) => c.isMesh && !c.children.length && c.position.lengthSq() === 0);
    const visit = (node) => {
      const piece = node.isMesh || isSplitObject(node);
      if (piece) {
        const offset = new THREE.Box3().setFromObject(node).getCenter(new THREE.Vector3()).sub(center);
        // Le décalage est exprimé dans le repère du parent de la pièce.
        const toParent = new THREE.Matrix3().setFromMatrix4(node.parent.matrixWorld.clone().invert());
        this.pieces.push({ node, base: node.position.clone(), offset: offset.applyMatrix3(toParent) });
      }
      if (!isSplitObject(node)) node.children.forEach(visit);
    };
    visit(root);
  }

  // À appeler quand le modèle a changé d'aspect : ombres et image à refaire.
  _changed() {
    this.renderer.shadowMap.needsUpdate = true;
    this.dirty = true;
  }

  _applyExplode() {
    this._changed();
    const k = this.explode * 1.6;
    for (const { node, base, offset } of this.pieces) node.position.copy(base).addScaledVector(offset, k);
  }

  // ---- animation -----------------------------------------------------------

  _setupAnimation(root, clips) {
    const keep = Math.min(this.time, clips.length ? clips[0].duration : 0);
    this.mixer = null;
    this.duration = 0;
    this.playing = false;
    this.animated = [];
    if (clips.length) {
      const clip = clips[0];
      this.mixer = new THREE.AnimationMixer(root);
      this.mixer.clipAction(clip).play();
      this.duration = clip.duration;
      // Pièces que l'animation déplace : la vue éclatée part de leur position animée.
      const names = new Set(clip.tracks.map((track) => THREE.PropertyBinding.parseTrackName(track.name).nodeName));
      this.animated = this.pieces.filter(({ node }) => names.has(node.name) || names.has(node.uuid));
    }
    this.seek(keep);
  }

  seek(time) {
    this.time = THREE.MathUtils.clamp(time, 0, this.duration);
    if (this.mixer) {
      // Juste avant la fin : à la durée exacte, la boucle reviendrait au début.
      this.mixer.setTime(Math.min(this.time, this.duration - 1e-4));
      for (const piece of this.animated) piece.base.copy(piece.node.position);
      this._applyExplode();
    }
    this.onTime(this.time, this.duration, this.playing);
  }

  play(on = !this.playing) {
    if (!this.mixer) return;
    this.playing = on;
    if (on && this.time >= this.duration - 1e-3) this.time = 0;
    this.seek(this.time);
  }

  // ---- sélection d'une pièce ----------------------------------------------

  // Pièce (objet Blender) à laquelle appartient un maillage.
  _pieceOf(mesh) {
    return this.pieces.find((piece) => piece.node === mesh.parent)?.node
      || this.pieces.find((piece) => piece.node === mesh)?.node || mesh;
  }

  _pick(event) {
    if (!this.model) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(pointer, this.camera);
    // On ignore ce que la coupe a retiré : on clique sur ce qu'on voit.
    const hit = ray.intersectObject(this.model, true)
      .find((h) => h.object.isMesh && this.plane.distanceToPoint(h.point) >= 0);
    return hit ? this._pieceOf(hit.object) : null;
  }

  // Entoure une pièce d'un cadre orange (ou aucune) et prévient l'interface.
  // Un cadre, et non une teinte : les couleurs du modèle restent exactes.
  select(node) {
    if (this.selected) {
      this.scene.remove(this.selected.frame);
      this.selected.frame.geometry.dispose();
      this.selected.frame.material.dispose();
      this.selected = null;
    }
    this.dirty = true;
    if (!node) { this.onSelect(null); return; }
    const frame = new THREE.BoxHelper(node, 0xff8a1f);
    frame.material.depthTest = false;      // visible même derrière d'autres pièces
    frame.material.transparent = true;
    frame.renderOrder = 10;
    this.scene.add(frame);
    this.selected = { node, frame };
    this.dirty = true;
    const box = new THREE.Box3().setFromObject(node);
    // Position et taille de la pièce assemblée (sans le décalage de la vue éclatée).
    const piece = this.pieces.find((item) => item.node === node);
    const center = box.getCenter(new THREE.Vector3());
    if (piece && node.parent) {
      const shift = piece.offset.clone().multiplyScalar(this.explode * 1.6)
        .applyMatrix3(new THREE.Matrix3().setFromMatrix4(node.parent.matrixWorld));
      center.sub(shift);
    }
    const size = box.getSize(new THREE.Vector3());
    this.onSelect({
      name: node.userData.name || node.name || 'Pièce',
      center: toBlender(center),
      size: [size.x, size.z, size.y],
    });
  }

  // Caméra actuelle, dans le repère de Blender (pour le rendu réaliste).
  cameraForBlender() {
    const position = this.camera.position;
    const target = this.mode === 'visit'
      ? position.clone().add(this.camera.getWorldDirection(new THREE.Vector3()))
      : this.controls.target;
    return { position: toBlender(position), target: toBlender(target),
      fov: this.camera.fov, aspect: this.camera.aspect, time: this.time };
  }

  // ---- entrées -----------------------------------------------------------

  _bindInput() {
    const el = this.renderer.domElement;
    el.tabIndex = 0;
    let drag = null;
    // Un clic sans glisser désigne une pièce (ou aucune, dans le vide).
    let press = null;
    el.addEventListener('pointerdown', (e) => { press = e.button === 0 ? { x: e.clientX, y: e.clientY } : null; });
    el.addEventListener('pointerup', (e) => {
      if (press && Math.hypot(e.clientX - press.x, e.clientY - press.y) < 5) this.select(this._pick(e));
      press = null;
    });
    el.addEventListener('pointerdown', (e) => {
      el.focus();
      if (this.mode !== 'visit') return;
      drag = { x: e.clientX, y: e.clientY };
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener('pointermove', (e) => {
      if (!drag || this.mode !== 'visit') return;
      this.yaw -= (e.clientX - drag.x) * 0.004;
      this.pitch -= (e.clientY - drag.y) * 0.004;
      this._clampPitch();
      drag = { x: e.clientX, y: e.clientY };
    });
    const end = () => { drag = null; };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('wheel', (e) => {
      if (this.mode !== 'visit') return;
      e.preventDefault();
      const dir = this.camera.getWorldDirection(new THREE.Vector3());
      this.camera.position.addScaledVector(dir, -e.deltaY * 0.004 * this._speed());
    }, { passive: false });

    const typing = (e) => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    window.addEventListener('keydown', (e) => {
      if (typing(e) || e.metaKey || e.ctrlKey) return;
      if (this.mode === 'visit') {
        if (e.code === 'Escape') { this.setMode('orbit'); return; }
        if (/^(Key[WASDEC]|Space|Shift(Left|Right)|Arrow\w+)$/.test(e.code)) {
          this.keys.add(e.code);
          e.preventDefault();
        }
      } else if (e.code.startsWith('Arrow') && this.model) {
        this.rotate(e.code.slice(5).toLowerCase());
        e.preventDefault();
      }
    });
    window.addEventListener('keyup', (e) => this.keys.delete(e.code));
    window.addEventListener('blur', () => this.keys.clear());
  }

  _resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.dirty = true;
  }

  _frame() {
    const now = performance.now();
    const dt = Math.min((now - this.last) / 1000, 0.1);
    this.last = now;
    if (this.playing) {
      const next = this.time + dt;
      if (next >= this.duration) { this.playing = false; this.seek(this.duration); }
      else this.seek(next);
    }
    if (this.mode === 'visit') {
      this._walk(dt);
    } else {
      if (this.flight) {
        const f = this.flight;
        f.elapsed += dt;
        const k = Math.min(f.elapsed / f.duration, 1);
        const ease = k * k * (3 - 2 * k);
        this.camera.position.lerpVectors(f.fromPosition, f.toPosition, ease);
        this.controls.target.lerpVectors(f.fromTarget, f.toTarget, ease);
        if (k >= 1) this.flight = null;
      }
      this.controls.update();
    }
    this.camera.updateMatrixWorld();
    if (!this.dirty && this.camera.matrixWorld.equals(this.lastView)) return;   // rien n'a bougé
    this.dirty = false;
    this.lastView.copy(this.camera.matrixWorld);
    if (this.selected) this.selected.frame.update();   // suit l'animation et la vue éclatée
    this.renderer.render(this.scene, this.camera);
  }
}

"""Serveur local d'ModelForge : sert l'interface, relaie le chat vers l'agent
par WebSocket et expose les fichiers des projets (modèle, aperçus)."""

import asyncio
import base64
import contextlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import MODEL, AgentSession
from . import blender_bridge
from .blender_bridge import Project, find_blender

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PROJECTS = ROOT / "projects"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
STORED = {"user", "text", "questions", "model", "error", "tool_start", "photo", "restored"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 6_000_000

rooms: dict[str, "Room"] = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # À l'arrêt : fermer proprement les agents encore ouverts.
    await asyncio.gather(*(room.session.close() for room in rooms.values()), return_exceptions=True)


app = FastAPI(title="ModelForge", lifespan=lifespan)


def local_origin(origin: str | None) -> bool:
    """Seule l'interface servie en local a le droit de piloter l'agent : une
    page web quelconque ouverte dans le navigateur ne doit pas pouvoir le faire."""
    return origin is None or urlparse(origin).hostname in LOCAL_HOSTS


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in ("GET", "HEAD") and not local_origin(request.headers.get("origin")):
        return JSONResponse({"detail": "Origine refusée."}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith(("/files/", "/api/")) or request.url.path in ("/", "/app.js", "/style.css"):
        response.headers["Cache-Control"] = "no-store"
    return response


def project_dir(pid: str) -> Path:
    if not ID_RE.match(pid):
        raise HTTPException(404, "Projet inconnu.")
    path = PROJECTS / pid
    if not path.is_dir():
        raise HTTPException(404, "Projet inconnu.")
    return path


class Room:
    """Un projet ouvert : son agent, son historique de chat et les navigateurs
    connectés dessus."""

    def __init__(self, pid: str):
        self.pid = pid
        self.project = Project(PROJECTS / pid)
        self.sockets: set[WebSocket] = set()
        self.chat_path = self.project.root / "chat.json"
        self.items: list[dict] = (json.loads(self.chat_path.read_text(encoding="utf-8"))
                                  if self.chat_path.exists() else [])
        self.session = AgentSession(self.project, self.emit)
        self.last_refs: list[Path] = []   # images jointes au dernier message

    async def emit(self, event: dict) -> None:
        if event["type"] == "new_project":
            await self.branch(event["title"], event["prompt"])
            return
        if event["type"] in STORED:
            self.record(event)
        for ws in list(self.sockets):
            try:
                await ws.send_json(event)
            except Exception:
                self.sockets.discard(ws)

    def record(self, event: dict) -> None:
        item = {k: v for k, v in event.items() if k != "streamed"}
        if item["type"] == "restored":
            item.pop("report", None)
        if item["type"] == "model":
            item.pop("previews", None)
        self.items.append(item)
        self.chat_path.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")

    async def branch(self, title: str, prompt: str) -> None:
        """L'agent a jugé que la demande concerne un autre objet : on ouvre un
        nouveau projet, on y emmène l'utilisateur et on y transmet la demande."""
        pid = new_project_id()
        Project(PROJECTS / pid).save_meta(created=time.time(), title=title)
        for ws in list(self.sockets):
            try:
                await ws.send_json({"type": "open_project", "id": pid})
            except Exception:
                self.sockets.discard(ws)
        # Les images jointes à la demande suivent dans le nouveau projet.
        images = [base64.b64encode(path.read_bytes()).decode() for path in self.last_refs]
        await room_for(pid).user_message(prompt, images)

    def save_images(self, images: list) -> list[str]:
        """Enregistre les images de référence jointes (JPEG en base64, préparées
        par l'interface) dans le dossier refs/ du projet. Renvoie leurs noms."""
        folder = self.project.root / "refs"
        names = []
        for data in images[:MAX_IMAGES]:
            try:
                raw = base64.b64decode(str(data).split(",", 1)[-1], validate=True)
            except ValueError:
                continue
            if not raw.startswith(b"\xff\xd8") or len(raw) > MAX_IMAGE_BYTES:
                continue
            folder.mkdir(exist_ok=True)
            name = f"ref-{int(time.time() * 1000)}-{len(names)}.jpg"
            (folder / name).write_bytes(raw)
            names.append(name)
        return names

    async def user_message(self, text: str, images: list | None = None,
                           piece: dict | None = None) -> None:
        names = self.save_images(images or [])
        text = text or "Voici une image de référence."
        if not self.project.meta().get("title"):
            self.project.save_meta(title=text.strip().splitlines()[0][:70])
        event = {"type": "user", "text": text}
        if names:
            event["images"] = names
        if piece:
            event["piece"] = piece["name"]
            # L'agent reçoit en plus de quoi retrouver la pièce dans la scène.
            text += (f"\n\n[Pièce désignée par l'utilisateur dans la visionneuse : « {piece['name']} »"
                     f", centre {piece['center']}, taille {piece['size']} (mètres, repère Blender).]")
        await self.emit(event)
        self.last_refs = [self.project.root / "refs" / n for n in names]
        self.session.send(text, [str(path) for path in self.last_refs])


def new_project_id() -> str:
    pid = time.strftime("%Y%m%d-%H%M%S")
    while (PROJECTS / pid).exists():
        pid += "x"
    return pid


def room_for(pid: str) -> Room:
    project_dir(pid)
    if pid not in rooms:
        rooms[pid] = Room(pid)
    return rooms[pid]


# ---- API ---------------------------------------------------------------

@app.get("/api/status")
async def status():
    try:
        blender = find_blender()
    except FileNotFoundError as exc:
        return {"blender": None, "error": str(exc), "model": MODEL, "live": False,
                "claude": bool(shutil.which("claude"))}
    return {"blender": blender, "model": MODEL, "claude": bool(shutil.which("claude")),
            "live": await blender_bridge.live_available()}


@app.get("/api/projects")
def list_projects():
    out = []
    if PROJECTS.exists():
        for path in PROJECTS.iterdir():
            if path.is_dir() and ID_RE.match(path.name):
                meta = Project(path).meta()
                out.append({"id": path.name, "title": meta.get("title") or "Nouveau projet",
                            "created": meta.get("created", 0), "has_model": (path / "model.glb").exists()})
    return sorted(out, key=lambda p: -p["created"])


@app.post("/api/projects")
def create_project():
    pid = new_project_id()
    Project(PROJECTS / pid).save_meta(created=time.time())
    return {"id": pid}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    path = project_dir(pid)
    room = rooms.pop(pid, None)
    if room is not None:
        await room.session.close()
    shutil.rmtree(path)
    return {"ok": True}


@app.post("/api/projects/{pid}/live")
async def live(pid: str):
    """Montre le projet dans un Blender en direct : celui qui est déjà ouvert
    s'il y en a un, sinon on en lance un."""
    project = Project(project_dir(pid))
    shown = await blender_bridge.live_call({"mode": "open", "blend": str(project.blend)}, timeout=60)
    if shown is None:
        blender_bridge.launch_live(project)
        return {"ok": True, "launched": True}
    blender = find_blender()
    if ".app/" in blender:  # macOS : amener la fenêtre de Blender au premier plan
        subprocess.Popen(["open", "-a", blender.split(".app/")[0] + ".app"])
    return {"ok": True, "launched": False}


def clean_piece(value) -> dict | None:
    """Pièce cliquée dans la visionneuse, telle qu'envoyée par l'interface."""
    if not isinstance(value, dict) or not str(value.get("name", "")).strip():
        return None
    try:
        center = [round(float(v), 3) for v in value["center"]]
        size = [round(float(v), 3) for v in value["size"]]
    except (KeyError, TypeError, ValueError):
        return None
    if len(center) != 3 or len(size) != 3:
        return None
    name = re.sub(r"[\[\]«»\n\r]", " ", str(value["name"]))[:120].strip()
    return {"name": name, "center": center, "size": size}


@app.get("/api/projects/{pid}/versions")
def versions(pid: str):
    project = Project(project_dir(pid))
    meta = project.meta()
    if not project.versions() and project.blend.exists() and meta.get("report"):
        # Projet d'avant l'historique : son état actuel devient la première version.
        project.snapshot(str(meta.get("version") or int(time.time() * 1000)), "Version de départ", meta["report"])
        meta = project.meta()
    return {"versions": project.versions(), "current": meta.get("current")}


@app.post("/api/projects/{pid}/versions/{vid}/restore")
async def restore(pid: str, vid: str):
    """Revient à une version antérieure du modèle."""
    room = room_for(pid)
    if room.session.busy:
        raise HTTPException(409, "L'agent travaille : attends qu'il ait fini.")
    try:
        entry, report = room.project.restore(vid)
    except KeyError:
        raise HTTPException(404, "Version inconnue.")
    await blender_bridge.live_call({"mode": "reload", "blend": str(room.project.blend)}, timeout=30)
    await room.emit({"type": "restored", "label": entry["label"], "id": vid, "report": report,
                     "version": room.project.meta()["version"]})
    return {"ok": True}


def vector(value) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise HTTPException(400, "Caméra invalide.")
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        raise HTTPException(400, "Caméra invalide.")


@app.post("/api/projects/{pid}/photo")
async def photo(pid: str, request: Request):
    """Rendu réaliste du modèle, vu depuis la caméra de la visionneuse."""
    room = room_for(pid)
    if not room.project.blend.exists():
        raise HTTPException(409, "Pas encore de modèle.")
    body = await request.json()
    try:
        fov, aspect = float(body.get("fov", 45)), float(body.get("aspect", 1.5))
    except (TypeError, ValueError):
        raise HTTPException(400, "Caméra invalide.")
    try:
        at = float(body.get("time") or 0)
    except (TypeError, ValueError):
        at = 0.0
    result = await blender_bridge.photo(
        room.project, vector(body.get("position")), vector(body.get("target")), fov, aspect, at)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Échec du rendu.")[-600:])
    name = Path(result["path"]).name
    await room.emit({"type": "photo", "file": name})
    return {"ok": True, "file": name}


@app.get("/api/projects/{pid}/stl")
async def stl(pid: str):
    """Fichier pour l'impression 3D, fabriqué à la demande."""
    project = Project(project_dir(pid))
    if not project.blend.exists():
        raise HTTPException(409, "Pas encore de modèle.")
    result = await blender_bridge.export_stl(project)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Échec de l'export.")[-600:])
    return FileResponse(result["path"], filename="modele.stl", media_type="model/stl")


@app.get("/files/{pid}/{name:path}")
def project_file(pid: str, name: str):
    base = project_dir(pid)
    if name not in ("model.glb", "scene.blend") and not re.fullmatch(
            r"(previews|photos|refs)/[\w.-]+\.jpg|versions/\d{1,20}/(model\.glb|thumb\.jpg)", name):
        raise HTTPException(404)
    path = base / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=f"{pid}-{path.name}" if name == "scene.blend" else None)


@app.websocket("/ws/{pid}")
async def chat(ws: WebSocket, pid: str):
    if not local_origin(ws.headers.get("origin")):
        await ws.close(code=4403)
        return
    try:
        room = room_for(pid)
    except HTTPException:
        await ws.close(code=4404)
        return
    await ws.accept()
    room.sockets.add(ws)
    meta = room.project.meta()
    await ws.send_json({"type": "history", "items": room.items, "busy": room.session.busy,
                        "title": meta.get("title"), "report": meta.get("report"),
                        "version": meta.get("version"), "effort": room.session.effort})
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            images = message.get("images") if isinstance(message.get("images"), list) else []
            if kind == "user" and (str(message.get("text", "")).strip() or images):
                await room.user_message(str(message.get("text", "")).strip(), images,
                                        clean_piece(message.get("piece")))
            elif kind == "effort":
                if await room.session.set_effort(str(message.get("value"))):
                    await room.emit({"type": "effort", "value": room.session.effort})
            elif kind == "stop":
                await room.session.stop()
    except WebSocketDisconnect:
        pass
    finally:
        room.sockets.discard(ws)


app.mount("/vendor/three", StaticFiles(directory=WEB / "node_modules" / "three"), name="three")
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

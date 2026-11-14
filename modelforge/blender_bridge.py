"""Pont entre l'application et Blender : lance Blender sans interface sur un
travail (job) et renvoie son résultat."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

RUNNER = Path(__file__).parent / "blender" / "runner.py"
LIVE_SCRIPT = Path(__file__).parent / "blender" / "live.py"
LIVE_INFO = Path(__file__).resolve().parent.parent / ".live_blender.json"
TIMEOUT_S = 600
MAX_VERSIONS = 40


def find_blender() -> str:
    """Chemin de l'exécutable Blender (variable BLENDER_PATH, PATH, ou
    emplacement standard macOS)."""
    candidates = [
        os.environ.get("BLENDER_PATH"),
        shutil.which("blender"),
        "/Applications/Blender.app/Contents/MacOS/Blender",
        str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError(
        "Blender introuvable. Installe-le dans /Applications ou définis BLENDER_PATH.")


class Project:
    """Un projet = un dossier contenant la scène, le modèle exporté, les
    aperçus et l'historique des scripts."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "previews").mkdir(exist_ok=True)

    @property
    def blend(self) -> Path:
        return self.root / "scene.blend"

    @property
    def glb(self) -> Path:
        return self.root / "model.glb"

    @property
    def meta_path(self) -> Path:
        return self.root / "project.json"

    def meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        return {}

    def save_meta(self, **changes) -> dict:
        meta = {**self.meta(), **changes}
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    # ---- historique des versions ----------------------------------------

    @property
    def versions_path(self) -> Path:
        return self.root / "versions.json"

    def versions(self) -> list[dict]:
        if self.versions_path.exists():
            return json.loads(self.versions_path.read_text(encoding="utf-8"))
        return []

    def snapshot(self, vid: str, label: str, report: dict) -> dict:
        """Garde une copie de l'état actuel (scène, modèle exporté, vignette)
        pour pouvoir y revenir. Les plus anciennes copies sont supprimées."""
        folder = self.root / "versions" / vid
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.blend, folder / "scene.blend")
        if self.glb.exists():
            shutil.copy2(self.glb, folder / "model.glb")
        thumb = self.root / "previews" / "ext_0.jpg"
        if thumb.exists():
            shutil.copy2(thumb, folder / "thumb.jpg")
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        entry = {"id": vid, "label": label, "time": time.time(), "size": report["size"],
                 "objects": report["object_count"], "triangles": report["triangles"]}
        entries = self.versions() + [entry]
        for old in entries[:-MAX_VERSIONS]:
            shutil.rmtree(self.root / "versions" / old["id"], ignore_errors=True)
        entries = entries[-MAX_VERSIONS:]
        self.versions_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        self.save_meta(current=vid)
        return entry

    def restore(self, vid: str) -> tuple[dict, dict]:
        """Remet la scène et le modèle dans l'état de la version demandée.
        Renvoie (entrée de l'historique, rapport). KeyError si elle n'existe pas."""
        entry = next((e for e in self.versions() if e["id"] == vid), None)
        folder = self.root / "versions" / vid
        if entry is None or not (folder / "scene.blend").exists():
            raise KeyError(vid)
        shutil.copy2(folder / "scene.blend", self.blend)
        if (folder / "model.glb").exists():
            shutil.copy2(folder / "model.glb", self.glb)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        self.save_meta(report=report, version=int(time.time() * 1000), current=vid,
                       notice=f"L'utilisateur est revenu à une version antérieure du modèle "
                              f"(« {entry['label']} ») : la scène est maintenant dans cet état, "
                              "les passes faites après elle sont annulées. Repars de cet état.")
        return entry, report

    def next_script(self) -> Path:
        n = len(list((self.root / "scripts").glob("*.py"))) + 1
        return self.root / "scripts" / f"{n:03d}.py"


async def _run_job(job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="modelforge_") as tmp:
        job_path = Path(tmp) / "job.json"
        result_path = Path(tmp) / "result.json"
        job["result"] = str(result_path)
        job_path.write_text(json.dumps(job), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            find_blender(), "--background", "--factory-startup",
            "--python", str(RUNNER), "--", str(job_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            log, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"Blender a dépassé {TIMEOUT_S} s : le script est "
                    "trop lourd (trop de géométrie ou de découpes booléennes). Simplifie-le."}
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if not result_path.exists():
            tail = log.decode("utf-8", "replace")[-2500:]
            return {"ok": False, "error": "Blender s'est arrêté sans résultat.\n" + tail}
        return json.loads(result_path.read_text(encoding="utf-8"))


async def live_call(job: dict, timeout: float = TIMEOUT_S) -> dict | None:
    """Envoie un travail au Blender ouvert en mode « en direct » (live.py).
    Renvoie son résultat, ou None si aucun Blender en direct n'est joignable."""
    try:
        info = json.loads(LIVE_INFO.read_text(encoding="utf-8"))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", int(info["port"])), 1.5)
    except (OSError, ValueError, KeyError, asyncio.TimeoutError):
        return None
    try:
        writer.write((json.dumps({"token": info["token"], "job": job}) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        return json.loads(line) if line.strip() else None
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        return None
    finally:
        writer.close()


async def live_available() -> bool:
    result = await live_call({"mode": "ping"}, timeout=3)
    return bool(result and result.get("ok"))


def launch_live(project: Project) -> None:
    """Ouvre Blender avec sa fenêtre, à l'écoute d'ModelForge, sur le projet."""
    command = [find_blender()]
    if project.blend.exists():
        command.append(str(project.blend))
    command += ["--python", str(LIVE_SCRIPT)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


async def run_code(project: Project, code: str, reset: bool = False) -> dict:
    """Exécute du code de modélisation dans la scène du projet : dans le
    Blender ouvert en direct s'il y en a un, sinon dans un Blender invisible."""
    script = project.next_script()
    script.write_text(code, encoding="utf-8")
    job = {
        "mode": "run", "blend": str(project.blend), "glb": str(project.glb),
        "code": str(script), "reset": reset, "preview_dir": str(project.root / "previews"),
    }
    live = await live_call(dict(job))
    if live is None:
        return await _run_job(job)
    if not live.get("ok"):
        return live
    # La scène est construite et enregistrée ; un Blender invisible la mesure,
    # l'exporte et la photographie sans toucher à la fenêtre de l'utilisateur.
    result = await _run_job({**job, "mode": "post"})
    result["stdout"] = live.get("stdout", "")
    result["live"] = True
    return result


async def look(project: Project, position=None, target=None, viewpoint=None,
               fov: float = 60, label: str = "", photo: bool = False) -> dict:
    """Rend une image de la scène depuis un point choisi, sans la modifier.
    photo=True : rendu réaliste (ciel, soleil, ombres) au lieu de l'aperçu."""
    return await _run_job({
        "mode": "look", "photo": photo, "blend": str(project.blend), "position": position, "target": target,
        "viewpoint": viewpoint, "fov": fov, "label": label,
        "preview_dir": str(project.root / "previews"),
    })


async def photo(project: Project, position, target, fov: float, aspect: float,
                at: float = 0.0) -> dict:
    """Rendu réaliste de la scène depuis la caméra de la visionneuse. L'image
    est rangée dans le dossier photos/ du projet."""
    aspect = min(max(float(aspect), 0.5), 2.4)
    width = 1600 if aspect >= 1 else int(1600 * aspect)
    height = int(width / aspect)
    out = project.root / "photos" / f"photo-{int(time.time() * 1000)}.jpg"
    return await _run_job({
        "mode": "photo", "blend": str(project.blend), "position": position, "target": target,
        "fov": min(max(float(fov), 5.0), 120.0), "width": width - width % 2,
        "height": height - height % 2, "out": str(out), "time": max(float(at), 0.0),
    })


async def export_stl(project: Project) -> dict:
    """Exporte le modèle en STL (millimètres) pour l'impression 3D."""
    return await _run_job({"mode": "stl", "blend": str(project.blend),
                           "out": str(project.root / "model.stl")})

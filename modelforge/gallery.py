"""Fabrique la galerie en ligne (dossier docs/, servi par GitHub Pages).

    .venv/bin/python -m modelforge.gallery ID_PROJET["=Titre affiché"] ...

Copie la visionneuse et, pour chaque projet cité, son modèle (.glb), sa vignette
et ce que la page doit savoir de lui (dimensions, points de vue, animation).
La page obtenue est statique : elle se consulte sans Blender ni compte Claude.
Sans argument, la visionneuse et la liste existante sont seulement remises à jour."""

import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from .blender_bridge import Project

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DOCS = ROOT / "docs"
MODELS = DOCS / "models"
INDEX = DOCS / "models.json"


def slug(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:48] or "modele"


def entry(pid: str, title: str | None) -> dict:
    project_dir = ROOT / "projects" / pid
    if not (project_dir / "model.glb").exists():
        raise SystemExit(f"Projet introuvable ou sans modèle : {pid}")
    project = Project(project_dir)
    meta = project.meta()
    report = meta["report"]
    title = title or meta.get("title") or pid
    name = slug(title)
    shutil.copy2(project.glb, MODELS / f"{name}.glb")
    thumb = project_dir / "previews" / "ext_0.jpg"
    if thumb.exists():
        shutil.copy2(thumb, MODELS / f"{name}.jpg")
    return {
        "id": name, "title": title, "file": f"models/{name}.glb",
        "thumb": f"models/{name}.jpg" if thumb.exists() else None,
        "size": report["size"], "objects": report["object_count"],
        "triangles": report["triangles"],
        "animation_seconds": report.get("animation_seconds") or 0,
        "viewpoints": [v for v in report.get("viewpoints", []) if v.get("direction")],
    }


def main(args: list[str]) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").touch()            # GitHub Pages sert les fichiers tels quels
    for name in ("viewer.js", "style.css"):
        shutil.copy2(WEB / name, DOCS / name)
    models = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else []
    for arg in args:
        pid, _, title = arg.partition("=")
        new = entry(pid, title.strip() or None)
        models = [m for m in models if m["id"] != new["id"]] + [new]
        print(f"+ {new['title']}  ({new['objects']} pièces, {new['triangles']} triangles)")
    INDEX.write_text(json.dumps(models, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Galerie : {len(models)} modèle(s) dans {DOCS}")


if __name__ == "__main__":
    main(sys.argv[1:])

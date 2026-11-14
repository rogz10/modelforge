"""Script exécuté DANS Blender (sans interface) par modelforge.

    Blender --background --factory-startup --python runner.py -- job.json

Le fichier job.json décrit le travail :
    mode "run"  : ouvrir la scène du projet, exécuter le code de l'agent,
                  sauvegarder, mesurer, exporter le .glb, rendre des aperçus ;
    mode "post" : la scène vient d'être construite en direct dans un Blender
                  ouvert (voir live.py) : seulement mesurer, exporter, rendre ;
    mode "look" : ouvrir la scène et rendre une vue choisie, sans rien modifier ;
    mode "photo": comme "look", mais en rendu réaliste (ciel, soleil, ombres,
                  reflets) depuis la caméra de la visionneuse.
Le résultat est écrit dans le fichier indiqué par job["result"].
"""

import contextlib
import io
import json
import math
import os
import sys
import traceback

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afkit  # noqa: E402

PREVIEW_SIZE = (720, 480)


def main():
    job_path = sys.argv[sys.argv.index("--") + 1]
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    result = {"ok": False}
    try:
        {"run": run, "post": post, "look": look, "photo": photo, "stl": stl}[job["mode"]](job, result)
    except Exception:
        result["ok"] = False
        result["error"] = traceback.format_exc()
    with open(job["result"], "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def open_scene(job, fresh=False):
    blend = job["blend"]
    if os.path.exists(blend) and not fresh:
        bpy.ops.wm.open_mainfile(filepath=blend)
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    return scene


def exec_code(code):
    """Exécute le code de l'agent dans la scène courante.
    Renvoie (sortie des print, message d'erreur ou None)."""
    namespace = {"__name__": "__main__", "bpy": bpy, "math": math, "Vector": Vector}
    exec("import bmesh, mathutils\nfrom mathutils import Matrix, Euler, Quaternion\n"
         "from math import radians, degrees, sin, cos, tan, pi, sqrt", namespace)
    namespace.update({name: getattr(afkit, name) for name in afkit.__all__})
    out = io.StringIO()
    try:
        compiled = compile(code, "<code>", "exec")
        with contextlib.redirect_stdout(out):
            exec(compiled, namespace)
    except Exception as exc:
        return out.getvalue()[-4000:], describe_error(exc, code)
    bpy.context.scene.frame_set(0)  # une animation se regarde depuis son début
    return out.getvalue()[-4000:], None


def run(job, result):
    open_scene(job, fresh=job.get("reset", False))
    with open(job["code"], encoding="utf-8") as f:
        code = f.read()
    result["stdout"], error = exec_code(code)
    if error:
        result["error"] = error
        return  # la scène n'est PAS sauvegardée : on reste sur le dernier état valide
    bpy.context.view_layer.update()
    bpy.ops.wm.save_as_mainfile(filepath=job["blend"], check_existing=False)
    finish(job, result)


def post(job, result):
    """Après une passe faite en direct dans un Blender ouvert : la scène est
    déjà enregistrée, il reste à la mesurer, l'exporter et la photographier."""
    open_scene(job)
    finish(job, result)


def finish(job, result):
    bpy.context.scene.frame_set(0)
    result["report"] = build_report()
    try:
        bpy.ops.export_scene.gltf(filepath=job["glb"], export_format="GLB",
                                  export_apply=True, export_cameras=False,
                                  export_yup=True, export_animations=True,
                                  export_animation_mode="SCENE",
                                  export_anim_scene_split_object=False)
        result["glb"] = True
    except Exception:
        result["glb"] = False
        result["glb_error"] = traceback.format_exc()[-1500:]
    result["previews"] = render_previews(job["preview_dir"])
    result["ok"] = True


def look(job, result):
    open_scene(job)
    if not afkit._model_objects():
        result["error"] = "La scène est vide : rien à regarder."
        return
    realistic = bool(job.get("photo"))
    cam = make_camera(job.get("fov", 60))
    name = job.get("viewpoint")
    if name:
        source = bpy.data.objects.get(afkit.VIEW_PREFIX + name)
        if source is None:
            names = [o.name[len(afkit.VIEW_PREFIX):] for o in viewpoints()]
            result["error"] = f"Point de vue « {name} » introuvable. Disponibles : {names}"
            return
        cam.matrix_world = source.matrix_world.copy()
        cam.data.angle = source.data.angle
    else:
        afkit.aim(cam, job["position"], job["target"])
    if realistic:
        setup_photo(cam, 1080, 720)
    else:
        setup_preview(cam)
    path = os.path.join(job["preview_dir"], "regard.jpg")
    render(path)
    result["previews"] = [{"label": job.get("label") or name or "Vue demandée", "path": path}]
    result["ok"] = True


def photo(job, result):
    """Rendu réaliste depuis la caméra de la visionneuse (position et cible en
    coordonnées Blender, champ de vision vertical en degrés)."""
    open_scene(job)
    if not afkit._model_objects():
        result["error"] = "La scène est vide : rien à photographier."
        return
    cam = make_camera(60)
    cam.data.sensor_fit = "VERTICAL"
    cam.data.angle = math.radians(job.get("fov", 45))
    afkit.aim(cam, job["position"], job["target"])
    # L'instant de l'animation affiché dans la visionneuse.
    bpy.context.scene.frame_set(int(round(float(job.get("time") or 0) * afkit.FPS)))
    setup_photo(cam, int(job["width"]), int(job["height"]))
    os.makedirs(os.path.dirname(job["out"]), exist_ok=True)
    render(job["out"])
    result["path"] = job["out"]
    result["ok"] = True


def stl(job, result):
    """Export pour l'impression 3D : toutes les pièces dans un fichier STL,
    en millimètres (l'unité qu'attendent les logiciels d'impression)."""
    open_scene(job)
    if not afkit._model_objects():
        result["error"] = "La scène est vide : rien à exporter."
        return
    bpy.context.scene.frame_set(0)
    bpy.ops.wm.stl_export(filepath=job["out"], apply_modifiers=True, global_scale=1000.0,
                          use_scene_unit=False, forward_axis="Y", up_axis="Z")
    result["path"] = job["out"]
    result["ok"] = True


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------

def describe_error(exc, code):
    lines = code.splitlines()
    tb = traceback.extract_tb(exc.__traceback__)
    spots = [fr for fr in tb if fr.filename == "<code>"]
    if isinstance(exc, SyntaxError) and exc.filename == "<code>":
        lineno = exc.lineno or 0
    else:
        lineno = spots[-1].lineno if spots else 0
    text = f"{type(exc).__name__}: {exc}"
    if 0 < lineno <= len(lines):
        text += f"\n  ligne {lineno} de ton code : {lines[lineno - 1].strip()}"
    inner = [fr for fr in tb if fr.filename != "<code>" and "runner.py" not in fr.filename]
    if inner:
        fr = inner[-1]
        text += f"\n  (levée dans {os.path.basename(fr.filename)}:{fr.lineno}, fonction {fr.name})"
    return text


def viewpoints():
    return [o for o in bpy.context.scene.objects
            if o.type == "CAMERA" and o.name.startswith(afkit.VIEW_PREFIX)]


def r(value):
    return round(float(value), 3)


def build_report():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objs = afkit._model_objects()
    lo, hi = afkit.bounds()
    size = hi - lo

    tri_cache = {}

    def triangles(obj):
        key = (obj.data.name, len(obj.modifiers))
        if key not in tri_cache or obj.modifiers:
            try:
                ev = obj.evaluated_get(depsgraph)
                mesh = ev.to_mesh()
                tri_cache[key] = len(mesh.loop_triangles)
                ev.to_mesh_clear()
            except Exception:
                tri_cache[key] = 0
        return tri_cache[key]

    groups = {}
    total = 0
    for obj in objs:
        tris = triangles(obj) if obj.type == "MESH" else 0
        total += tris
        g = groups.setdefault(obj.data.name, {"names": [], "tris": tris, "objs": []})
        g["names"].append(obj.name)
        g["objs"].append(obj)

    items = []
    for g in groups.values():
        glo, ghi = afkit.bounds(g["objs"])
        first = g["objs"][0]
        coll = first.users_collection[0].name if first.users_collection else ""
        items.append({
            "name": g["names"][0] if len(g["names"]) == 1 else g["names"][0].split(".")[0],
            "count": len(g["names"]),
            "collection": coll,
            "one_size": [r(d) for d in first.dimensions],
            "min": [r(v) for v in glo], "max": [r(v) for v in ghi],
            "triangles": g["tris"] * len(g["names"]),
            "materials": [m.name for m in first.data.materials if m] if hasattr(first.data, "materials") else [],
        })
    items.sort(key=lambda it: -(it["max"][0] - it["min"][0]) * (it["max"][1] - it["min"][1] + 1e-6))

    return {
        "object_count": len(objs),
        "size": {"longueur_x": r(size.x), "largeur_y": r(size.y), "hauteur_z": r(size.z)},
        "min": [r(v) for v in lo], "max": [r(v) for v in hi],
        "triangles": total,
        "animation_seconds": round(afkit.animation_length(), 2),
        "items": items,
        "viewpoints": [{"name": o.name[len(afkit.VIEW_PREFIX):],
                        "position": [r(v) for v in o.matrix_world.translation],
                        "direction": [r(v) for v in o.matrix_world.to_3x3() @ Vector((0, 0, -1))],
                        "fov": r(math.degrees(o.data.angle))}
                       for o in viewpoints()],
    }


# --------------------------------------------------------------------------
# Aperçus
# --------------------------------------------------------------------------

STAGE = {}   # décor du rendu réaliste en cours : emprise du modèle, soleil, lampe


def setup_photo(cam, width, height, samples=48, ground=True, raytracing=True, sky=True):
    """Prépare un rendu réaliste (EEVEE) : ciel, soleil avec ombres, sol,
    reflets. Rien n'est enregistré dans la scène du projet."""
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.exposure = -0.4
    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        pass
    for attr, value in (("taa_render_samples", samples), ("use_raytracing", raytracing), ("use_shadows", True)):
        try:
            setattr(scene.eevee, attr, value)
        except Exception:
            pass

    lo, hi = afkit.bounds()
    center = (lo + hi) / 2
    extent = max((hi - lo).length, 0.01)

    # Ciel : lumière d'ambiance bleutée venant de partout.
    world = bpy.data.worlds.new("_photo")
    try:
        world.use_nodes = True
    except Exception:
        pass
    scene.world = world
    nodes, links = world.node_tree.nodes, world.node_tree.links
    background = next((n for n in nodes if n.type == "BACKGROUND"), None)
    if background is not None and not sky:
        # Fond uni : le modèle se détache bien, même vu d'en dessous.
        background.inputs["Color"].default_value = (0.42, 0.50, 0.62, 1.0)
        background.inputs["Strength"].default_value = 0.9
    elif background is not None:
        try:
            sky = nodes.new("ShaderNodeTexSky")
            sky.sky_type = "MULTIPLE_SCATTERING"
            sky.sun_disc = False
            sky.sun_elevation = math.radians(38)
            links.new(sky.outputs[0], background.inputs["Color"])
            background.inputs["Strength"].default_value = 0.17
        except Exception:
            background.inputs["Color"].default_value = (0.55, 0.68, 0.9, 1.0)
            background.inputs["Strength"].default_value = 1.0

    sun_data = bpy.data.lights.new("_soleil", "SUN")
    sun_data.energy = 5.0
    sun_data.angle = math.radians(2.0)
    sun = bpy.data.objects.new("_soleil", sun_data)
    scene.collection.objects.link(sun)

    lamp_data = bpy.data.lights.new("_lampe", "POINT")
    lamp = bpy.data.objects.new("_lampe", lamp_data)
    scene.collection.objects.link(lamp)

    if ground:
        # Sol : un grand plan mat qui reçoit les ombres.
        half = extent * 60
        mesh = bpy.data.meshes.new("_sol")
        z = lo.z - extent * 2e-4
        mesh.from_pydata([(-half, -half, z), (half, -half, z), (half, half, z), (-half, half, z)],
                         [], [(0, 1, 2, 3)])
        plane = bpy.data.objects.new("_sol", mesh)
        plane.location = (center.x, center.y, 0.0)
        mesh.materials.append(afkit.mat("_sol", "#b9b6ae", roughness=0.95))
        scene.collection.objects.link(plane)

    STAGE.update(lo=lo, hi=hi, center=center, extent=extent, sun=sun, lamp=lamp)
    light_for(cam)


def light_for(cam):
    """Règle la lumière pour la caméra donnée : le soleil se place au-dessus,
    derrière l'épaule gauche du photographe, pour que la face visible soit
    éclairée et que les ombres donnent du relief. À l'intérieur du modèle, où
    le soleil n'entre presque pas, une lampe douce éclaire depuis la caméra."""
    lo, hi, center, extent = STAGE["lo"], STAGE["hi"], STAGE["center"], STAGE["extent"]
    bpy.context.view_layer.update()
    rot = cam.matrix_world.to_3x3()
    right, back = rot.col[0], rot.col[2]
    eye = cam.matrix_world.translation
    flat_back = Vector((back.x, back.y, 0.0))
    flat_right = Vector((right.x, right.y, 0.0))
    toward_sun = (flat_back * 0.55 - flat_right * 0.75 + Vector((0, 0, 0.85))).normalized()
    afkit.aim(STAGE["sun"], center + toward_sun * extent * 3, center)

    cam.data.clip_end = max(cam.data.clip_end, extent * 150)
    lamp = STAGE["lamp"]
    if all(lo[i] <= eye[i] <= hi[i] for i in range(3)):
        cam.data.clip_start = min(cam.data.clip_start, 0.02)
        room = max(min(hi - lo), 0.01)
        lamp.data.energy = 9.0 * room * room
        lamp.data.shadow_soft_size = room * 0.15
        lamp.location = eye + rot.col[1] * room * 0.05
    else:
        lamp.data.energy = 0.0


def setup_preview(cam):
    """Images de contrôle de l'agent : même rendu que la photo réaliste, en
    plus léger et sans sol (pour pouvoir regarder le modèle par en dessous).
    Elles montrent donc les vraies matières, la transparence et les couleurs
    peintes, comme la visionneuse."""
    setup_photo(cam, *PREVIEW_SIZE, samples=12, ground=False, raytracing=False, sky=False)
    bpy.context.scene.render.image_settings.quality = 88


def make_camera(fov):
    data = bpy.data.cameras.new("_apercu")
    data.sensor_fit = "HORIZONTAL"
    data.angle = math.radians(fov)
    lo, hi = afkit.bounds()
    extent = max((hi - lo).length, 1.0)
    data.clip_start = max(extent / 5000, 0.01)
    data.clip_end = extent * 20
    cam = bpy.data.objects.new("_apercu", data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam


def render(path):
    bpy.context.view_layer.update()
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def frame(cam, direction, corners, center, fov):
    """Place la caméra dans la direction donnée (depuis le centre) à la
    distance minimale où toute la boîte englobante tient dans l'image."""
    afkit.aim(cam, center + direction.normalized(), center)
    bpy.context.view_layer.update()
    rot = cam.matrix_world.to_3x3()
    right, up, back = rot.col[0], rot.col[1], rot.col[2]
    tan_h = math.tan(math.radians(fov) / 2)
    tan_v = tan_h * PREVIEW_SIZE[1] / PREVIEW_SIZE[0]
    dist = 0.0
    for corner in corners:
        d = corner - center
        depth = d.dot(back)
        dist = max(dist, depth + abs(d.dot(right)) / tan_h, depth + abs(d.dot(up)) / tan_v)
    afkit.aim(cam, center + direction.normalized() * dist * 1.06, center)


def render_previews(folder):
    os.makedirs(folder, exist_ok=True)
    for old in os.listdir(folder):
        os.remove(os.path.join(folder, old))
    if not afkit._model_objects():
        return []
    lo, hi = afkit.bounds()
    center = (lo + hi) / 2
    corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    fov = 40
    cam = make_camera(fov)
    setup_preview(cam)

    shots = [
        ("Trois-quarts avant gauche", Vector((1.0, 0.9, 0.55))),
        ("Côté gauche (avant à gauche de l'image)", Vector((0.0, 1.0, 0.04))),
        ("Dessus (avant à droite de l'image)", Vector((0.0, -0.001, 1.0))),
        ("Trois-quarts arrière droit, vu d'en bas", Vector((-1.0, -0.8, -0.3))),
    ]
    out = []
    for i, (label, direction) in enumerate(shots):
        frame(cam, direction, corners, center, fov)
        light_for(cam)
        path = os.path.join(folder, f"ext_{i}.jpg")
        render(path)
        out.append({"label": label, "path": path})

    for i, view in enumerate(viewpoints()[:3]):
        cam.matrix_world = view.matrix_world.copy()
        cam.data.angle = view.data.angle
        cam.data.clip_start = 0.02
        light_for(cam)
        path = os.path.join(folder, f"vue_{i}.jpg")
        render(path)
        out.append({"label": "Point de vue « %s »" % view.name[len(afkit.VIEW_PREFIX):], "path": path})
    return out

if __name__ == "__main__":
    main()

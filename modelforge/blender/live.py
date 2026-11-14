"""Mode « en direct » : ce script tourne DANS un Blender ouvert avec sa fenêtre.

    Blender [scene.blend] --python live.py

Il écoute sur 127.0.0.1 les travaux envoyés par ModelForge et les exécute dans
la scène affichée : l'utilisateur voit le modèle se construire sous ses yeux.

Blender n'autorise les modifications de scène que depuis son fil principal :
un fil secondaire reçoit les travaux par le réseau et les dépose dans une file,
qu'une minuterie Blender (fil principal) vide plusieurs fois par seconde.
"""

import atexit
import contextlib
import io
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import afkit  # noqa: E402
import runner  # noqa: E402

# Fichier par lequel ModelForge découvre ce Blender (port + jeton secret).
INFO_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)), ".live_blender.json")
TOKEN = secrets.token_hex(24)
jobs: "queue.Queue[tuple[dict, queue.Queue]]" = queue.Queue()


# --------------------------------------------------------------------------
# Affichage
# --------------------------------------------------------------------------

def window():
    wm = bpy.context.window_manager
    return wm.windows[0] if wm.windows else None


def view3d_override():
    """Contexte d'une vue 3D (nécessaire aux opérateurs d'affichage)."""
    win = window()
    if win is None:
        return None
    for area in win.screen.areas:
        if area.type == "VIEW_3D":
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                return {"window": win, "area": area, "region": region}
    return None


def show_materials():
    """Passe les vues 3D en « aperçu des matériaux » : on y voit les vraies
    matières, la transparence et les couleurs peintes, comme dans la
    visionneuse. L'affichage « solide » par défaut ne montre qu'une couleur
    unie par pièce. Si l'utilisateur a choisi un autre affichage, on le garde.
    Les caméras des points de vue sont masquées : leurs traits encombrent la vue."""
    win = window()
    if win is None:
        return
    for area in win.screen.areas:
        if area.type == "VIEW_3D":
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    if space.shading.type == "SOLID":
                        space.shading.type = "MATERIAL"
                    space.overlay.show_extras = False


def frame_all():
    ctx = view3d_override()
    if ctx and afkit._model_objects():
        try:
            with bpy.context.temp_override(**ctx):
                bpy.ops.view3d.view_all(center=False)
        except Exception:
            pass


class Animator:
    """Redessine la fenêtre pendant que le script tourne, pour que les pièces
    apparaissent une à une au lieu de surgir toutes à la fin."""

    def __init__(self):
        self.steps = 0
        self.enabled = True

    def step(self):
        if not self.enabled:
            return
        self.steps += 1
        # Les 200 premières pièces une par une, les suivantes par paquets :
        # un gros modèle (des centaines de sièges) reste rapide à construire.
        if self.steps > 200 and self.steps % 10:
            return
        win = window()
        if win is None:
            return
        try:
            # L'opérateur de rafraîchissement écrit un compte rendu à chaque
            # appel : on le détourne pour qu'il ne se mêle pas aux print de l'agent.
            with contextlib.redirect_stdout(io.StringIO()), \
                    bpy.context.temp_override(window=win):
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
            if self.steps <= 200:
                time.sleep(0.02)
        except Exception:
            self.enabled = False


# --------------------------------------------------------------------------
# Travaux
# --------------------------------------------------------------------------

def same_file(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def op(operator, **kwargs):
    win = window()
    if win is None:
        return operator(**kwargs)
    with bpy.context.temp_override(window=win):
        return operator(**kwargs)


def show_project(blend):
    """Affiche la scène du projet demandé. Renvoie True si la scène affichée a
    changé (nouveau fichier ouvert ou scène vidée)."""
    if same_file(bpy.data.filepath, blend):
        return False
    if os.path.exists(blend):
        op(bpy.ops.wm.open_mainfile, filepath=blend, load_ui=False)
    else:
        afkit.clear()
    return True


def handle(job):
    mode = job.get("mode")
    if mode == "ping":
        return {"ok": True, "file": bpy.data.filepath}
    blend = job["blend"]
    if mode == "open":
        if show_project(blend):
            show_materials()
            frame_all()
        return {"ok": True}
    if mode == "reload":
        # Le fichier du projet a été remplacé (retour à une version antérieure) :
        # s'il est affiché ici, on le relit pour ne pas travailler sur l'ancien état.
        if same_file(bpy.data.filepath, blend) and os.path.exists(blend):
            op(bpy.ops.wm.revert_mainfile)
            show_materials()
        return {"ok": True}
    if mode != "run":
        return {"ok": False, "error": f"mode inconnu : {mode}"}

    changed = show_project(blend)
    if job.get("reset"):
        afkit.clear()
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    show_materials()

    with open(job["code"], encoding="utf-8") as f:
        code = f.read()
    afkit._step_hook = Animator().step
    try:
        stdout, error = runner.exec_code(code)
    finally:
        afkit._step_hook = None

    if error:
        # Comme en mode invisible : après une erreur, on revient au dernier
        # état valide de la scène.
        try:
            if os.path.exists(blend) and same_file(bpy.data.filepath, blend):
                op(bpy.ops.wm.revert_mainfile)
            else:
                afkit.clear()
        except Exception:
            pass
        return {"ok": False, "stdout": stdout, "error": error}

    bpy.context.view_layer.update()
    op(bpy.ops.wm.save_as_mainfile, filepath=blend, check_existing=False)
    if changed or job.get("reset"):
        frame_all()
    return {"ok": True, "stdout": stdout}


def pump():
    """Minuterie du fil principal : exécute les travaux en attente."""
    try:
        job, reply = jobs.get_nowait()
    except queue.Empty:
        return 0.1
    try:
        reply.put(handle(job))
    except Exception:
        reply.put({"ok": False, "error": traceback.format_exc()[-2500:]})
    return 0.02


# --------------------------------------------------------------------------
# Réseau
# --------------------------------------------------------------------------

def serve(server):
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        threading.Thread(target=talk, args=(conn,), daemon=True).start()


def talk(conn):
    with conn:
        try:
            conn.settimeout(10)
            line = conn.makefile("r", encoding="utf-8").readline()
            message = json.loads(line)
            # Le jeton prouve que la demande vient d'ModelForge (qui lit le
            # fichier d'information), pas d'un autre programme ou d'une page web.
            if not secrets.compare_digest(str(message.get("token", "")), TOKEN):
                return
            reply: queue.Queue = queue.Queue()
            jobs.put((message["job"], reply))
            result = reply.get()
            conn.settimeout(30)
            conn.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass


def start():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))  # port libre choisi par le système
    server.listen(4)
    port = server.getsockname()[1]
    fd = os.open(INFO_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": TOKEN, "pid": os.getpid()}, f)

    def cleanup():
        try:
            with open(INFO_PATH) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(INFO_PATH)
        except Exception:
            pass

    atexit.register(cleanup)
    threading.Thread(target=serve, args=(server,), daemon=True).start()
    bpy.app.timers.register(pump, first_interval=0.5, persistent=True)
    bpy.app.timers.register(lambda: show_materials(), first_interval=1.0)
    print(f"ModelForge : Blender en direct, à l'écoute sur 127.0.0.1:{port}")


start()

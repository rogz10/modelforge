"""L'agent modeleur : une session Claude (Claude Agent SDK) par projet, avec
ses outils — poser des questions, modéliser dans Blender, regarder la scène,
ouvrir un nouveau projet.

L'authentification est celle de Claude Code (connexion déjà faite sur la
machine) : aucune clé API n'est nécessaire."""

import asyncio
import base64
import os
import time
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, create_sdk_mcp_server, tool,
)

from . import blender_bridge
from .blender_bridge import Project
from .prompts import system_prompt

MODEL = os.environ.get("MODELFORGE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("MODELFORGE_EFFORT", "medium")
EFFORTS = ("medium", "high")   # « Rapide » et « Soigné » dans l'interface
SERVER = "atelier"

Emit = Callable[[dict], Awaitable[None]]

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array", "minItems": 1, "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "La question, complète et claire."},
                    "options": {
                        "type": "array", "minItems": 2, "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "Réponses proposées, la recommandée en premier.",
                    },
                    "multiple": {"type": "boolean", "description": "true si plusieurs réponses peuvent être cochées."},
                },
                "required": ["question", "options"],
            },
        }
    },
    "required": ["questions"],
}

MODELISER_SCHEMA = {
    "type": "object",
    "properties": {
        "resume": {"type": "string", "description": "Ce que fait cette passe, en quelques mots, affiché à l'utilisateur (ex. « Fuselage et ailes »)."},
        "code": {"type": "string", "description": "Code Python exécuté dans Blender."},
        "titre": {"type": "string", "description": "Nom court de l'objet modélisé (ex. « Airbus A350-900 »), qui devient le titre du projet. À donner au premier appel, et de nouveau si l'objet change."},
        "reset": {"type": "boolean", "description": "true : repartir d'une scène vide avant d'exécuter (script complet). false : modifier la scène existante."},
    },
    "required": ["resume", "code"],
}

REGARDER_SCHEMA = {
    "type": "object",
    "properties": {
        "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "Position de l'œil [x, y, z] en mètres."},
        "cible": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "Point regardé [x, y, z]."},
        "point_de_vue": {"type": "string", "description": "À la place de position/cible : nom d'un point de vue déclaré avec viewpoint()."},
        "champ": {"type": "number", "description": "Champ de vision en degrés (60 par défaut)."},
        "photo": {"type": "boolean", "description": "true : rendu réaliste (ciel, soleil, ombres, reflets), plus lent (environ 10 s). Pour juger les matériaux et l'allure finale."},
    },
}

PROJET_SCHEMA = {
    "type": "object",
    "properties": {
        "titre": {"type": "string", "description": "Nom court du nouvel objet."},
        "demande": {"type": "string", "description": "La demande complète de l'utilisateur pour ce nouvel objet, avec toutes les précisions qu'il a déjà données, rédigée comme s'il l'écrivait lui-même."},
    },
    "required": ["titre", "demande"],
}


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def user_message(text: str, images: list[str]) -> dict:
    """Message utilisateur accompagné d'images de référence (fichiers JPEG)."""
    content: list[dict] = []
    for path in images:
        with open(path, "rb") as f:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(f.read()).decode()}})
    content.append({"type": "text", "text": text})
    return {"type": "user", "message": {"role": "user", "content": content},
            "parent_tool_use_id": None}


def image_block(path: str) -> dict:
    with open(path, "rb") as f:
        return {"type": "image", "data": base64.b64encode(f.read()).decode(), "mimeType": "image/jpeg"}


def format_report(report: dict) -> str:
    size = report["size"]
    lines = [
        f"Dimensions du modèle : longueur (X) {size['longueur_x']} m, largeur (Y) "
        f"{size['largeur_y']} m, hauteur (Z) {size['hauteur_z']} m.",
        f"Emprise : min {report['min']}, max {report['max']}.",
        f"{report['object_count']} objets, {report['triangles']} triangles.",
    ]
    if report["triangles"] > 500_000:
        lines.append("ATTENTION : plus de 500 000 triangles, la visionneuse va ramer. Allège.")
    if report.get("animation_seconds"):
        lines.append(f"Animation : {report['animation_seconds']} s (les images montrent l'instant 0).")
    if report["min"][2] < -0.01:
        lines.append("ATTENTION : une partie du modèle est sous le sol (Z < 0).")
    lines.append("Pièces (nom ×nombre [collection] : taille d'un exemplaire ; emprise min→max ; matériaux) :")
    for item in report["items"][:70]:
        count = f" ×{item['count']}" if item["count"] > 1 else ""
        lines.append(
            f"- {item['name']}{count} [{item['collection']}] : {item['one_size']} ; "
            f"{item['min']}→{item['max']} ; {', '.join(item['materials']) or 'sans matériau'}")
    if len(report["items"]) > 70:
        lines.append(f"... et {len(report['items']) - 70} autres pièces.")
    if report["viewpoints"]:
        lines.append("Points de vue : " + ", ".join(
            f"{v['name']} {v['position']}" for v in report["viewpoints"]))
    else:
        lines.append("Aucun point de vue déclaré.")
    return "\n".join(lines)


class AgentSession:
    """Conversation d'un projet. Les messages de l'utilisateur sont traités un
    par un par une tâche de fond ; tout ce qui se passe est transmis à
    l'interface par `emit`."""

    def __init__(self, project: Project, emit: Emit):
        self.project = project
        self.emit = emit
        self.queue: asyncio.Queue[tuple[str, list[str]]] = asyncio.Queue()
        self.client: ClaudeSDKClient | None = None
        self.busy = False
        saved = project.meta().get("effort")
        self.effort = saved if saved in EFFORTS else EFFORT
        self.worker = asyncio.create_task(self._work())

    # ---- outils --------------------------------------------------------

    def _tools(self):
        project, emit = self.project, self.emit

        @tool("poser_questions",
              "Pose 1 à 4 questions à l'utilisateur pour préciser son besoin, avec des réponses "
              "proposées. Les questions s'affichent dans l'interface ; termine ton tour juste "
              "après, la réponse arrive dans le message suivant.", QUESTIONS_SCHEMA)
        async def poser_questions(args: dict[str, Any]) -> dict:
            await emit({"type": "questions", "questions": args["questions"]})
            return text_result("Questions affichées. Termine ton tour maintenant, sans rien "
                               "modéliser : la réponse de l'utilisateur arrive au message suivant.")

        @tool("modeliser",
              "Exécute du code Python dans Blender pour construire ou modifier le modèle, puis "
              "renvoie un rapport chiffré et des images du résultat (extérieur et points de vue).",
              MODELISER_SCHEMA)
        async def modeliser(args: dict[str, Any]) -> dict:
            started = time.time()
            result = await blender_bridge.run_code(project, args["code"], bool(args.get("reset")))
            if not result.get("ok"):
                await emit({"type": "tool_end", "ok": False})
                out = result.get("stdout") or ""
                return text_result(
                    "Le code a échoué, la scène n'a pas changé.\n" + result.get("error", "")
                    + (f"\nSortie avant l'erreur :\n{out}" if out else ""), is_error=True)
            report = result["report"]
            version = int(time.time() * 1000)
            project.save_meta(report=report, version=version)
            project.snapshot(str(version), str(args.get("resume") or "Modification")[:80], report)
            title = str(args.get("titre") or "").strip()[:70]
            if title and title != project.meta().get("title"):
                project.save_meta(title=title)
                await emit({"type": "title", "title": title})
            previews = result.get("previews", [])
            await emit({
                "type": "model", "version": version, "report": report,
                "previews": [{"label": p["label"], "file": os.path.basename(p["path"])} for p in previews],
                "seconds": round(time.time() - started, 1), "live": bool(result.get("live")),
            })
            content: list[dict] = [{"type": "text", "text": format_report(report)}]
            if result.get("stdout"):
                content.append({"type": "text", "text": "Sortie de tes print :\n" + result["stdout"]})
            if not result.get("glb"):
                content.append({"type": "text", "text": "ATTENTION : l'export pour la visionneuse a "
                                "échoué : " + result.get("glb_error", "")})
            for p in previews:
                content.append({"type": "text", "text": p["label"] + " :"})
                content.append(image_block(p["path"]))
            return {"content": content}

        @tool("regarder",
              "Renvoie une image de la scène actuelle vue depuis une position choisie (ou depuis "
              "un point de vue nommé), sans rien modifier. Sert à contrôler un détail ou l'intérieur.",
              REGARDER_SCHEMA)
        async def regarder(args: dict[str, Any]) -> dict:
            name = args.get("point_de_vue")
            if not name and not (args.get("position") and args.get("cible")):
                return text_result("Donne soit point_de_vue, soit position et cible.", is_error=True)
            result = await blender_bridge.look(
                project, args.get("position"), args.get("cible"), name, args.get("champ") or 60,
                photo=bool(args.get("photo")))
            await emit({"type": "tool_end", "ok": bool(result.get("ok"))})
            if not result.get("ok"):
                return text_result(result.get("error", "Échec du rendu."), is_error=True)
            return {"content": [image_block(result["previews"][0]["path"])]}

        @tool("nouveau_projet",
              "Ouvre un nouveau projet vide pour un objet différent de celui de ce projet, et y "
              "transmet la demande. L'utilisateur y est emmené et la modélisation s'y fait : "
              "termine ton tour juste après, sans rien modéliser ici.", PROJET_SCHEMA)
        async def nouveau_projet(args: dict[str, Any]) -> dict:
            await emit({"type": "new_project", "title": str(args["titre"]).strip()[:70],
                        "prompt": str(args["demande"]).strip()})
            return text_result("Nouveau projet ouvert, la demande y est transmise. Termine ton "
                               "tour maintenant par une phrase courte, sans rien modéliser ici.")

        return [poser_questions, modeliser, regarder, nouveau_projet]

    # ---- conversation --------------------------------------------------

    def _options(self) -> ClaudeAgentOptions:
        tools = self._tools()
        names = [f"mcp__{SERVER}__{t.name}" for t in tools]
        return ClaudeAgentOptions(
            model=MODEL,
            effort=self.effort,
            system_prompt=system_prompt(),
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
            tools=["WebSearch"],            # aucun outil fichier/terminal de Claude Code
            allowed_tools=[*names, "WebSearch"],
            setting_sources=[],             # n'hérite d'aucun réglage ni CLAUDE.md de la machine
            cwd=str(self.project.root),
            resume=self.project.meta().get("session_id"),
            include_partial_messages=True,
            max_turns=60,
        )

    def send(self, text: str, images: list[str] | None = None) -> None:
        self.queue.put_nowait((text, images or []))

    async def set_effort(self, effort: str) -> bool:
        """Change le soin apporté par l'agent (réglage gardé avec le projet).
        Il vaut pour la session suivante : on referme donc la session en
        cours, qui reprendra la conversation là où elle en est."""
        if effort not in EFFORTS or self.busy:
            return False
        if effort != self.effort:
            self.effort = effort
            self.project.save_meta(effort=effort)
            await self._drop_client()
        return True

    async def stop(self) -> None:
        if self.client is not None and self.busy:
            await self.client.interrupt()

    async def close(self) -> None:
        self.worker.cancel()
        try:
            await self.worker
        except (asyncio.CancelledError, Exception):
            pass

    async def _work(self) -> None:
        try:
            while True:
                text, images = await self.queue.get()
                self.busy = True
                await self.emit({"type": "busy", "value": True})
                try:
                    await self._turn(text, images)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # panne de connexion, CLI absente, etc.
                    await self._drop_client()
                    await self.emit({"type": "error", "message": f"{type(exc).__name__} : {exc}"})
                finally:
                    self.busy = False
                    await self.emit({"type": "busy", "value": False})
        finally:
            await self._drop_client()

    async def _drop_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def _context(self) -> str:
        """État du projet, rappelé à l'agent avant chaque message : il sait
        ainsi ce que contient la scène même après une reprise de session."""
        meta = self.project.meta()
        report = meta.get("report")
        if not report or not report.get("object_count"):
            return "[État du projet : vide, aucun modèle pour l'instant.]\n\n"
        size = report["size"]
        notice = ""
        if meta.get("notice"):   # dit une seule fois
            notice = f"[{meta['notice']}]\n\n"
            self.project.save_meta(notice=None)
        return notice + (f"[État du projet : titre « {meta.get('title') or 'sans titre'} » ; il contient "
                f"déjà un modèle de {report['object_count']} objets, {size['longueur_x']} × "
                f"{size['largeur_y']} × {size['hauteur_z']} m.]\n\n")

    async def _turn(self, text: str, images: list[str]) -> None:
        if self.client is None:
            self.client = ClaudeSDKClient(self._options())
            await self.client.connect()
        text = self._context() + text
        if images:
            async def with_images():
                yield user_message(text, images)
            await self.client.query(with_images())
        else:
            await self.client.query(text)
        streamed = False
        failed = False  # une erreur a déjà été affichée pendant ce tour
        async for message in self.client.receive_response():
            if isinstance(message, StreamEvent):
                if message.parent_tool_use_id:
                    continue
                event = message.event
                if event.get("type") == "content_block_start":
                    # Dès que l'agent commence à réfléchir ou à écrire un appel
                    # d'outil, l'interface le montre (un script peut prendre
                    # plusieurs minutes à écrire).
                    block = event.get("content_block", {})
                    if block.get("type") == "thinking":
                        await self.emit({"type": "working", "label": "Réflexion…"})
                    elif block.get("type") == "tool_use":
                        await self.emit({"type": "working", "label": _WRITING.get(
                            block.get("name", "").split("__")[-1], "Préparation…")})
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed = True
                        await self.emit({"type": "text_delta", "text": delta["text"]})
            elif isinstance(message, AssistantMessage):
                if message.error:
                    # Le texte qui accompagne une erreur n'est que son détail
                    # technique : on affiche une seule ligne claire.
                    failed = True
                    detail = " ".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
                    await self.emit({"type": "error", "message": _explain(message.error)
                                     + (f" ({detail})" if detail else "")})
                    continue
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await self.emit({"type": "text", "text": block.text, "streamed": streamed})
                        streamed = False
                    elif isinstance(block, ToolUseBlock):
                        await self.emit(_tool_start(block))
            elif isinstance(message, ResultMessage):
                self.project.save_meta(session_id=message.session_id)
                if message.is_error and not failed and message.subtype != "error_during_execution":
                    detail = message.result or "; ".join(message.errors or []) or message.subtype
                    await self.emit({"type": "error", "message": detail})
                await self.emit({"type": "done", "cost_usd": message.total_cost_usd})


_WRITING = {
    "modeliser": "Écriture du code de modélisation…",
    "regarder": "Contrôle visuel…",
    "poser_questions": "Préparation des questions…",
    "WebSearch": "Recherche sur le web…",
    "nouveau_projet": "Ouverture d'un nouveau projet…",
}


def _tool_start(block: ToolUseBlock) -> dict:
    name = block.name.split("__")[-1]
    if name == "modeliser":
        label = "Blender : " + (block.input.get("resume") or "modélisation")
    elif name == "regarder":
        label = ("Rendu réaliste" if block.input.get("photo") else "Contrôle visuel") + (f" : {block.input['point_de_vue']}" if block.input.get("point_de_vue") else "")
    elif name == "poser_questions":
        label = "Questions"
    elif name == "nouveau_projet":
        label = "Nouveau projet : " + str(block.input.get("titre", ""))
    elif name == "WebSearch":
        label = "Recherche : " + str(block.input.get("query", ""))
    else:
        label = name
    return {"type": "tool_start", "tool": name, "label": label}


def _explain(error: str) -> str:
    return {
        "authentication_failed": "Connexion à Claude refusée. Lance `claude` dans un terminal et connecte-toi (/login).",
        "billing_error": "Problème de facturation ou de forfait sur le compte Claude.",
        "rate_limit": "Limite d'utilisation atteinte. Réessaie dans un moment.",
        "invalid_request": "Requête refusée par l'API.",
        "server_error": "Erreur côté serveur Claude. Réessaie.",
    }.get(error, f"Erreur du modèle : {error}")

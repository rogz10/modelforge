"""Banc d'évaluation de l'agent : une série de demandes fixes, jouées sans
interface, avec des mesures automatiques.

    .venv/bin/python -m modelforge.bench                    # tous les cas
    .venv/bin/python -m modelforge.bench --cases mug,table  # certains cas
    .venv/bin/python -m modelforge.bench --limit 5 --effort high --name essai

Pour chaque cas : un modèle a-t-il été produit, respecte-t-il ce que la demande
impose (dimensions, nombre de pièces, animation, points de vue), en combien de
passes, de temps et pour quel coût. Les résultats vont dans bench/runs/<nom>/
(results.json, results.md, et un projet par cas, modèle et aperçus compris).

Chaque cas lance une vraie session de l'agent : un passage complet consomme du
forfait Claude (ou des crédits API) et dure plusieurs minutes par cas."""

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from . import agent as agent_module
from .agent import AgentSession
from .blender_bridge import Project

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "bench" / "cases.json"
RUNS = ROOT / "bench" / "runs"
TOLERANCE = 0.05   # écart relatif admis sur une dimension demandée
AUTO_ANSWER = ("Choisis toi-même la réponse recommandée à chaque question, puis modélise "
               "sans poser d'autre question.")
MAX_ANSWERS = 2    # au-delà, l'agent tourne en rond : le cas est arrêté


def measure(report: dict, name: str) -> float:
    """Dimension du modèle, indépendante de son orientation au sol."""
    size = report["size"]
    x, y, z = size["longueur_x"], size["largeur_y"], size["hauteur_z"]
    return {"hauteur": z, "longueur": max(x, y), "largeur": min(x, y),
            "plus_grande": max(x, y, z)}[name]


def check(report: dict, checks: dict) -> list[dict]:
    """Compare le modèle aux exigences du cas. Une ligne par exigence."""
    out = []
    for name, wanted in checks.items():
        if name in ("hauteur", "longueur", "largeur", "plus_grande"):
            got = measure(report, name)
            if isinstance(wanted, list):                      # fourchette plausible
                low, high = wanted
                label = f"{name} entre {low:g} et {high:g} m"
            else:                                             # valeur demandée
                tolerance = TOLERANCE
                if isinstance(wanted, dict):
                    wanted, tolerance = wanted["valeur"], wanted["tolerance"]
                low, high = wanted * (1 - tolerance), wanted * (1 + tolerance)
                label = f"{name} {wanted:g} m (± {tolerance:.0%})"
            out.append({"check": label, "got": round(got, 4), "ok": low <= got <= high})
        elif name == "pieces_min":
            got = report["object_count"]
            out.append({"check": f"au moins {wanted} pièces", "got": got, "ok": got >= wanted})
        elif name == "points_de_vue_min":
            got = len(report.get("viewpoints") or [])
            out.append({"check": f"au moins {wanted} point(s) de vue", "got": got, "ok": got >= wanted})
        elif name == "animation":
            got = report.get("animation_seconds") or 0
            out.append({"check": "animation présente", "got": got, "ok": got > 0})
        else:
            raise ValueError(f"exigence inconnue : {name}")
    return out


async def run_case(case: dict, folder: Path, effort: str, timeout: float) -> dict:
    project = Project(folder)
    project.save_meta(effort=effort, title=case["id"])
    stats = {"passes": 0, "attempts": 0, "looks": 0, "questions": 0, "cost_usd": 0.0,
             "errors": [], "asked": False}
    idle = asyncio.Event()

    async def emit(event: dict) -> None:
        kind = event["type"]
        if kind == "tool_start" and event["tool"] == "modeliser":
            stats["attempts"] += 1
        elif kind == "tool_start" and event["tool"] == "regarder":
            stats["looks"] += 1
        elif kind == "model":
            stats["passes"] += 1
        elif kind == "questions":
            stats["questions"] += len(event["questions"])
            stats["asked"] = True
        elif kind == "error":
            stats["errors"].append(event["message"])
        elif kind == "done":
            stats["cost_usd"] += event.get("cost_usd") or 0.0
        elif kind == "busy":
            idle.clear() if event["value"] else idle.set()

    session = AgentSession(project, emit)
    started = time.time()
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            message, answers = case["prompt"], 0
            while True:
                stats["asked"] = False
                idle.clear()
                session.send(message)
                await idle.wait()
                if not stats["asked"] or answers >= MAX_ANSWERS:
                    break
                message, answers = AUTO_ANSWER, answers + 1
    except TimeoutError:
        timed_out = True
        stats["errors"].append(f"arrêté après {timeout:.0f} s")
    finally:
        await session.close()

    report = project.meta().get("report")
    produced = bool(report and report.get("object_count"))
    checks = check(report, case.get("checks", {})) if produced else []
    return {
        "id": case["id"], "prompt": case["prompt"],
        "produced": produced,
        "success": produced and not timed_out and all(c["ok"] for c in checks),
        "checks": checks,
        "passes": stats["passes"],
        "failed_passes": stats["attempts"] - stats["passes"],
        "looks": stats["looks"], "questions": stats["questions"],
        "seconds": round(time.time() - started),
        "cost_usd": round(stats["cost_usd"], 3),
        "objects": report["object_count"] if produced else 0,
        "triangles": report["triangles"] if produced else 0,
        "size": report["size"] if produced else None,
        "errors": stats["errors"],
        "title": project.meta().get("title"),
    }


def summary(results: list[dict]) -> dict:
    n = len(results)
    checks = [c for r in results for c in r["checks"]]
    attempts = sum(r["passes"] + r["failed_passes"] for r in results)
    return {
        "cases": n,
        "produced": sum(r["produced"] for r in results),
        "success": sum(r["success"] for r in results),
        "checks": len(checks), "checks_ok": sum(c["ok"] for c in checks),
        "failed_pass_rate": round(sum(r["failed_passes"] for r in results) / attempts, 3) if attempts else 0,
        "seconds_mean": round(sum(r["seconds"] for r in results) / n) if n else 0,
        "cost_usd_total": round(sum(r["cost_usd"] for r in results), 2),
    }


def markdown(run: dict) -> str:
    s = run["summary"]
    lines = [
        f"# Banc d'évaluation : {run['name']}", "",
        f"Modèle `{run['model']}`, effort `{run['effort']}`, {run['date']}.", "",
        f"- **Cas réussis** : {s['success']} / {s['cases']} (modèle produit et toutes les exigences respectées)",
        f"- **Modèle produit** : {s['produced']} / {s['cases']}",
        f"- **Exigences respectées** : {s['checks_ok']} / {s['checks']}",
        f"- **Passes Blender en échec** : {s['failed_pass_rate']:.0%} des passes (erreur dans le code généré)",
        f"- **Durée moyenne par cas** : {s['seconds_mean']} s",
        f"- **Coût total estimé** : {s['cost_usd_total']} $ (équivalent API, indicatif avec un forfait)", "",
        "| Cas | Réussi | Exigences | Passes | En échec | Contrôles visuels | Questions | Durée | Coût | Pièces |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in run["results"]:
        details = " ; ".join(f"{'✓' if c['ok'] else '✗'} {c['check']} : {c['got']:g}" for c in r["checks"])
        if not r["produced"]:
            details = "aucun modèle" + (f" ({r['errors'][0][:80]})" if r["errors"] else "")
        lines.append(
            f"| {r['id']} | {'oui' if r['success'] else 'non'} | {details or 'aucune'} | {r['passes']} | "
            f"{r['failed_passes']} | {r['looks']} | {r['questions']} | {r['seconds']} s | "
            f"{r['cost_usd']} $ | {r['objects']} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Banc d'évaluation de l'agent ModelForge.")
    parser.add_argument("--cases", help="identifiants séparés par des virgules (défaut : tous)")
    parser.add_argument("--limit", type=int, help="ne jouer que les N premiers cas")
    parser.add_argument("--effort", choices=agent_module.EFFORTS, default="medium")
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"), help="nom du passage")
    parser.add_argument("--timeout", type=float, default=1500, help="durée maximale d'un cas, en secondes")
    args = parser.parse_args()

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.cases:
        wanted = args.cases.split(",")
        unknown = set(wanted) - {c["id"] for c in cases}
        if unknown:
            raise SystemExit(f"Cas inconnus : {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in wanted]
    cases = cases[:args.limit]

    folder = RUNS / args.name
    if folder.exists():
        shutil.rmtree(folder)
    run = {"name": args.name, "date": time.strftime("%Y-%m-%d %H:%M"), "model": agent_module.MODEL,
           "effort": args.effort, "results": []}
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']} : {case['prompt']}", flush=True)
        result = await run_case(case, folder / case["id"], args.effort, args.timeout)
        run["results"].append(result)
        print(f"      {'réussi' if result['success'] else 'ÉCHEC'} · {result['passes']} passe(s) · "
              f"{result['seconds']} s · {result['cost_usd']} $", flush=True)
        for c in result["checks"]:
            print(f"      {'✓' if c['ok'] else '✗'} {c['check']} : {c['got']:g}", flush=True)
        # Écrit après chaque cas : un passage interrompu garde ses résultats.
        run["summary"] = summary(run["results"])
        (folder / "results.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "results.md").write_text(markdown(run), encoding="utf-8")
    s = run["summary"]
    print(f"\n{s['success']} / {s['cases']} cas réussis · exigences {s['checks_ok']} / {s['checks']} · "
          f"{s['cost_usd_total']} $\nRésultats : {folder / 'results.md'}")


if __name__ == "__main__":
    asyncio.run(main())

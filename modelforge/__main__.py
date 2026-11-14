"""Lance ModelForge :  .venv/bin/python -m modelforge  [--port 8765] [--no-browser]"""

import argparse
import threading
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="ModelForge : décris un objet, l'agent le modélise dans Blender.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    print(f"ModelForge : {url}   (Ctrl+C pour arrêter)")
    # 127.0.0.1 uniquement : l'agent exécute du code sur cette machine, le
    # serveur ne doit pas être joignable depuis le réseau.
    uvicorn.run("modelforge.server:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

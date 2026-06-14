"""
main.py  (focal — two-layer)
─────────────────────────────
Entry point for the focal sync tool.

Run:  python main.py [--port PORT] [--debug]
"""

import argparse
import threading
import webbrowser
from focal.app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="focal — Notion PM sync tool")
    parser.add_argument("--port",    type=int, default=8765, help="Port to listen on")
    parser.add_argument("--debug",   action="store_true",    help="Enable Flask debug mode")
    parser.add_argument("--no-open", action="store_true",    help="Don't open browser automatically")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    app = create_app()
    print(f"focal running → {url}")

    if not args.no_open and not args.debug:
        # Open the browser after a short delay so Flask is ready to serve
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="0.0.0.0", port=args.port, debug=args.debug)

"""
__main__.py
───────────
Entry point for `python -m focal`.
Launches the Flask development server and opens the browser automatically.
"""

import threading
import webbrowser

from .app import create_app

HOST = "127.0.0.1"
PORT = 8765


def main() -> None:
    app = create_app()

    def open_browser() -> None:
        import time
        time.sleep(1.2)
        webbrowser.open(f"http://{HOST}:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()

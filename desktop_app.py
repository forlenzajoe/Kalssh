"""Desktop launcher — runs the scanner dashboard as a native window.

Double-click the desktop shortcut (or run ``python desktop_app.py``) and the
Streamlit dashboard opens in its own application window — no browser, no
localhost URL to type. Under the hood it starts a local Streamlit server bound
to 127.0.0.1 and shows it in an embedded Edge WebView (built into Windows 11).

If the native window can't start for any reason, it falls back to opening your
default browser so you're never stuck.
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "src" / "dashboard" / "app.py"


def _venv_python() -> str:
    """Return the interpreter to launch Streamlit with (prefer this venv)."""
    here = Path(sys.executable)
    candidate = here.with_name("python.exe")          # sibling of pythonw.exe
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(port: int, timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _start_streamlit(port: int) -> subprocess.Popen:
    cmd = [
        _venv_python(), "-m", "streamlit", "run", str(APP),
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    # Hide the console window on Windows.
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen(cmd, cwd=str(ROOT), creationflags=flags)


def main() -> int:
    port = _free_port()
    proc = _start_streamlit(port)
    atexit.register(lambda: proc.terminate())

    if not _wait_until_ready(port):
        print("Streamlit did not start in time. Check the logs.", file=sys.stderr)
        proc.terminate()
        return 1

    url = f"http://127.0.0.1:{port}"
    try:
        import webview  # pywebview

        window = webview.create_window(
            "Kalshi Weather Scanner", url, width=1480, height=940, min_size=(1000, 700)
        )

        def _cleanup():
            try:
                proc.terminate()
            except Exception:
                pass

        window.events.closed += _cleanup
        webview.start()
    except Exception as exc:  # pragma: no cover - GUI/environment dependent
        # Fallback: open in the default browser.
        print(f"Native window unavailable ({exc}); opening in your browser: {url}")
        import webbrowser

        webbrowser.open(url)
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

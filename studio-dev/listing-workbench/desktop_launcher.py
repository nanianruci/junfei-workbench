"""Desktop entry point, with a private worker entry for isolated accounts."""
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
import portable_support
from runtime_environment import workbench_port
if portable_support.handle_cli(BASE):
    raise SystemExit(0)
PORTABLE = portable_support.configure(BASE)
os.chdir(BASE)
os.environ.setdefault('JUNFEI_ACCOUNT_DATA', str(Path.home() / 'AmazonListingWorkbenchData'))
log_dir = Path(os.environ['JUNFEI_ACCOUNT_DATA'])
log_dir.mkdir(parents=True, exist_ok=True)
# Windowed PyInstaller executables have no stderr. HTTP logging must have a stream.
if sys.stdout is None:
    sys.stdout = open(log_dir / 'desktop.log', 'a', encoding='utf-8', buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout

if '--account-runtime' in sys.argv:
    import account_runtime
    raise SystemExit(0)

PORT = workbench_port()
URL = f'http://127.0.0.1:{PORT}'
OPEN_BROWSER = os.environ.get('JUNFEI_OPEN_BROWSER', '1') != '0'
def ready():
    try:
        with urlopen(URL + '/api/desktop-health', timeout=2) as response:
            return response.read() == b'{"app": "amazon-listing-workbench"}'
    except Exception:
        return False

if ready():
    if PORTABLE:
        print('Port already in use; portable app will not reuse an unidentified instance', flush=True)
        raise SystemExit(2)
    if os.environ.get('JUNFEI_LOCAL_MODE') == '1':
        with urlopen(URL + '/api/auth/status', timeout=2) as response:
            if not json.load(response).get('localMode'):
                print('Port already in use by a workbench in team mode.', flush=True)
                raise SystemExit(2)
    if OPEN_BROWSER: webbrowser.open(URL)
    raise SystemExit(0)

from http.server import ThreadingHTTPServer
import server
http = ThreadingHTTPServer(('127.0.0.1', PORT), server.Handler)
server.studio_bridge.restart_tasks()
server.visual_studio.start_worker()
server.colour_sets.start_worker()
server.local_colour_tasks.start_worker()
server.start_supplier_workers()
def open_ui():
    for _ in range(60):
        if ready():
            if OPEN_BROWSER: webbrowser.open(URL)
            return
        time.sleep(.25)
    print('Desktop readiness timed out; inspect desktop.log', flush=True)
threading.Thread(target=open_ui, daemon=True).start()
http.serve_forever()

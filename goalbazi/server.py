"""Railway root entrypoint.

Railway may start the service from the repository root with `gunicorn server:app`.
The real Flask app lives in goalbazi/server.py, so this file enters that folder
first and then exposes the same `app` object.
"""

import os
import sys
import importlib.util

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goalbazi")
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

spec = importlib.util.spec_from_file_location("goalbazi_app", os.path.join(APP_DIR, "server.py"))
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

app = module.app

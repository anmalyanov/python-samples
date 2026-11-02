"""Запуск: ``python -m timetracker`` (из каталога time_tracker)."""

import os

from . import create_app

app = create_app()
app.run(
    host=os.environ.get("TIMETRACKER_HOST", "127.0.0.1"),
    port=int(os.environ.get("TIMETRACKER_PORT", "5000")),
    debug=os.environ.get("TIMETRACKER_DEBUG") == "1",
)

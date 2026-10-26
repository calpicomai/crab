"""Self-contained local operations UI. Assets are inlined; no frontend service/dependencies."""
from pathlib import Path
from shared import ACTION_PATHS, Action, COMPANION_EVENTS_PATH
from shared.clock import MAX_ANCHOR_AGE_S, MAX_RTT_S, MAX_OFFSET_S, WALL_STEP_S, DRIFT_S_PER_S

_ASSETS = Path(__file__).with_name("dashboard")
DASHBOARD_HTML = (_ASSETS / "page.html").read_text().replace("__STYLES__", (_ASSETS / "style.css").read_text()).replace("__SCRIPT__", (_ASSETS / "app.js").read_text())
DASHBOARD_HTML = DASHBOARD_HTML.replace("__EVENT_PATH__", COMPANION_EVENTS_PATH).replace("__STOP_PATH__", ACTION_PATHS[Action.STOP])
for _name, _value in (("MAX_AGE_MS", MAX_ANCHOR_AGE_S*1000), ("MAX_RTT_MS", MAX_RTT_S*1000),
                      ("MAX_OFFSET_MS", MAX_OFFSET_S*1000), ("WALL_STEP_MS", WALL_STEP_S*1000),
                      ("DRIFT", DRIFT_S_PER_S)):
    DASHBOARD_HTML = DASHBOARD_HTML.replace("__CLOCK_"+_name+"__", str(_value))

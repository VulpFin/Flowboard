# Copyright (C) 2025 TG11
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os, json, datetime as dt
from typing import Any, Dict, List

LOG_PATH = os.getenv("FLOWBOARD_AI_LOG", "./flowboard_ai_log.jsonl")


def _ts():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enabled() -> bool:
    # Enable logging when FLOWBOARD_AI_LOG is set to a path (default given above)
    return bool(LOG_PATH)


def write(entry: Dict[str, Any]) -> None:
    if not enabled():
        return
    entry = {"time": _ts(), **entry}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_recent(limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(LOG_PATH):
        return []
    out: List[Dict[str, Any]] = []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out[-limit:]

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

import json, os, uuid
from typing import Dict, List
from .models import Task

DB_PATH = os.getenv("FLOWBOARD_DB", "./flowboard_tasks.json")


def _load() -> Dict[str, Task]:
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {tid: Task(**t) for tid, t in raw.items()}


def _save(tasks: Dict[str, Task]) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {k: t.model_dump() for k, t in tasks.items()},
            f,
            indent=2,
            ensure_ascii=False,
        )


def list_tasks(include_done=False) -> List[Task]:
    tasks = _load()
    values = list(tasks.values())
    return values if include_done else [t for t in values if not t.done]


def get_all() -> Dict[str, Task]:
    return _load()


def put(task: Task):
    tasks = _load()
    tasks[task.id] = task
    _save(tasks)


def delete(task_id: str):
    tasks = _load()
    if task_id in tasks:
        del tasks[task_id]
        _save(tasks)


def mark_done(task_id: str, done=True):
    tasks = _load()
    if task_id in tasks:
        t = tasks[task_id]
        t.done = done
        tasks[task_id] = t
        _save(tasks)


def new_id() -> str:
    return str(uuid.uuid4())[:8]


def clear_all():
    # Overwrite the DB with an empty dict (safer than delete)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f)

from __future__ import annotations
from typing import Optional, Dict, Any, Iterable, List
from datetime import datetime
from pathlib import Path
import uuid

from app.storage.base import Storage, Task
from app.settings import settings

# import the real vfdb
import vfdb

TASK_TABLE = "tasks"

SCHEMA = f"""
CREATE TABLE {TASK_TABLE} (
  id TEXT,
  title TEXT,
  notes TEXT,
  context TEXT,
  tags TEXT,
  estimate_min INT,
  due TEXT,
  done TEXT,
  created_at TEXT,
  updated_at TEXT,
  priority INT
);
"""

# If VFDB supports full-text or trigram: do that here
# e.g., CREATE INDEX ... ON tasks USING FTS(title, notes, tags);

class VFDBStorage(Storage):
    def __init__(self, path: Path = settings.VFDB_PATH):
        self.path = Path(path)
        self.db = None

    def init(self) -> None:
        self.db = vfdb.connect(str(self.path))  # adjust if vfdb uses db = vfdb.open(...)
        with self.db.transaction():
            for stmt in SCHEMA.split(";\n"):
                s = stmt.strip()
                if s:
                    self.db.execute(s + ";")

    def _row_to_task(self, row) -> Task:
        # Map VFDB row to dataclass; adjust getters depending on row type
        return Task(
            id=row["id"],
            title=row["title"],
            notes=row.get("notes",""),
            context=row.get("context","inbox"),
            tags=row.get("tags") or [],
            estimate_min=int(row.get("estimate_min") or 0),
            due=row.get("due"),
            done=bool(row.get("done") or False),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            priority=int(row.get("priority") or 0),
        )

    def create_task(self, t: Task) -> Task:
        now = datetime.utcnow()
        if not t.id:
            t.id = uuid.uuid4().hex
        t.created_at = now
        t.updated_at = now
        self.db.execute(
            f"""INSERT INTO {TASK_TABLE}
               (id, title, notes, context, tags, estimate_min, due, done, created_at, updated_at, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [t.id, t.title, t.notes, t.context, t.tags, t.estimate_min, t.due, t.done, t.created_at, t.updated_at, t.priority]
        )
        return t

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self.db.fetchone(f"SELECT * FROM {TASK_TABLE} WHERE id = ?", [task_id])
        return self._row_to_task(row) if row else None

    def update_task(self, task_id: str, patch: Dict[str, Any]) -> Optional[Task]:
        # build dynamic SET
        if not patch:
            return self.get_task(task_id)
        patch = dict(patch)
        patch["updated_at"] = datetime.utcnow()
        cols = ", ".join(f"{k} = ?" for k in patch.keys())
        vals = list(patch.values()) + [task_id]
        self.db.execute(f"UPDATE {TASK_TABLE} SET {cols} WHERE id = ?", vals)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> bool:
        self.db.execute(f"DELETE FROM {TASK_TABLE} WHERE id = ?", [task_id])
        return True

    def list_tasks(self, ctx: Optional[str]=None, include_done: bool=False) -> Iterable[Task]:
        if ctx and include_done:
            rows = self.db.fetchall(f"SELECT * FROM {TASK_TABLE} WHERE context = ? ORDER BY priority DESC, due NULLS LAST, created_at ASC", [ctx])
        elif ctx:
            rows = self.db.fetchall(f"SELECT * FROM {TASK_TABLE} WHERE context = ? AND done = FALSE ORDER BY priority DESC, due NULLS LAST, created_at ASC", [ctx])
        elif include_done:
            rows = self.db.fetchall(f"SELECT * FROM {TASK_TABLE} ORDER BY priority DESC, due NULLS LAST, created_at ASC")
        else:
            rows = self.db.fetchall(f"SELECT * FROM {TASK_TABLE} WHERE done = FALSE ORDER BY priority DESC, due NULLS LAST, created_at ASC")
        for r in rows:
            yield self._row_to_task(r)

    def search(self, q: str, limit: int=100) -> List[Task]:
        # Portable fallback: title/notes/tags ILIKE-ish
        rows = self.db.fetchall(
            f"""SELECT * FROM {TASK_TABLE}
                WHERE title LIKE ? OR notes LIKE ? OR (tags IS NOT NULL AND ANY_MATCH(tags, ?))
                ORDER BY priority DESC, due NULLS LAST, created_at ASC
                LIMIT ?""",
            [f"%{q}%", f"%{q}%", q, limit]
        )
        return [self._row_to_task(r) for r in rows]

    def vfql(self, q: str, limit: int=200) -> List[Task]:
        # If VFDB exposes a VFQL API, route directly; else treat q as WHERE clause
        # Example 1: direct VFQL
        # rows = self.db.vfql(f"SELECT * FROM tasks WHERE {q} LIMIT {limit}")
        # Example 2: pretend VFQL==SQL-ish
        rows = self.db.fetchall(f"SELECT * FROM {TASK_TABLE} WHERE {q} LIMIT {limit}")
        return [self._row_to_task(r) for r in rows]

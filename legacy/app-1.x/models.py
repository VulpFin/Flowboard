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

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    estimate_min: int = Field(30, ge=1, le=1440)
    importance: int = Field(3, ge=1, le=5)
    due: Optional[str] = None  # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"
    contexts: List[str] = Field(default_factory=list)
    energy: str = "medium"  # low|medium|high
    depends_on: List[str] = Field(default_factory=list)
    created: str = Field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    done: bool = False
    manual_order: int = 0  # user drag order within primary context
    last_actual_min: Optional[int] = None  # from Focus Mode logging
    completions: int = 0  # how many times you’ve done similar tasks (for pacing)

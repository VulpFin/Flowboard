# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""ORM models.  Importing this package registers every table on `Base`."""
from .common import utcnow, new_uuid  # noqa: F401
from .user import User, UserProfile, IdentityLink, PasswordResetToken, UserSession  # noqa: F401
from .board import Board, BoardMembership, BoardRole  # noqa: F401
from .task import Task, TaskActivity, TaskReflection  # noqa: F401
from .ai import AIProviderCredential, AIUsageRecord, AIChangeSet  # noqa: F401
from .calendar import CalendarConnection, CalendarEventLink, CalendarFeedToken  # noqa: F401

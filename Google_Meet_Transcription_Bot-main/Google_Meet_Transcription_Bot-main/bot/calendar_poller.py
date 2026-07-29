"""
Legacy single-account calendar poller — superseded by the per-user
core/calendar_sync_service.py + core/auto_join_scheduler.py pipeline.

Kept for backward compatibility. The new system is started automatically
from main.py lifespan and handles multiple users via the database.
"""

# No-op stub so any existing imports don't break
async def calendar_poll_loop(*args, **kwargs):
    pass

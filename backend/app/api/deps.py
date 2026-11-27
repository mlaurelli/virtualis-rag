"""Dipendenze FastAPI condivise tra i router (re-export comodi)."""

from ..auth import get_current_user, require_admin
from ..db import get_db

__all__ = ["get_current_user", "get_db", "require_admin"]

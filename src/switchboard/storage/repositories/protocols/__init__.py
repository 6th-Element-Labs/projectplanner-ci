"""Repository Protocols — application depends on these; SQL stays behind them."""
from .access import AccessRepository
from .claims import ClaimsRepository
from .tasks import TaskRepository

__all__ = ["AccessRepository", "ClaimsRepository", "TaskRepository"]

"""Request/response models for the auth service (ActionEngine-style contracts)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RegisterBody(BaseModel):
    email: str
    display_name: str = ""
    password: str = Field(..., min_length=8)


class LoginBody(BaseModel):
    email: str
    password: str
    remember_me: bool = False


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


def public_user(account: Dict[str, Any], projects: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Shape a user account for the client — never leaks the password hash."""
    out = {
        "id": account.get("id"),
        "email": account.get("email"),
        "display_name": account.get("display_name"),
        "is_superadmin": bool(account.get("is_superadmin")),
        "status": account.get("status") or "active",
        "last_login": account.get("last_login"),
    }
    if projects is not None:
        out["projects"] = projects
    return out

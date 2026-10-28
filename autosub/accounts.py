"""Sign-up, sign-in and guest users. Used by the website and the desktop app.

Nobody has to register to try the tool: the first upload creates a *guest* user
(a row in `users` without e-mail or password). Signing up or in later attaches
that guest's projects to the real account, so nothing is lost.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from .repository import Repository

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


class AccountError(ValueError):
    """A sign-up or sign-in problem, worded for the person who has to fix it."""


def _public(user: dict | None) -> dict | None:
    if user is None:
        return None
    return {k: user[k] for k in ("id", "email", "display_name", "is_guest")}


class Accounts:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._dummy_hash: str | None = None

    # ---- guests ------------------------------------------------------
    def create_guest(self) -> dict:
        user_id = uuid.uuid4().hex
        self.repo.create_user(user_id=user_id, display_name="Guest", is_guest=True)
        return _public(self.repo.get_user(user_id))  # type: ignore[return-value]

    def get(self, user_id: str | None) -> dict | None:
        return _public(self.repo.get_user(user_id)) if user_id else None

    def local_user(self, data_dir: Path) -> dict:
        """The one guest user the desktop app uses. Its id is remembered in a small file."""
        data_dir.mkdir(parents=True, exist_ok=True)
        marker = data_dir / "desktop_user.txt"
        if marker.exists():
            user = self.get(marker.read_text(encoding="utf-8").strip())
            if user:
                return user
        user = self.create_guest()
        marker.write_text(user["id"], encoding="utf-8")
        return user

    # ---- registered users -------------------------------------------
    @staticmethod
    def _check_email(email: str) -> str:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 255:
            raise AccountError("Enter a valid e-mail address.")
        return email

    def register(self, name: str, email: str, password: str, guest_id: str | None = None) -> dict:
        name = " ".join((name or "").split())
        if not 1 <= len(name) <= 80:
            raise AccountError("Enter your name (up to 80 characters).")
        email = self._check_email(email)
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Use a password with at least {MIN_PASSWORD} characters.")
        if len(password) > 200:
            raise AccountError("That password is too long.")
        if self.repo.get_user_by_email(email):
            raise AccountError("An account with this e-mail already exists. Sign in instead.")

        pw_hash = generate_password_hash(password)
        guest = self.repo.get_user(guest_id) if guest_id else None
        try:
            if guest and guest["is_guest"]:
                # keep the projects the visitor already made
                self.repo.promote_guest(guest["id"], email=email, display_name=name, password_hash=pw_hash)
                user_id = guest["id"]
            else:
                user_id = uuid.uuid4().hex
                self.repo.create_user(
                    user_id=user_id, display_name=name, email=email, password_hash=pw_hash, is_guest=False
                )
        except Exception:
            if self.repo.get_user_by_email(email):  # lost a race with another sign-up
                raise AccountError("An account with this e-mail already exists. Sign in instead.") from None
            raise
        return _public(self.repo.get_user(user_id))  # type: ignore[return-value]

    def login(self, email: str, password: str, guest_id: str | None = None) -> dict:
        email = (email or "").strip().lower()
        user = self.repo.get_user_by_email(email) if email else None
        if user is None or not user.get("password_hash"):
            # Spend the same time as a real check so the answer does not reveal which e-mails exist.
            if self._dummy_hash is None:
                self._dummy_hash = generate_password_hash("not-a-real-password")
            check_password_hash(self._dummy_hash, password or "")
            raise AccountError("The e-mail or password is not correct.")
        if not check_password_hash(user["password_hash"], password or ""):
            raise AccountError("The e-mail or password is not correct.")

        if guest_id and guest_id != user["id"]:
            guest = self.repo.get_user(guest_id)
            if guest and guest["is_guest"]:
                self.repo.adopt_jobs(guest["id"], user["id"])
                self.repo.delete_user(guest["id"])
        return _public(user)  # type: ignore[return-value]

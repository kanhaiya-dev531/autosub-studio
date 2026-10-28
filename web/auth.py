"""Sign up, sign in, sign out."""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, session, url_for

from autosub.accounts import AccountError

from .security import ext

bp = Blueprint("auth", __name__)


def _next_url() -> str:
    target = request.values.get("next", "")
    # only ever redirect to a path on this site
    return target if target.startswith("/") and not target.startswith("//") else url_for("pages.dashboard")


def _sign_in(user: dict) -> None:
    csrf = session.get("csrf")
    session.clear()  # new session id for the new identity
    session["uid"] = user["id"]
    session["csrf"] = csrf or ""
    session.permanent = True


@bp.route("/register", methods=["GET", "POST"])
def register():
    error, form = None, {"name": "", "email": ""}
    if request.method == "POST":
        form = {"name": request.form.get("name", ""), "email": request.form.get("email", "")}
        try:
            user = ext().accounts.register(
                form["name"], form["email"], request.form.get("password", ""), guest_id=session.get("uid")
            )
        except AccountError as exc:
            error = str(exc)
        else:
            _sign_in(user)
            return redirect(_next_url())
    return render_template("register.html", error=error, form=form, next=request.values.get("next", "")), 400 if error else 200


@bp.route("/login", methods=["GET", "POST"])
def login():
    error, email = None, ""
    if request.method == "POST":
        email = request.form.get("email", "")
        key = request.remote_addr or "unknown"
        limiter = ext().limiter
        if limiter.blocked(key):
            error = "Too many attempts. Wait a few minutes and try again."
        else:
            try:
                user = ext().accounts.login(email, request.form.get("password", ""), guest_id=session.get("uid"))
            except AccountError as exc:
                limiter.fail(key)
                error = str(exc)
            else:
                limiter.reset(key)
                _sign_in(user)
                return redirect(_next_url())
    status = 429 if error and error.startswith("Too many") else (401 if error else 200)
    return render_template("login.html", error=error, email=email, next=request.values.get("next", "")), status


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("pages.index"))

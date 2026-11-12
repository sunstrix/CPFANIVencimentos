"""
core/routes_public.py — Rotas públicas (login, logout e raiz).

Blueprint "public": o endpoint "login" é o alvo de auth.LOGIN_ENDPOINT.

Segurança:
  * POST /login com rate limit (LIMITE_LOGIN) e CSRF (CSRFProtect global).
  * Mensagem de erro genérica (não revela se o usuário existe).
  * Parâmetro "next" validado por auth.destino_pos_login (anti open redirect).
  * Logout apenas por POST (com CSRF), para não ser acionado por link/imagem.
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import auth

logger = logging.getLogger(__name__)

bp = Blueprint("public", __name__)


def _destino_padrao(papel: str) -> str:
    if papel == auth.PAPEL_ESCRITORIO:
        return url_for("escritorio.dashboard")
    return url_for("loja.lancamento")


def _quer_json() -> bool:
    return request.path.startswith("/api/") or (
        request.accept_mimetypes.best == "application/json"
    )


@bp.route("/", methods=["GET"])
def raiz():
    u = auth.usuario_atual()
    if u is None:
        return redirect(url_for("public.login"))
    return redirect(_destino_padrao(u["papel"]))


@bp.route("/login", methods=["GET", "POST"])
@auth.limiter.limit(auth.LIMITE_LOGIN, methods=["POST"])
def login():
    u = auth.usuario_atual()
    if u is not None:
        return redirect(_destino_padrao(u["papel"]))

    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        senha = request.form.get("senha", "")
        if auth.login_sessao(usuario, senha):
            perfil = auth.usuario_atual()
            proximo = auth.destino_pos_login(
                request.args.get("next") or request.form.get("next"),
                _destino_padrao(perfil["papel"]),
            )
            logger.info("[auth] login ok: %s (%s)", perfil["usuario"], perfil["papel"])
            if _quer_json():
                return jsonify({"ok": True, "destino": proximo})
            return redirect(proximo)

        logger.warning("[auth] tentativa de login inválida para %r", usuario)
        if _quer_json():
            return jsonify({"erro": "Usuário ou senha inválidos."}), 401
        flash("Usuário ou senha inválidos.", "erro")
        return render_template("login.html", usuario=usuario), 401

    return render_template("login.html", usuario="")


@bp.route("/logout", methods=["POST"])
def logout():
    auth.logout_sessao()
    if _quer_json():
        return jsonify({"ok": True})
    flash("Sessão encerrada.", "info")
    return redirect(url_for("public.login"))
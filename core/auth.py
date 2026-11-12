"""
core/auth.py — Autenticação, sessão e controle de acesso.

Decisões de negócio refletidas:
  * Login das lojas = código BPCS (6a); escritório = usuário "escritorio".
  * Senhas com hash Werkzeug (AUTH_USERS_JSON no .env / ambiente).
  * A sessão identifica automaticamente a loja e o BPCS correspondentes.

Segurança:
  * usuario_atual() NÃO confia nos dados brutos da sessão: re-resolve a loja
    pelo catálogo e confere que o usuário bate com o BPCS/papel.
  * Usuário inexistente ainda roda um check_password_hash dummy (mitiga
    enumeração de logins por tempo de resposta).
  * Decoradores por papel retornam 401 (não autenticado) / 403 (proibido),
    em JSON para /api/* e redirect/abort para páginas HTML.
  * destino_pos_login() evita open redirect (só aceita caminho relativo).

Rate limit:
  * O objeto `limiter` é criado aqui; o app factory chama inicializar_auth(app).
  * A rota de login aplica LIMITE_LOGIN (Config.LOGIN_RATE_LIMIT).
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from flask import abort, jsonify, redirect, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from .lojas import USUARIO_ESCRITORIO, obter_loja

logger = logging.getLogger(__name__)

# Papéis e chaves de sessão.
PAPEL_LOJA = "loja"
PAPEL_ESCRITORIO = "escritorio"
SESSION_USUARIO = "usuario"
SESSION_PAPEL = "papel"
SESSION_BPCS = "loja_bpcs"
SESSION_NOME_LOJA = "nome_loja"

# Endpoint de login (blueprint "public" em routes_public.py).
LOGIN_ENDPOINT = "public.login"

# Rate limiter (inicializado no app factory). Sem limites globais por padrão.
limiter = Limiter(get_remote_address, default_limits=[])
LIMITE_LOGIN = Config.LOGIN_RATE_LIMIT

_HASH_DUMMY = None


def _hash_dummy() -> str:
    """Hash descartável só para equalizar o tempo de usuário inexistente."""
    global _HASH_DUMMY
    if _HASH_DUMMY is None:
        _HASH_DUMMY = generate_password_hash("cpfani-dummy-nao-usar")
    return _HASH_DUMMY


def normalizar_usuario(usuario: object) -> str:
    return "" if usuario is None else str(usuario).strip().lower()


def autenticar(usuario: object, senha: object) -> dict | None:
    """Valida credenciais e devolve os dados do perfil, ou None."""
    usuario = normalizar_usuario(usuario)
    senha = "" if senha is None else str(senha)
    if not usuario or not senha:
        return None

    hash_senha = Config.AUTH_USERS.get(usuario)
    if not hash_senha:
        # Equaliza o tempo mesmo quando o usuário não existe.
        check_password_hash(_hash_dummy(), senha)
        return None

    try:
        valido = check_password_hash(hash_senha, senha)
    except ValueError:
        logger.error("[auth] hash inválido para o usuário %s", usuario)
        return None
    if not valido:
        return None

    if usuario == USUARIO_ESCRITORIO:
        return {
            "usuario": USUARIO_ESCRITORIO,
            "papel": PAPEL_ESCRITORIO,
            "loja_bpcs": None,
            "nome_loja": "Escritório",
        }

    loja = obter_loja(usuario)
    if loja is None:
        logger.warning(
            "[auth] usuário %s tem hash mas não é loja nem escritório.", usuario
        )
        return None
    return {
        "usuario": loja.bpcs,
        "papel": PAPEL_LOJA,
        "loja_bpcs": loja.bpcs,
        "nome_loja": loja.nome,
    }


def login_sessao(usuario: object, senha: object) -> bool:
    """Autentica e grava a sessão. Retorna True em sucesso."""
    dados = autenticar(usuario, senha)
    if dados is None:
        return False
    session.clear()
    session[SESSION_USUARIO] = dados["usuario"]
    session[SESSION_PAPEL] = dados["papel"]
    session[SESSION_BPCS] = dados["loja_bpcs"]
    session[SESSION_NOME_LOJA] = dados["nome_loja"]
    session.permanent = True
    return True


def logout_sessao() -> None:
    session.clear()


def usuario_atual() -> dict | None:
    """Perfil atual, re-resolvido pelo catálogo (não confia na sessão bruta)."""
    usuario = session.get(SESSION_USUARIO)
    papel = session.get(SESSION_PAPEL)
    if not usuario or papel not in (PAPEL_LOJA, PAPEL_ESCRITORIO):
        return None

    if papel == PAPEL_ESCRITORIO:
        if normalizar_usuario(usuario) != USUARIO_ESCRITORIO:
            return None
        return {
            "usuario": USUARIO_ESCRITORIO,
            "papel": PAPEL_ESCRITORIO,
            "loja_bpcs": None,
            "nome_loja": "Escritório",
        }

    loja = obter_loja(session.get(SESSION_BPCS))
    if loja is None or normalizar_usuario(usuario) != loja.bpcs:
        return None
    return {
        "usuario": loja.bpcs,
        "papel": PAPEL_LOJA,
        "loja_bpcs": loja.bpcs,
        "nome_loja": loja.nome,
    }


def esta_logado() -> bool:
    return usuario_atual() is not None


def papel_atual() -> str | None:
    u = usuario_atual()
    return u["papel"] if u else None


def loja_atual_bpcs() -> str | None:
    u = usuario_atual()
    return u["loja_bpcs"] if u and u["papel"] == PAPEL_LOJA else None


def inicializar_auth(app) -> None:
    """Liga o rate limiter ao app (chamado no app factory)."""
    limiter.init_app(app)


# --- Respostas de erro / decoradores ---
def _quer_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best == "application/json"


def _nao_autenticado():
    if _quer_json():
        return jsonify({"erro": "Não autenticado."}), 401
    return redirect(url_for(LOGIN_ENDPOINT, next=request.path))


def _proibido():
    if _quer_json():
        return jsonify({"erro": "Acesso negado para este perfil."}), 403
    abort(403)


def login_obrigatorio(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if usuario_atual() is None:
            return _nao_autenticado()
        return view(*args, **kwargs)

    return wrapper


def loja_obrigatoria(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        u = usuario_atual()
        if u is None:
            return _nao_autenticado()
        if u["papel"] != PAPEL_LOJA:
            return _proibido()
        return view(*args, **kwargs)

    return wrapper


def escritorio_obrigatorio(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        u = usuario_atual()
        if u is None:
            return _nao_autenticado()
        if u["papel"] != PAPEL_ESCRITORIO:
            return _proibido()
        return view(*args, **kwargs)

    return wrapper


def destino_pos_login(proximo: object, padrao: str = "/") -> str:
    """Só aceita caminho relativo seguro (evita open redirect)."""
    if proximo is None:
        return padrao
    texto = str(proximo).strip()
    if not texto.startswith("/") or texto.startswith("//"):
        return padrao
    if "\\" in texto or "://" in texto:
        return padrao
    if any(ord(c) < 32 for c in texto):
        return padrao
    return texto
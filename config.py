"""
config.py — Configuração central do Controle de Vencimentos CP FANI.

Responsabilidades:
  * Carregar variáveis de ambiente (e o .env apenas em dev local).
  * Expor a classe Config consumida por create_app() via from_object().
  * Validar com fail-fast: em APP_ENV=production qualquer inconsistência
    (segredo placeholder, credencial ausente, JSON inválido, planilha não
    configurada) impede a aplicação de subir.

Ordem de uso no app factory:
    Config.validar()                 # normaliza e falha alto se preciso
    app.config.from_object(Config)   # importa os atributos MAIÚSCULOS

Segurança: nenhum segredo real aqui — tudo vem do ambiente/.env.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Em produção as variáveis vêm do host; override=False não sobrescreve o
# ambiente real e não faz nada se o .env não existir.
load_dotenv(BASE_DIR / ".env", override=False)

logger = logging.getLogger(__name__)

SECRET_KEY_PLACEHOLDER = "troque-por-64-hex-gerado-com-secrets-token_hex"
_AMBIENTES_VALIDOS = ("development", "production")
_NIVEIS_LOG_VALIDOS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(RuntimeError):
    """Erro de configuração com mensagem amigável em PT-BR (fail-fast)."""


def _env_str(nome: str, padrao: str = "") -> str:
    """Lê string do ambiente, removendo espaços nas bordas."""
    valor = os.environ.get(nome)
    return padrao if valor is None else valor.strip()


def _env_int(
    nome: str,
    padrao: int,
    minimo: int | None = None,
    maximo: int | None = None,
) -> int:
    """Lê inteiro do ambiente com validação de faixa (fail-fast)."""
    bruto = _env_str(nome)
    if not bruto:
        valor = padrao
    else:
        try:
            valor = int(bruto)
        except ValueError as exc:
            raise ConfigError(
                f"Variável {nome} deve ser um número inteiro (recebido: {bruto!r})."
            ) from exc
    if minimo is not None and valor < minimo:
        raise ConfigError(f"Variável {nome} deve ser >= {minimo} (recebido: {valor}).")
    if maximo is not None and valor > maximo:
        raise ConfigError(f"Variável {nome} deve ser <= {maximo} (recebido: {valor}).")
    return valor


class Config:
    """Atributos em MAIÚSCULAS são importados pelo Flask via from_object()."""

    # --- Aplicação ---
    APP_ENV = _env_str("APP_ENV", "development").lower()
    APP_TIMEZONE = _env_str("APP_TIMEZONE", "America/Sao_Paulo")
    LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
    PORT = _env_int("PORT", 5000, minimo=1, maximo=65535)

    # --- Segurança / sessão ---
    SECRET_KEY = _env_str("SECRET_KEY")
    AUTH_USERS_JSON = _env_str("AUTH_USERS_JSON", "{}")
    AUTH_USERS: dict[str, str] = {}  # preenchido em validar()
    SESSION_LIFETIME_MINUTES = _env_int("SESSION_LIFETIME_MINUTES", 480, minimo=1)
    LOGIN_RATE_LIMIT = _env_str("LOGIN_RATE_LIMIT", "5 per minute")

    SESSION_COOKIE_NAME = "cp_fani_sessao"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = APP_ENV == "production"
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=SESSION_LIFETIME_MINUTES)
    PREFERRED_URL_SCHEME = "https" if APP_ENV == "production" else "http"

    # CSRF alinhado à sessão: evita erro de token expirado em operador ocioso.
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = SESSION_LIFETIME_MINUTES * 60

    # Rate limit em memória: coerente com gunicorn 1 worker (ver Procfile).
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_HEADERS_ENABLED = True

    # Apenas formulários pequenos; limita o payload das requisições.
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024

    # --- Google Sheets: credenciais da service account ---
    GOOGLE_CREDENTIALS_FILE = _env_str(
        "GOOGLE_CREDENTIALS_FILE", "secrets/service_account.json"
    )
    GOOGLE_SERVICE_ACCOUNT_JSON = _env_str("GOOGLE_SERVICE_ACCOUNT_JSON")
    GOOGLE_SERVICE_ACCOUNT_INFO: dict | None = None  # preenchido em validar()

    # --- Google Sheets: planilhas ---
    LANCAMENTOS_SHEET_ID = _env_str("LANCAMENTOS_SHEET_ID")
    LANCAMENTOS_TAB = _env_str("LANCAMENTOS_TAB", "Lancamentos")
    PRODUTOS_SHEET_ID = _env_str("PRODUTOS_SHEET_ID")
    PRODUTOS_TAB = _env_str("PRODUTOS_TAB")  # vazio = usa a 1ª aba

    # --- Cache em memória (protege a quota da API) ---
    PRODUTOS_CACHE_TTL = _env_int("PRODUTOS_CACHE_TTL", 600, minimo=0)
    LANCAMENTOS_CACHE_TTL = _env_int("LANCAMENTOS_CACHE_TTL", 60, minimo=0)

    @classmethod
    def is_production(cls) -> bool:
        return cls.APP_ENV == "production"

    @classmethod
    def validar(cls) -> None:
        """Valida e normaliza a configuração. Em produção, falha alto."""
        erros: list[str] = []
        avisos: list[str] = []
        prod = cls.is_production()

        if cls.APP_ENV not in _AMBIENTES_VALIDOS:
            erros.append(
                f"APP_ENV inválido: {cls.APP_ENV!r}. Use development ou production."
            )
        if cls.LOG_LEVEL not in _NIVEIS_LOG_VALIDOS:
            erros.append(f"LOG_LEVEL inválido: {cls.LOG_LEVEL!r}.")

        # Fuso horário (tzdata garante a base IANA também no Windows).
        try:
            ZoneInfo(cls.APP_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            erros.append(f"APP_TIMEZONE inválido: {cls.APP_TIMEZONE!r}.")

        # SECRET_KEY
        chave_invalida = (
            not cls.SECRET_KEY
            or cls.SECRET_KEY == SECRET_KEY_PLACEHOLDER
            or len(cls.SECRET_KEY) < 32
        )
        if chave_invalida:
            if prod:
                erros.append(
                    "SECRET_KEY ausente/placeholder/curta (<32 caracteres). "
                    "Gere com: python -c \"import secrets; print(secrets.token_hex(32))\"."
                )
            elif not cls.SECRET_KEY:
                cls.SECRET_KEY = secrets.token_hex(32)
                avisos.append(
                    "SECRET_KEY vazia em dev: gerada chave efêmera "
                    "(sessões são derrubadas a cada restart)."
                )
            else:
                avisos.append(
                    "SECRET_KEY placeholder em dev: NÃO use este valor em produção."
                )

        # AUTH_USERS_JSON -> dict normalizado {usuario: hash}
        try:
            dados = json.loads(cls.AUTH_USERS_JSON or "{}")
        except json.JSONDecodeError as exc:
            dados = None
            erros.append(f"AUTH_USERS_JSON não é JSON válido: {exc}.")
        if dados is not None:
            if not isinstance(dados, dict):
                erros.append("AUTH_USERS_JSON deve ser um objeto JSON {usuario: hash}.")
            else:
                normalizado: dict[str, str] = {}
                for usuario, hash_senha in dados.items():
                    u = str(usuario).strip().lower()
                    h = str(hash_senha).strip()
                    if not u or not h:
                        erros.append("AUTH_USERS_JSON contém usuário ou hash vazio.")
                        continue
                    if ":" not in h or "$" not in h:
                        erros.append(
                            f"Hash de senha inválido para {u!r} "
                            "(gere com scripts/gerar_hashes.py)."
                        )
                        continue
                    normalizado[u] = h
                cls.AUTH_USERS = normalizado
                if not normalizado:
                    msg = "AUTH_USERS_JSON sem usuários: nenhum login será possível."
                    (erros if prod else avisos).append(msg)

        # Credenciais Google: inline (prioridade) ou arquivo.
        if cls.GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                info = json.loads(cls.GOOGLE_SERVICE_ACCOUNT_JSON)
            except json.JSONDecodeError as exc:
                info = None
                erros.append(f"GOOGLE_SERVICE_ACCOUNT_JSON não é JSON válido: {exc}.")
            if info is not None:
                campos_ok = (
                    isinstance(info, dict)
                    and info.get("type") == "service_account"
                    and info.get("client_email")
                    and info.get("private_key")
                )
                if not campos_ok:
                    erros.append(
                        "GOOGLE_SERVICE_ACCOUNT_JSON deve ser a chave completa de "
                        "service account (type/client_email/private_key)."
                    )
                else:
                    cls.GOOGLE_SERVICE_ACCOUNT_INFO = info
        elif cls.GOOGLE_CREDENTIALS_FILE:
            caminho = Path(cls.GOOGLE_CREDENTIALS_FILE)
            if not caminho.is_absolute():
                caminho = BASE_DIR / caminho
            if caminho.is_file():
                cls.GOOGLE_CREDENTIALS_FILE = str(caminho)
            else:
                msg = f"Arquivo de credenciais não encontrado: {caminho}."
                (erros if prod else avisos).append(msg)
        else:
            msg = (
                "Nenhuma credencial Google configurada "
                "(GOOGLE_SERVICE_ACCOUNT_JSON ou GOOGLE_CREDENTIALS_FILE)."
            )
            (erros if prod else avisos).append(msg)

        # Planilhas
        if not cls.LANCAMENTOS_SHEET_ID:
            msg = (
                "LANCAMENTOS_SHEET_ID vazio: rode scripts/setup_planilha.py "
                "e preencha o .env."
            )
            (erros if prod else avisos).append(msg)
        if not cls.PRODUTOS_SHEET_ID:
            msg = "PRODUTOS_SHEET_ID vazio: autocomplete de produtos indisponível."
            (erros if prod else avisos).append(msg)

        if not cls.LOGIN_RATE_LIMIT:
            erros.append("LOGIN_RATE_LIMIT vazio: defina ex. '5 per minute'.")

        for aviso in avisos:
            logger.warning("[config] %s", aviso)
        if erros:
            raise ConfigError(
                "Configuração inválida:\n  - " + "\n  - ".join(erros)
            )
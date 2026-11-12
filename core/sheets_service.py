"""
core/sheets_service.py — Camada de persistência no Google Sheets.

Responsabilidades:
  * Cliente gspread via service account (inline no .env ou arquivo JSON).
  * Lançamentos: upsert idempotente por ID determinístico (DEDUP), com lock
    de escrita para impedir corrida entre requisições simultâneas.
  * Produtos: leitura da base de preços (SKU | Descrição | Preço), com
    dedup por SKU ("última ocorrência vence") e busca em memória (autocomplete).
  * Cache TTL para proteger a quota da API (60 req/min/usuário).
  * Retry com backoff exponencial + jitter para HTTP 429/5xx e falhas de rede.

Decisões de negócio refletidas:
  * DEDUP (2a) via ID = SHA-256(BPCS|SKU|MM/AAAA); descrição fora da chave.
  * Planilha de preços é SOMENTE leitura (permissão de visualizador).
  * Lançamentos em planilha própria (LANCAMENTOS_SHEET_ID).
  * listar_lancamentos() oculta VENCIDO por padrão (7b).
  * Escrita RAW: texto não é interpretado como fórmula/data pelo Sheets.

Compatibilidade gspread 6.x:
  * Worksheet.find() retorna Cell | None (a exceção CellNotFound do 5.x
    não existe mais). Não importar CellNotFound.

Nota de deploy: o lock de escrita é in-process; use 1 worker gunicorn.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import unicodedata
from typing import Any, Callable

import gspread
from gspread.exceptions import APIError
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout as ReqTimeout

from config import Config
from . import datas
from .models import COLUNAS, NUM_COLUNAS, Lancamento, Produto

logger = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
_MAX_TENTATIVAS = 4
_STATUS_RETENTAVEIS = {429, 500, 502, 503, 504}


class ServicoSheetsError(RuntimeError):
    """Erro amigável da camada Sheets (credenciais, quota, rede, dados)."""


# --- Estado / locks ---
_lock_cliente = threading.Lock()
_lock_escrita = threading.Lock()
_lock_carregamento = threading.Lock()
_cliente = None
_ws_lanc_obj = None
_ws_prod_obj = None
_cabecalho_verificado = False


class _CacheTTL:
    """Cache em memória com expiração por TTL (thread-safe)."""

    def __init__(self, ttl_segundos: int):
        self._ttl = ttl_segundos
        self._valor: Any = None
        self._expira = 0.0
        self._lock = threading.Lock()

    def obter(self) -> Any:
        with self._lock:
            if self._valor is not None and time.monotonic() < self._expira:
                return self._valor
            return None

    def guardar(self, valor: Any) -> None:
        with self._lock:
            self._valor = valor
            self._expira = time.monotonic() + self._ttl

    def invalidar(self) -> None:
        with self._lock:
            self._valor = None
            self._expira = 0.0


_cache_produtos = _CacheTTL(Config.PRODUTOS_CACHE_TTL)
_cache_lancamentos = _CacheTTL(Config.LANCAMENTOS_CACHE_TTL)


def _obter_ou_carregar(cache: _CacheTTL, loader: Callable[[], Any]) -> Any:
    """Cache com double-checked locking (evita carregamentos duplicados)."""
    valor = cache.obter()
    if valor is not None:
        return valor
    with _lock_carregamento:
        valor = cache.obter()
        if valor is not None:
            return valor
        valor = loader()
        cache.guardar(valor)
        return valor


# --- Retry / backoff ---
def _status_erro(exc: APIError) -> Any:
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc:
            return sc
    return getattr(exc, "code", None)


def _com_retry(descricao: str, fn: Callable, *args, **kwargs) -> Any:
    """Executa fn com retry/backoff para erros transitórios da API/rede."""
    espera = 1.0
    for tentativa in range(1, _MAX_TENTATIVAS + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            status = _status_erro(exc)
            if status in _STATUS_RETENTAVEIS and tentativa < _MAX_TENTATIVAS:
                pausa = espera + random.uniform(0, 0.5)
                logger.warning(
                    "[sheets] %s: HTTP %s, tentativa %d/%d, aguardando %.1fs",
                    descricao, status, tentativa, _MAX_TENTATIVAS, pausa,
                )
                time.sleep(pausa)
                espera *= 2
                continue
            raise ServicoSheetsError(
                f"Falha ao {descricao} (HTTP {status}): {exc}"
            ) from exc
        except (ReqConnectionError, ReqTimeout) as exc:
            if tentativa < _MAX_TENTATIVAS:
                logger.warning(
                    "[sheets] %s: falha de rede (%s), tentativa %d/%d",
                    descricao, exc, tentativa, _MAX_TENTATIVAS,
                )
                time.sleep(espera)
                espera *= 2
                continue
            raise ServicoSheetsError(
                f"Falha de rede ao {descricao}: {exc}"
            ) from exc
    raise ServicoSheetsError(f"Falha ao {descricao}: tentativas esgotadas.")


# --- Cliente / abas ---
def _criar_cliente() -> gspread.Client:
    info = Config.GOOGLE_SERVICE_ACCOUNT_INFO
    caminho = Config.GOOGLE_CREDENTIALS_FILE
    if info:
        creds = Credentials.from_service_account_info(info, scopes=list(SCOPES))
    elif caminho:
        creds = Credentials.from_service_account_file(caminho, scopes=list(SCOPES))
    else:
        raise ServicoSheetsError(
            "Credenciais Google ausentes. Configure GOOGLE_SERVICE_ACCOUNT_JSON "
            "ou GOOGLE_CREDENTIALS_FILE no .env."
        )
    return gspread.authorize(creds)


def _cliente_gspread() -> gspread.Client:
    global _cliente
    if _cliente is not None:
        return _cliente
    with _lock_cliente:
        if _cliente is None:
            _cliente = _criar_cliente()
        return _cliente


def _abrir_aba(sheet_id: str, tab: str, rotulo: str):
    if not sheet_id:
        raise ServicoSheetsError(
            f"ID da planilha de {rotulo} não configurado. "
            "Preencha o .env (veja .env.example)."
        )
    cliente = _cliente_gspread()
    planilha = _com_retry(
        f"abrir planilha de {rotulo}", cliente.open_by_key, sheet_id
    )
    if tab:
        return _com_retry(f"abrir aba de {rotulo}", planilha.worksheet, tab)
    return _com_retry(f"abrir 1ª aba de {rotulo}", planilha.get_worksheet, 0)


def _ws_lancamentos():
    global _ws_lanc_obj
    if _ws_lanc_obj is not None:
        return _ws_lanc_obj
    with _lock_cliente:
        if _ws_lanc_obj is None:
            _ws_lanc_obj = _abrir_aba(
                Config.LANCAMENTOS_SHEET_ID, Config.LANCAMENTOS_TAB, "lançamentos"
            )
        return _ws_lanc_obj


def _ws_produtos():
    global _ws_prod_obj
    if _ws_prod_obj is not None:
        return _ws_prod_obj
    with _lock_cliente:
        if _ws_prod_obj is None:
            _ws_prod_obj = _abrir_aba(
                Config.PRODUTOS_SHEET_ID, Config.PRODUTOS_TAB, "produtos"
            )
        return _ws_prod_obj


# --- Lançamentos: leitura / upsert ---
def _carregar_lancamentos() -> list[Lancamento]:
    ws = _ws_lancamentos()
    linhas = _com_retry("ler lançamentos", ws.get_all_values)
    if not linhas:
        return []
    inicio = 1 if (linhas[0] and str(linhas[0][0]).strip() == COLUNAS[0]) else 0
    resultado: list[Lancamento] = []
    descartadas = 0
    for linha in linhas[inicio:]:
        try:
            resultado.append(Lancamento.de_linha(linha))
        except (ValueError, IndexError):
            descartadas += 1
    if descartadas:
        logger.warning(
            "[sheets] %d linha(s) de lançamento inválida(s) ignorada(s).",
            descartadas,
        )
    resultado.sort(
        key=lambda l: (l.vencimento_data, l.nome_loja, l.codigo_produto)
    )
    return resultado


def listar_lancamentos(
    incluir_vencidos: bool = False, forcar_atualizacao: bool = False
) -> list[Lancamento]:
    """Lista lançamentos; por padrão oculta os VENCIDO (Decisão 7b)."""
    if forcar_atualizacao:
        _cache_lancamentos.invalidar()
    dados = _obter_ou_carregar(_cache_lancamentos, _carregar_lancamentos)
    if incluir_vencidos:
        return list(dados)
    return [l for l in dados if l.alerta != datas.ALERTA_VENCIDO]


def _garantir_cabecalho(ws) -> None:
    """Grava o cabeçalho apenas se a 1ª linha estiver vazia (idempotente)."""
    global _cabecalho_verificado
    if _cabecalho_verificado:
        return
    linha1 = _com_retry("verificar cabeçalho", ws.row_values, 1)
    if not linha1:
        fim = rowcol_to_a1(1, NUM_COLUNAS)
        _com_retry(
            "gravar cabeçalho",
            ws.update,
            values=[list(COLUNAS)],
            range_name=f"A1:{fim}",
            value_input_option="RAW",
        )
    _cabecalho_verificado = True


def upsert_lancamento(lancamento: Lancamento) -> str:
    """Cria ou sobrescreve o registro da mesma chave DEDUP. Retorna a ação."""
    with _lock_escrita:
        ws = _ws_lancamentos()
        _garantir_cabecalho(ws)
        # gspread 6.x: find() devolve None quando não encontra (sem exceção).
        celula = _com_retry(
            "localizar lançamento", ws.find, lancamento.id, in_column=1
        )
        linha = celula.row if celula is not None else None

        if linha:
            fim = rowcol_to_a1(linha, NUM_COLUNAS)
            _com_retry(
                "atualizar lançamento",
                ws.update,
                values=[lancamento.para_linha()],
                range_name=f"A{linha}:{fim}",
                value_input_option="RAW",
            )
            acao = "atualizado"
        else:
            _com_retry(
                "inserir lançamento",
                ws.append_row,
                lancamento.para_linha(),
                value_input_option="RAW",
            )
            acao = "criado"

        _cache_lancamentos.invalidar()
        logger.info(
            "[sheets] lançamento %s: %s (loja=%s sku=%s venc=%s)",
            acao, lancamento.id[:12], lancamento.loja_bpcs,
            lancamento.codigo_produto, lancamento.vencimento_mmaaaa,
        )
        return acao


# --- Produtos: leitura / autocomplete ---
def _sem_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _carregar_produtos() -> list[Produto]:
    ws = _ws_produtos()
    linhas = _com_retry("ler produtos", ws.get_all_values)
    if not linhas:
        return []
    primeira = str(linhas[0][0]).strip().upper() if linhas[0] else ""
    inicio = 1 if primeira in {"SKU", "CODIGO", "CÓDIGO", "CODIGO_PRODUTO"} else 0
    mapa: dict[str, Produto] = {}
    for linha in linhas[inicio:]:
        if len(linha) < 2:
            continue
        try:
            produto = Produto.criar(linha[0], linha[1])
        except ValueError:
            continue
        mapa[produto.sku] = produto  # última ocorrência vence (base duplicada)
    produtos = sorted(mapa.values(), key=lambda p: p.sku)
    logger.info(
        "[sheets] base de produtos carregada: %d SKU(s) único(s).", len(produtos)
    )
    return produtos


def buscar_produtos(
    termo: str, limite: int = 20, forcar_atualizacao: bool = False
) -> list[dict]:
    """Busca em memória por SKU ou descrição (autocomplete com debounce)."""
    termo = str(termo or "").strip()
    if not termo:
        return []
    if forcar_atualizacao:
        _cache_produtos.invalidar()
    produtos = _obter_ou_carregar(_cache_produtos, _carregar_produtos)
    t_sku = termo.upper()
    t_desc = _sem_acentos(termo).casefold()
    resultados: list[dict] = []
    for p in produtos:
        if t_sku in p.sku or t_desc in _sem_acentos(p.descricao).casefold():
            resultados.append(p.para_dict())
            if len(resultados) >= limite:
                break
    return resultados


def obter_produto(sku: str, forcar_atualizacao: bool = False) -> Produto | None:
    """Retorna o produto exato de um SKU (para preencher a descrição)."""
    if forcar_atualizacao:
        _cache_produtos.invalidar()
    produtos = _obter_ou_carregar(_cache_produtos, _carregar_produtos)
    alvo = str(sku or "").strip().upper()
    for p in produtos:
        if p.sku == alvo:
            return p
    return None


def invalidar_tudo() -> None:
    """Zera caches e cliente (útil após setup/mudança de configuração)."""
    global _cliente, _ws_lanc_obj, _ws_prod_obj, _cabecalho_verificado
    with _lock_cliente:
        _cliente = None
        _ws_lanc_obj = None
        _ws_prod_obj = None
        _cabecalho_verificado = False
    _cache_produtos.invalidar()
    _cache_lancamentos.invalidar()
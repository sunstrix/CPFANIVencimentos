"""
core/datas.py — Regras de data/vencimento do Controle de Vencimentos CP FANI.

Formato oficial (Decisão 8): MM/AAAA (ex.: 11/2026) em TODAS as camadas.

Regras de negócio implementadas:
  * Decisão 1a: lançamento com vencimento anterior ao mês atual é BLOQUEADO.
  * Janela rolante: exatamente 12 meses (mês atual + 11 seguintes).
  * Decisão 4a: Vencimento_Data = último dia do mês (calendário real, bissexto).
  * Fuso oficial: America/Sao_Paulo (Config.APP_TIMEZONE) — define o "mês atual".
  * Classificação de alerta (objetiva):
      VENCIDO  -> mês anterior ao atual
      CRITICO  -> mês atual ou próximo (vermelho)
      ATENCAO  -> meses subsequentes (amarelo)
    Obs.: EXIBIR ou OCULTAR os "VENCIDO" no dashboard é decisão de apresentação
    (pendência #7), aplicada em routes_escritorio/dashboard — não aqui.

Testabilidade: todas as funções que dependem do "agora" aceitam `referencia`
(uma date) para permitir testes determinísticos sem usar o relógio real.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import Config

# --- Constantes de negócio ---
MESES_JANELA = 12            # mês atual + 11 seguintes (fixo, conforme requisito)
ANO_MINIMO = 2000
ANO_MAXIMO = 2100

ALERTA_VENCIDO = "VENCIDO"
ALERTA_CRITICO = "CRITICO"
ALERTA_ATENCAO = "ATENCAO"

NOMES_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)

_PADRAO_MMAAAA = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$")


# --- Fuso / relógio (único ponto que lê a hora real) ---
def fuso() -> ZoneInfo:
    """Fuso oficial do negócio (Config.APP_TIMEZONE)."""
    return ZoneInfo(Config.APP_TIMEZONE)


def agora() -> datetime:
    """Data/hora atual no fuso oficial."""
    return datetime.now(fuso())


def hoje() -> date:
    """Data de hoje no fuso oficial."""
    return agora().date()


def _ref(referencia: date | None) -> date:
    return referencia if referencia is not None else hoje()


# --- Parsing / normalização MM/AAAA ---
def normalizar_mmaaaa(texto: object) -> str:
    """Valida e devolve o formato canônico 'MM/AAAA'. Levanta ValueError."""
    if texto is None or not str(texto).strip():
        raise ValueError("Vencimento ausente. Selecione um mês/ano.")
    bruto = str(texto)
    encontrado = _PADRAO_MMAAAA.match(bruto)
    if not encontrado:
        raise ValueError(
            f"Vencimento inválido: {bruto!r}. Use o formato MM/AAAA (ex.: 11/2026)."
        )
    mes = int(encontrado.group(1))
    ano = int(encontrado.group(2))
    if not 1 <= mes <= 12:
        raise ValueError(f"Mês inválido: {mes:02d}. Deve estar entre 01 e 12.")
    if not ANO_MINIMO <= ano <= ANO_MAXIMO:
        raise ValueError(
            f"Ano inválido: {ano}. Deve estar entre {ANO_MINIMO} e {ANO_MAXIMO}."
        )
    return f"{mes:02d}/{ano:04d}"


def mmaaaa_valido(texto: object) -> bool:
    """Versão sem exceção de normalizar_mmaaaa (para predicados)."""
    try:
        normalizar_mmaaaa(texto)
        return True
    except ValueError:
        return False


def partes_mmaaaa(texto: object) -> tuple[int, int]:
    """Retorna (mes, ano) já validados."""
    canon = normalizar_mmaaaa(texto)
    mes_s, ano_s = canon.split("/")
    return int(mes_s), int(ano_s)


def formatar_mmaaaa(mes: int, ano: int) -> str:
    """Monta 'MM/AAAA' a partir de partes (sem validar faixa)."""
    return f"{mes:02d}/{ano:04d}"


# --- Vencimento_Data (Decisão 4a: último dia do mês) ---
def vencimento_data(mmaaaa: object) -> date:
    """Último dia do mês de vencimento, respeitando o calendário/bissexto."""
    mes, ano = partes_mmaaaa(mmaaaa)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, ultimo_dia)


def formatar_data_br(data: date) -> str:
    """Formata uma date como 'DD/MM/AAAA' (UI/exports)."""
    return data.strftime("%d/%m/%Y")


# --- Mês atual / janela rolante ---
def mes_atual(referencia: date | None = None) -> str:
    """Mês atual canônico 'MM/AAAA' no fuso oficial."""
    ref = _ref(referencia)
    return formatar_mmaaaa(ref.month, ref.year)


def janela_lancamento(referencia: date | None = None) -> list[str]:
    """Lista rolante com exatamente MESES_JANELA meses: atual + 11 seguintes."""
    ref = _ref(referencia)
    ano, mes = ref.year, ref.month
    janela: list[str] = []
    for _ in range(MESES_JANELA):
        janela.append(formatar_mmaaaa(mes, ano))
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return janela


def diff_meses(mmaaaa: object, referencia: date | None = None) -> int:
    """Diferença em meses entre o vencimento e o mês de referência."""
    mes, ano = partes_mmaaaa(mmaaaa)
    ref = _ref(referencia)
    return (ano - ref.year) * 12 + (mes - ref.month)


def dentro_da_janela(mmaaaa: object, referencia: date | None = None) -> bool:
    """True se o mês está na janela [atual, atual+11]. Inválido -> False."""
    try:
        diferenca = diff_meses(mmaaaa, referencia)
    except ValueError:
        return False
    return 0 <= diferenca < MESES_JANELA


def validar_lancamento(mmaaaa: object, referencia: date | None = None) -> str:
    """Valida formato + janela (bloqueia vencido e além de 12 meses).

    Retorna o formato canônico. Levanta ValueError com mensagem PT-BR.
    """
    canon = normalizar_mmaaaa(mmaaaa)
    diferenca = diff_meses(canon, referencia)
    if diferenca < 0:
        raise ValueError(
            f"Vencimento {canon} já passou. Não é permitido lançar produto vencido."
        )
    if diferenca >= MESES_JANELA:
        raise ValueError(
            f"Vencimento {canon} está além da janela de {MESES_JANELA} meses permitida."
        )
    return canon


# --- Classificação de alerta (objetiva; exibição é a pendência #7) ---
def classificar_alerta(mmaaaa: object, referencia: date | None = None) -> str:
    """VENCIDO (passado), CRITICO (mês atual/próximo), ATENCAO (demais)."""
    diferenca = diff_meses(mmaaaa, referencia)
    if diferenca < 0:
        return ALERTA_VENCIDO
    if diferenca <= 1:
        return ALERTA_CRITICO
    return ALERTA_ATENCAO


def rotulo_mes(mmaaaa: object) -> str:
    """Rótulo por extenso para gráficos (ex.: 'novembro de 2026')."""
    mes, ano = partes_mmaaaa(mmaaaa)
    return f"{NOMES_MESES[mes - 1]} de {ano}"
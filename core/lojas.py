"""
core/lojas.py — Catálogo fixo das 17 lojas físicas + códigos BPCS.

Fonte da verdade dos identificadores de loja no sistema:
  * A sessão do operador guarda o BPCS; este módulo resolve o nome oficial.
  * O DEDUP e as linhas da planilha usam exatamente estes BPCS/nomes.
  * O escritório NÃO é uma loja: é o usuário administrativo "escritorio".

Garantias:
  * Catálogo imutável em tempo de execução (dataclass frozen + MappingProxyType).
  * Validação fail-fast na importação: BPCS numérico, único e nome não vazio.
  * Ordenação alfabética determinística, ignorando acentos e caixa.

Nota: o formato do usuário de login das lojas (BPCS x nome normalizado)
será definido em core/auth.py; o slug aqui é apenas um identificador
interno estável (âncoras de UI, filtros e testes).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class LojaDesconhecidaError(ValueError):
    """BPCS informado não existe no catálogo fixo de lojas."""


def _sem_acentos(texto: str) -> str:
    """Remove acentos para ordenação interna (não altera o nome exibido)."""
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def _gerar_slug(nome: str) -> str:
    """Identificador interno estável: 'Assaí Anchieta' -> 'assai_anchieta'."""
    base = _sem_acentos(nome).casefold()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


@dataclass(frozen=True)
class Loja:
    """Unidade física. bpcs é str para preservar o formato exato da planilha."""

    bpcs: str
    nome: str

    @property
    def slug(self) -> str:
        return _gerar_slug(self.nome)


# Ordem exatamente como na especificação do projeto (Matriz primeiro).
LOJAS: tuple[Loja, ...] = (
    Loja("4830", "Matriz"),
    Loja("4842", "Metrópole"),
    Loja("5152", "Coração"),
    Loja("6105", "Assaí Anchieta"),
    Loja("6106", "Direita"),
    Loja("6110", "Arouche"),
    Loja("8001", "Dom José"),
    Loja("12055", "São Bento"),
    Loja("11576", "D'Avó"),
    Loja("12605", "Coop"),
    Loja("12645", "Light"),
    Loja("20371", "Metrô"),
    Loja("21502", "Bem Barato"),
    Loja("23000", "Outlet"),
    Loja("23379", "Assaí Piraporinha"),
    Loja("25341", "Pq. Dom Pedro"),
    Loja("12056", "Marechal"),
)

# Login administrativo exclusivo (não é loja e não lança produtos).
USUARIO_ESCRITORIO = "escritorio"


def _validar_catalogo(lojas: tuple[Loja, ...]) -> None:
    """Fail-fast na importação se alguém editar o catálogo incorretamente."""
    bpcs_vistos: set[str] = set()
    slugs_vistos: set[str] = set()
    for loja in lojas:
        if not loja.bpcs.isdigit():
            raise RuntimeError(
                f"BPCS inválido (não numérico): {loja.bpcs!r} ({loja.nome})."
            )
        if loja.bpcs in bpcs_vistos:
            raise RuntimeError(
                f"BPCS duplicado no catálogo: {loja.bpcs} ({loja.nome})."
            )
        if not loja.nome.strip():
            raise RuntimeError(f"Loja com nome vazio (BPCS {loja.bpcs}).")
        if loja.slug in slugs_vistos:
            raise RuntimeError(
                f"Slug duplicado no catálogo: {loja.slug!r} ({loja.nome})."
            )
        bpcs_vistos.add(loja.bpcs)
        slugs_vistos.add(loja.slug)


_validar_catalogo(LOJAS)

LOJAS_POR_BPCS: Mapping[str, Loja] = MappingProxyType(
    {loja.bpcs: loja for loja in LOJAS}
)


def _normalizar_bpcs(bpcs: object) -> str:
    """Aceita str/int e remove espaços; nunca levanta exceção."""
    return str(bpcs).strip()


def bpcs_valido(bpcs: object) -> bool:
    """True se o BPCS pertence ao catálogo fixo."""
    return _normalizar_bpcs(bpcs) in LOJAS_POR_BPCS


def obter_loja(bpcs: object) -> Loja | None:
    """Retorna a loja ou None (validações que tratam o erro localmente)."""
    return LOJAS_POR_BPCS.get(_normalizar_bpcs(bpcs))


def loja_obrigatoria(bpcs: object) -> Loja:
    """Retorna a loja ou levanta LojaDesconhecidaError (rotas/serviços)."""
    loja = obter_loja(bpcs)
    if loja is None:
        raise LojaDesconhecidaError(
            f"BPCS desconhecido: {bpcs!r}. Loja fora do catálogo fixo."
        )
    return loja


def listar_lojas() -> tuple[Loja, ...]:
    """Catálogo na ordem da especificação (selects e relatórios)."""
    return LOJAS


def listar_lojas_por_nome() -> tuple[Loja, ...]:
    """Ordem alfabética PT-BR simplificada (sem acentos/caixa), estável."""
    return tuple(
        sorted(
            LOJAS,
            key=lambda loja: (_sem_acentos(loja.nome).casefold(), loja.bpcs),
        )
    )
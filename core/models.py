"""
core/models.py — Objetos de domínio do Controle de Vencimentos CP FANI.

Contém:
  * COLUNAS: esquema oficial da aba Lancamentos (fonte única p/ todas as camadas).
  * Lancamento: registro de produto/vencimento por loja, com ID determinístico.
  * Produto: item da base de preços usado no autocomplete (SKU + descrição).

Decisões de negócio refletidas:
  * DEDUP (2a): chave = Loja_BPCS + Codigo_Produto + Vencimento_MMAAAA.
    A descrição NÃO entra na chave -> editá-la não cria duplicata.
  * SKU obrigatório (3a), mesmo com descrição manual.
  * Formato MM/AAAA em todas as camadas (8a); coluna Vencimento_MMAAAA.
  * Vencimento_Data = último dia do mês (4a).
  * Operador_Usuario = BPCS do login da loja (6a); padrão = a própria loja.
  * criar() valida a janela e BLOQUEIA vencido (1a); de_linha() apenas lê
    (não bloqueia), pois registros antigos já vencidos existem na planilha.

Esquema de ID:
  SHA-256 da chave normalizada "BPCS|SKU|MM/AAAA" (hex). Determinístico:
  a mesma chave sempre gera o mesmo ID -> upsert idempotente no Sheets.

Datas armazenadas (formato técnico, ordenável, sem ambiguidade):
  * Vencimento_Data: "AAAA-MM-DD".
  * Data_Hora_Lancamento: "AAAA-MM-DD HH:MM:SS" no fuso America/Sao_Paulo.
  A UI/exports renderizam em PT-BR (DD/MM/AAAA).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from . import datas
from .lojas import LOJAS_POR_BPCS, loja_obrigatoria

# --- Esquema oficial da aba Lancamentos (ordem exata das colunas) ---
COLUNAS: tuple[str, ...] = (
    "ID",
    "Data_Hora_Lancamento",
    "Loja_BPCS",
    "Nome_Loja",
    "Codigo_Produto",
    "Descricao_Produto",
    "Vencimento_MMAAAA",
    "Vencimento_Data",
    "Operador_Usuario",
)
NUM_COLUNAS = len(COLUNAS)

SEPARADOR_CHAVE = "|"
_FORMATO_DATA_HORA = "%Y-%m-%d %H:%M:%S"


# --- Chave de deduplicação / ID determinístico ---
def chave_dedup(loja_bpcs: str, codigo_produto: str, vencimento_mmaaaa: str) -> str:
    """Chave canônica do DEDUP. Recebe componentes JÁ normalizados."""
    return (
        f"{loja_bpcs}{SEPARADOR_CHAVE}{codigo_produto}"
        f"{SEPARADOR_CHAVE}{vencimento_mmaaaa}"
    )


def gerar_id(loja_bpcs: str, codigo_produto: str, vencimento_mmaaaa: str) -> str:
    """ID determinístico (SHA-256 hex) a partir da chave normalizada."""
    chave = chave_dedup(loja_bpcs, codigo_produto, vencimento_mmaaaa)
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


# --- Normalizações compartilhadas ---
def _normalizar_sku(valor: object) -> str:
    if valor is None:
        raise ValueError("Código do produto (SKU) é obrigatório.")
    sku = str(valor).strip().upper()
    if not sku:
        raise ValueError("Código do produto (SKU) é obrigatório.")
    if SEPARADOR_CHAVE in sku:
        raise ValueError(f"Código do produto não pode conter {SEPARADOR_CHAVE!r}.")
    return sku


def _normalizar_descricao(valor: object) -> str:
    if valor is None:
        raise ValueError("Descrição do produto é obrigatória.")
    descricao = str(valor).strip()
    if not descricao:
        raise ValueError("Descrição do produto é obrigatória.")
    return descricao


def _normalizar_operador(valor: object, bpcs_padrao: str) -> str:
    if valor is None:
        return bpcs_padrao
    operador = str(valor).strip()
    return operador or bpcs_padrao


# --- Objeto de domínio: Lancamento ---
@dataclass(frozen=True)
class Lancamento:
    id: str
    data_hora_lancamento: str
    loja_bpcs: str
    nome_loja: str
    codigo_produto: str
    descricao_produto: str
    vencimento_mmaaaa: str
    vencimento_data: str
    operador_usuario: str

    # -- Fábrica para NOVO lançamento (valida janela, bloqueia vencido) --
    @classmethod
    def criar(
        cls,
        *,
        loja_bpcs: object,
        codigo_produto: object,
        descricao_produto: object,
        vencimento_mmaaaa: object,
        operador_usuario: object = None,
        data_hora: datetime | None = None,
        referencia: date | None = None,
    ) -> "Lancamento":
        loja = loja_obrigatoria(loja_bpcs)  # LojaDesconhecidaError se inválido
        sku = _normalizar_sku(codigo_produto)
        descricao = _normalizar_descricao(descricao_produto)
        mmaaaa = datas.validar_lancamento(vencimento_mmaaaa, referencia)  # 1a + janela
        venc_data = datas.vencimento_data(mmaaaa).isoformat()
        operador = _normalizar_operador(operador_usuario, loja.bpcs)
        momento = (data_hora or datas.agora()).strftime(_FORMATO_DATA_HORA)
        return cls(
            id=gerar_id(loja.bpcs, sku, mmaaaa),
            data_hora_lancamento=momento,
            loja_bpcs=loja.bpcs,
            nome_loja=loja.nome,
            codigo_produto=sku,
            descricao_produto=descricao,
            vencimento_mmaaaa=mmaaaa,
            vencimento_data=venc_data,
            operador_usuario=operador,
        )

    # -- Fábrica para LEITURA de linha existente (não bloqueia vencido) --
    @classmethod
    def de_linha(cls, linha: Sequence) -> "Lancamento":
        bruto = list(linha)[:NUM_COLUNAS]
        bruto += [""] * (NUM_COLUNAS - len(bruto))
        valores = ["" if c is None else str(c) for c in bruto]

        bpcs = valores[2].strip()
        loja = LOJAS_POR_BPCS.get(bpcs)
        nome_loja = loja.nome if loja else valores[3].strip()
        sku = _normalizar_sku(valores[4])
        descricao = valores[5].strip()
        mmaaaa = datas.normalizar_mmaaaa(valores[6])  # normaliza, NÃO bloqueia
        venc_data = datas.vencimento_data(mmaaaa).isoformat()
        operador = valores[8].strip() or bpcs
        return cls(
            id=gerar_id(bpcs, sku, mmaaaa),
            data_hora_lancamento=valores[1].strip(),
            loja_bpcs=bpcs,
            nome_loja=nome_loja,
            codigo_produto=sku,
            descricao_produto=descricao,
            vencimento_mmaaaa=mmaaaa,
            vencimento_data=venc_data,
            operador_usuario=operador,
        )

    # -- Serialização --
    def para_linha(self) -> list[str]:
        """Lista na ordem exata de COLUNAS (para gravar no Sheets)."""
        return [
            self.id,
            self.data_hora_lancamento,
            self.loja_bpcs,
            self.nome_loja,
            self.codigo_produto,
            self.descricao_produto,
            self.vencimento_mmaaaa,
            self.vencimento_data,
            self.operador_usuario,
        ]

    def para_dict(self) -> dict:
        """Dict para JSON/templates, com campos derivados prontos."""
        return {
            "id": self.id,
            "data_hora_lancamento": self.data_hora_lancamento,
            "loja_bpcs": self.loja_bpcs,
            "nome_loja": self.nome_loja,
            "codigo_produto": self.codigo_produto,
            "descricao_produto": self.descricao_produto,
            "vencimento_mmaaaa": self.vencimento_mmaaaa,
            "vencimento_data": self.vencimento_data,
            "vencimento_data_br": self.vencimento_data_br,
            "operador_usuario": self.operador_usuario,
            "alerta": self.alerta,
            "chave_dedup": self.chave,
        }

    # -- Propriedades derivadas --
    @property
    def chave(self) -> str:
        return chave_dedup(
            self.loja_bpcs, self.codigo_produto, self.vencimento_mmaaaa
        )

    @property
    def alerta(self) -> str:
        """Classificação objetiva (VENCIDO/CRITICO/ATENCAO) no mês atual real."""
        return datas.classificar_alerta(self.vencimento_mmaaaa)

    @property
    def vencimento_data_br(self) -> str:
        return datas.formatar_data_br(date.fromisoformat(self.vencimento_data))


# --- Objeto de domínio: Produto (base de preços / autocomplete) ---
@dataclass(frozen=True)
class Produto:
    sku: str
    descricao: str

    @classmethod
    def criar(cls, sku: object, descricao: object) -> "Produto":
        return cls(
            sku=_normalizar_sku(sku),
            descricao=("" if descricao is None else str(descricao).strip()),
        )

    def para_dict(self) -> dict:
        return {"sku": self.sku, "descricao": self.descricao}
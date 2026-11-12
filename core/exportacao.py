"""
core/exportacao.py — Exportação de relatórios (CSV e XLSX).

Módulo puro: recebe uma lista de Lancamento (já filtrada pela rota, sem
VENCIDO por padrão — Decisão 7b) e devolve bytes prontos para download.

Segurança / usabilidade:
  * CSV: separador ';' + UTF-8 BOM (utf-8-sig) -> acentos corretos no Excel
    PT-BR. Células que começam com '= + - @' (ou tab/CR) recebem apóstrofo
    para NÃO serem interpretadas como fórmula (CSV injection).
  * XLSX: cada célula de dados é forçada como texto (data_type='s'), então
    uma descrição iniciada por '=' não vira fórmula e volta íntegra.
  * XLSX: cabeçalho destacado, autofiltro, painel congelado e cores por alerta.

Colunas do relatório (layout legível; o ID hash interno fica fora):
  BPCS · Loja · Código · Descrição · Vencimento · Vence em · Alerta ·
  Lançado em · Operador
"""
from __future__ import annotations

import csv
import io
from typing import Sequence

from . import datas
from .models import Lancamento

# Layout do relatório (cabeçalho amigável ao escritório).
CABECALHO_EXPORT: tuple[str, ...] = (
    "BPCS",
    "Loja",
    "Código",
    "Descrição",
    "Vencimento",
    "Vence em",
    "Alerta",
    "Lançado em",
    "Operador",
)

_INICIO_PERIGOSO = ("=", "+", "-", "@")
_CORES_ALERTA = {
    datas.ALERTA_CRITICO: "F8D7DA",   # vermelho claro
    datas.ALERTA_ATENCAO: "FFF3CD",   # amarelo claro
    datas.ALERTA_VENCIDO: "E2E3E5",   # cinza (só se incluído explicitamente)
}


def _linha_export(lancamento: Lancamento) -> list[str]:
    """Valores do relatório, na ordem de CABECALHO_EXPORT (tudo texto)."""
    return [
        lancamento.loja_bpcs,
        lancamento.nome_loja,
        lancamento.codigo_produto,
        lancamento.descricao_produto,
        lancamento.vencimento_mmaaaa,
        lancamento.vencimento_data_br,
        lancamento.alerta,
        lancamento.data_hora_lancamento,
        lancamento.operador_usuario,
    ]


def _sanitizar_texto(valor: object) -> str:
    """Neutraliza CSV injection prefixando apóstrofo em início perigoso."""
    texto = "" if valor is None else str(valor)
    primeiro = texto[:1]
    if (
        primeiro in _INICIO_PERIGOSO
        or primeiro in ("\t", "\r")
        or texto.lstrip()[:1] in _INICIO_PERIGOSO
    ):
        return "'" + texto
    return texto


def gerar_csv(lancamentos: Sequence[Lancamento]) -> bytes:
    """CSV ';' com UTF-8 BOM e proteção anti-fórmula."""
    buffer = io.StringIO()
    escritor = csv.writer(
        buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n"
    )
    escritor.writerow(CABECALHO_EXPORT)
    for lancamento in lancamentos:
        escritor.writerow(
            [_sanitizar_texto(v) for v in _linha_export(lancamento)]
        )
    return buffer.getvalue().encode("utf-8-sig")


def gerar_xlsx(lancamentos: Sequence[Lancamento]) -> bytes:
    """XLSX com cabeçalho, autofiltro, painel congelado e cores por alerta."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "openpyxl não instalado; não é possível gerar XLSX."
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Vencimentos"

    ws.append(list(CABECALHO_EXPORT))
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for lancamento in lancamentos:
        ws.append(_linha_export(lancamento))
        idx = ws.max_row
        cor = _CORES_ALERTA.get(lancamento.alerta)
        for col in range(1, len(CABECALHO_EXPORT) + 1):
            cell = ws.cell(row=idx, column=col)
            cell.data_type = "s"  # força texto (anti-fórmula), preserva o valor
            if cor:
                cell.fill = PatternFill("solid", fgColor=cor)

    larguras = (10, 22, 12, 50, 12, 12, 12, 20, 12)
    for i, largura in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(i)].width = largura

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def nome_arquivo(extensao: str) -> str:
    """Nome de download com carimbo de data/hora (fuso oficial)."""
    carimbo = datas.agora().strftime("%Y%m%d_%H%M")
    return f"vencimentos_{carimbo}.{extensao}"


def gerar_export(lancamentos: Sequence[Lancamento], formato: str = "csv"):
    """Despacha o formato e devolve (bytes, mimetype, nome_arquivo)."""
    formato = (formato or "csv").strip().lower()
    if formato == "xlsx":
        return (
            gerar_xlsx(lancamentos),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            nome_arquivo("xlsx"),
        )
    return (
        gerar_csv(lancamentos),
        "text/csv; charset=utf-8",
        nome_arquivo("csv"),
    )
"""
core/routes_loja.py — Rotas da operação da loja (lançamento + autocomplete).

Blueprint "loja": endpoint "lancamento" (alvo do redirect pós-login).

Regras de negócio:
  * Isolamento: o BPCS vem SOMENTE da sessão; qualquer loja_bpcs enviado
    pelo cliente é ignorado (loja não lança nem lê dados de outra).
  * Janela rolante de 12 meses (MM/AAAA) gerada no fuso oficial.
  * DEDUP via sheets_service.upsert_lancamento (último lançamento vence).
  * SKU obrigatório; descrição manual permitida; se vier vazia e o SKU
    existir na base, preenchemos pela base (conveniência do §5).
  * Lista da própria loja usa o filtro padrão (sem VENCIDO, 7b).

Degradação graciosa: se o Sheets estiver indisponível, a tela ainda abre
(a janela de meses é local) e o autocomplete orienta digitar manualmente.
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

from . import auth, datas
from . import sheets_service as sheets
from .models import Lancamento

logger = logging.getLogger(__name__)

bp = Blueprint("loja", __name__)


def _quer_json() -> bool:
    return (
        request.is_json
        or request.path.startswith("/api/")
        or request.accept_mimetypes.best == "application/json"
    )


def _dados_form() -> dict:
    """Aceita form clássico (PRG) ou JSON (fetch com X-CSRFToken)."""
    if request.is_json:
        dados = request.get_json(silent=True) or {}
        return dados if isinstance(dados, dict) else {}
    return request.form.to_dict()


@bp.route("/lancamento", methods=["GET"])
@auth.loja_obrigatoria
def lancamento():
    u = auth.usuario_atual()
    meses = datas.janela_lancamento()
    meus: list[dict] = []
    aviso = None
    try:
        todos = sheets.listar_lancamentos()
        meus = [l.para_dict() for l in todos if l.loja_bpcs == u["loja_bpcs"]]
    except sheets.ServicoSheetsError as exc:
        logger.warning("[loja] lista indisponível: %s", exc)
        aviso = (
            "Não foi possível carregar seus lançamentos agora. "
            "Tente novamente em instantes."
        )
    return render_template(
        "lancamento.html",
        perfil=u,
        meses=meses,
        mes_atual=datas.mes_atual(),
        lancamentos=meus,
        aviso=aviso,
    )


@bp.route("/api/produtos", methods=["GET"])
@auth.loja_obrigatoria
def api_produtos():
    termo = request.args.get("q") or request.args.get("termo") or ""
    try:
        resultados = sheets.buscar_produtos(termo, limite=20)
    except sheets.ServicoSheetsError as exc:
        logger.warning("[loja] autocomplete indisponível: %s", exc)
        return (
            jsonify(
                {
                    "erro": (
                        "Base de produtos indisponível. "
                        "Digite a descrição manualmente."
                    ),
                    "produtos": [],
                }
            ),
            503,
        )
    return jsonify({"produtos": resultados})


@bp.route("/lancamento", methods=["POST"])
@auth.loja_obrigatoria
def lancar():
    u = auth.usuario_atual()
    dados = _dados_form()
    codigo = dados.get("codigo_produto", "")
    descricao = str(dados.get("descricao_produto", "") or "").strip()
    vencimento = dados.get("vencimento", "")

    # Conveniência: descrição vazia + SKU conhecido -> preenche pela base.
    if not descricao and str(codigo).strip():
        try:
            produto = sheets.obter_produto(codigo)
            if produto is not None:
                descricao = produto.descricao
        except sheets.ServicoSheetsError:
            pass  # sem base: segue e a validação acusará descrição vazia

    try:
        novo = Lancamento.criar(
            loja_bpcs=u["loja_bpcs"],  # SOMENTE da sessão (isolamento)
            codigo_produto=codigo,
            descricao_produto=descricao,
            vencimento_mmaaaa=vencimento,
            operador_usuario=u["usuario"],
        )
    except ValueError as exc:
        if _quer_json():
            return jsonify({"erro": str(exc)}), 400
        flash(str(exc), "erro")
        return redirect(url_for("loja.lancamento"))

    try:
        acao = sheets.upsert_lancamento(novo)
    except sheets.ServicoSheetsError as exc:
        logger.error("[loja] falha ao gravar: %s", exc)
        mensagem = (
            "Não foi possível gravar na planilha agora. "
            "Tente novamente em instantes."
        )
        if _quer_json():
            return jsonify({"erro": mensagem}), 503
        flash(mensagem, "erro")
        return redirect(url_for("loja.lancamento"))

    if _quer_json():
        return jsonify({"ok": True, "acao": acao, "lancamento": novo.para_dict()})
    flash(
        f"Lançamento {acao} com sucesso: "
        f"{novo.codigo_produto} — {novo.vencimento_mmaaaa}.",
        "sucesso",
    )
    return redirect(url_for("loja.lancamento"))
"""
Dashboard gerencial (Blueprint Flask).

Módulo independente: apenas LÊ as funções já existentes do projeto
(detectar_ciclos_por_ancora, resumir_girador, consultar_tabelas_influx,
horario_turnos) e não altera nenhuma delas.

Todos os tempos (duração de ciclo/etapa, ociosidade, arco, lacunas) consideram
apenas a JORNADA PRODUTIVA: dentro dos turnos e fora das pausas de almoço e
janta (ver periodos.py). Noites, finais de semana e refeições são descartados.

Rotas:
  GET /dashboard            -> página (templates/dashboard.html)
  GET /dashboard/api/dados  -> JSON com ciclos, etapas, arco, paradas

Desenvolvimento sem InfluxDB: defina DASHBOARD_MOCK_DIR apontando para uma
pasta com soldagem.txt e posicao_girador.txt (JSONL), ex.: DASHBOARD_MOCK_DIR=mock
"""
import logging
import os
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from config import DURACAO_MAXIMA_CICLO_S, TZ_LOCAL
from dados import ler_jsonl
from girador import detectar_ciclos_por_ancora, resumir_girador
from influx_client import consultar_tabelas_influx
from periodos import IndiceArco, Jornada, intervalos_de_arco, subtrair_intervalos, uniao_de_arcos
from turnos import feriado

log = logging.getLogger(__name__)
bp = Blueprint("dashboard", __name__)

BASE_DIR = Path(__file__).resolve().parent

# Janela de análise por dia produtivo: 06:30 até 02:00 do dia seguinte
# (o 2º turno de segunda a quinta termina às 01:40).
INICIO_DIA_PRODUTIVO = time(6, 30)
FIM_DIA_PRODUTIVO = time(2, 0)
MAX_DIAS = 31
CACHE_TTL_S = 60

NOMES_ETAPAS = {
    10: "Posicionamento (início)",
    20: "Solda plana da emenda",
    30: "Solda vertical 1 + nervuras",
    40: "Solda vertical 2 + nervuras",
    50: "Limpeza",
    60: "Finalização (viga pronta)",
}

_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------
def _iso(dt):
    return dt.astimezone(TZ_LOCAL).isoformat() if dt else None


def _carregar_registros(janela_inicio, janela_fim):
    pasta_mock = os.getenv("DASHBOARD_MOCK_DIR")
    if pasta_mock:
        pasta = Path(pasta_mock)
        if not pasta.is_absolute():
            pasta = BASE_DIR / pasta
        return (
            ler_jsonl(pasta / "soldagem.txt"),
            ler_jsonl(pasta / "posicao_girador.txt"),
        )

    token = os.getenv("INFLUX_TOKEN")
    if not token:
        raise RuntimeError("INFLUX_TOKEN não definido no ambiente/.env.")
    url = os.getenv("INFLUX_URL", "http://10.20.0.239:8181")
    banco = os.getenv("INFLUX_DB", "manufatura")
    # Margem igual à usada na detecção de ciclos (-6h / +2h).
    return consultar_tabelas_influx(
        url, banco, token,
        janela_inicio - timedelta(hours=6),
        janela_fim + timedelta(hours=2),
    )


def _serializar_ciclo(c, jornada, indices_arco, fim_efetivo):
    dia, turno = jornada.turno_de(c["etapa_30"]["inicio"])
    forcado = bool(c.get("fim_forcado"))

    etapas = []
    for cod in (10, 20, 30, 40, 50, 60):
        e = c.get(f"etapa_{cod}")
        if not e:
            etapas.append({"cod": cod, "nome": NOMES_ETAPAS[cod], "presente": False})
            continue
        etapas.append({
            "cod": cod,
            "nome": NOMES_ETAPAS[cod],
            "presente": True,
            "angulo": e["angulo"],
            "inicio": _iso(e["inicio"]),
            "fim": _iso(e["fim"]),
            "duracao_s": e["duracao"],
            "duracao_prod_s": jornada.segundos(e["inicio"], e["fim"]),
            # Etapa 10 é sintética (fixa 5 min) e a 60 é limitada a 5 min;
            # etapa encerrada à força também não representa duração real.
            "estimada": cod in (10, 60) or (forcado and e["fim"] == c["fim_ciclo"]),
        })

    ini, fim = c["inicio_ciclo"], fim_efetivo
    trechos = jornada.trechos_ts(ini, fim) if fim > ini else []
    arco = {}
    for maq, indice in indices_arco.items():
        arco_s, corrente = indice.arco_em(trechos)
        arco[maq] = {"arco_s": arco_s, "corrente_A": corrente}

    return {
        "numero": c["numero"],
        "status": c["status"],
        "forcado": forcado,
        "dia": dia.isoformat(),
        "turno": turno,
        "inicio": _iso(c["inicio_ciclo"]),
        "fim": _iso(c["fim_ciclo"]),
        "duracao_s": c["duracao_ciclo"],
        "duracao_prod_s": jornada.segundos(ini, fim),
        "sequencia": c["sequencia"],
        "etapas": etapas,
        "arco": arco,
    }


def _paradas(ciclos, jornada, excluidos=()):
    """Ociosidade entre ciclos consecutivos, só dentro da jornada produtiva.

    Um intervalo que atravessa a noite ou o fim de semana conta apenas as
    horas de turno; cada (dia, turno) afetado gera uma entrada. Os períodos
    `excluidos` (ciclos descartados por falha do sensor) não contam como ocioso.
    """
    paradas = []
    for atual, prox in zip(ciclos, ciclos[1:]):
        if not atual["fim_ciclo"]:
            continue
        ini, fim = atual["fim_ciclo"], prox["inicio_ciclo"]
        if fim <= ini:
            continue
        por_turno = {}
        for fa, fb, dia, rotulo in jornada.fatiar(ini, fim):
            for a, b in subtrair_intervalos(fa, fb, excluidos):
                item = por_turno.setdefault((dia, rotulo), {"ini": a, "fim": b, "s": 0.0})
                item["fim"] = b
                item["s"] += (b - a).total_seconds()
        for (dia, rotulo), item in por_turno.items():
            if item["s"] < 1:
                continue
            paradas.append({
                "inicio": _iso(item["ini"]),
                "fim": _iso(item["fim"]),
                "duracao_s": item["s"],
                "dia": dia.isoformat(),
                "turno": rotulo,
                "entre": [atual["numero"], prox["numero"]],
            })
    return paradas


# ---------------------------------------------------------------------
# Cálculo principal
# ---------------------------------------------------------------------
def calcular_dashboard(dia_inicio, dia_fim):
    janela_inicio = datetime.combine(dia_inicio, INICIO_DIA_PRODUTIVO, tzinfo=TZ_LOCAL)
    janela_fim = datetime.combine(dia_fim + timedelta(days=1), FIM_DIA_PRODUTIVO, tzinfo=TZ_LOCAL)
    agora = datetime.now(TZ_LOCAL)

    reg_soldagem, reg_girador = _carregar_registros(janela_inicio, janela_fim)

    arcos = intervalos_de_arco(reg_soldagem)
    jornada = Jornada(dia_inicio, dia_fim, arcos, agora)
    indices_arco = {maq: IndiceArco(ivs) for maq, ivs in arcos.items()}

    descartados_brutos = []
    ciclos = detectar_ciclos_por_ancora(
        reg_girador, janela_inicio, janela_fim,
        arcos=uniao_de_arcos(arcos), descartados=descartados_brutos,
    )
    # Ciclos acima do limite de duração (falha do sensor): o período deles sai dos totais.
    referencia = min(janela_fim, agora)
    excluidos = [(c["inicio_ciclo"], c["fim_ciclo"] or referencia) for c in descartados_brutos]
    descartados = [
        {
            "inicio": _iso(c["inicio_ciclo"]),
            "fim": _iso(c["fim_ciclo"] or referencia),
            "em_andamento": c["fim_ciclo"] is None,
            "duracao_s": c["duracao_descartada_s"],
        }
        for c in descartados_brutos
    ]
    _, _, lacunas_girador, _ = resumir_girador(reg_girador, janela_inicio, janela_fim)

    ciclos_json = [
        _serializar_ciclo(c, jornada, indices_arco, c["fim_ciclo"] or min(janela_fim, agora))
        for c in ciclos
    ]

    # Lacunas de dados (sensor/rede): só as que caem dentro da jornada produtiva.
    lacunas = []
    for a, b, _ in lacunas_girador:
        prod = jornada.segundos(a, b)
        if prod >= 1:
            lacunas.append({"fonte": "Girador", "inicio": _iso(a), "fim": _iso(b), "duracao_s": prod})
    for (e_ini, e_fim), d in zip(excluidos, descartados):
        prod = jornada.segundos(e_ini, e_fim)
        if prod >= 1:
            lacunas.append({
                "fonte": f"Ciclo descartado (> {DURACAO_MAXIMA_CICLO_S // 3600} h)",
                "inicio": d["inicio"], "fim": d["fim"], "duracao_s": prod,
            })
    lacunas.sort(key=lambda x: x["inicio"])

    # Arco total por máquina no período, dentro da jornada produtiva.
    fim_total = min(janela_fim, agora)
    trechos_total = jornada.trechos_ts(janela_inicio, fim_total) if fim_total > janela_inicio else []
    maquinas = {}
    for maq, indice in indices_arco.items():
        arco_s, corrente = indice.arco_em(trechos_total)
        maquinas[maq] = {"arco_s": arco_s, "corrente_A": corrente}

    disponivel = [
        {"dia": d.isoformat(), "turno": t, "produtivo_s": s}
        for (d, t), s in sorted(jornada.disponivel(agora, excluidos).items())
    ]
    # Feriados em dias úteis do período (para a linha do tempo explicar a ausência de turno).
    feriados = []
    dia = dia_inicio
    while dia <= dia_fim:
        nome = feriado(dia)
        if nome and dia.weekday() < 5:
            feriados.append({"dia": dia.isoformat(), "nome": nome})
        dia += timedelta(days=1)

    turnos = [
        {"dia": d.isoformat(), "turno": r, "inicio": _iso(a), "fim": _iso(b)}
        for a, b, d, r in jornada.turnos
    ]
    pausas = [
        {"dia": d.isoformat(), "nome": n, "inicio": _iso(a), "fim": _iso(b)}
        for a, b, d, n in sorted(jornada.pausas)
    ]

    return {
        "periodo": {
            "inicio": _iso(janela_inicio),
            "fim": _iso(janela_fim),
            "dia_inicio": dia_inicio.isoformat(),
            "dia_fim": dia_fim.isoformat(),
        },
        "gerado_em": _iso(agora),
        "ciclos": ciclos_json,
        "paradas": _paradas(ciclos, jornada, excluidos),
        "descartados": descartados,
        "limite_ciclo_s": DURACAO_MAXIMA_CICLO_S,
        "lacunas_dados": lacunas,
        "maquinas_periodo": maquinas,
        "disponivel": disponivel,
        "turnos": turnos,
        "feriados": feriados,
        "pausas": pausas,
    }


# ---------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------
@bp.route("/dashboard")
def pagina():
    agora = datetime.now(TZ_LOCAL)
    hoje = agora.date()
    # Antes das 06:30 ainda é o dia produtivo anterior.
    if agora.time() < INICIO_DIA_PRODUTIVO:
        hoje -= timedelta(days=1)
    return render_template("dashboard.html", hoje=hoje.isoformat())


@bp.route("/dashboard/api/dados")
def api_dados():
    try:
        dia_inicio = date.fromisoformat(request.args.get("data_inicio", ""))
        dia_fim = date.fromisoformat(request.args.get("data_fim", ""))
    except ValueError:
        return jsonify(erro="Datas inválidas. Use AAAA-MM-DD."), 400
    if dia_fim < dia_inicio:
        return jsonify(erro="A data final deve ser igual ou posterior à inicial."), 400
    if (dia_fim - dia_inicio).days >= MAX_DIAS:
        return jsonify(erro=f"Período máximo: {MAX_DIAS} dias."), 400

    chave = (dia_inicio, dia_fim)
    agora = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(chave)
        if hit and agora - hit[0] < CACHE_TTL_S:
            return jsonify(hit[1])

    try:
        dados = calcular_dashboard(dia_inicio, dia_fim)
    except RuntimeError as exc:
        log.exception("Falha ao consultar dados do dashboard.")
        return jsonify(erro=str(exc)), 502
    except Exception:
        log.exception("Erro inesperado no dashboard.")
        return jsonify(erro="Erro interno ao calcular o dashboard. Verifique os logs."), 500

    with _cache_lock:
        _cache[chave] = (agora, dados)
        if len(_cache) > 50:
            _cache.pop(next(iter(_cache)))
    return jsonify(dados)

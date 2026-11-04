"""
Dashboard gerencial (Blueprint Flask).

Módulo independente: apenas LÊ as funções já existentes do projeto
(detectar_ciclos_por_ancora, resumir_soldagem, resumir_girador,
consultar_tabelas_influx, horario_turnos) e não altera nenhuma delas.

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
from bisect import bisect_left
from datetime import date, datetime, time, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from config import TZ_LOCAL
from dados import ler_jsonl, parse_iso_utc
from girador import detectar_ciclos_por_ancora, resumir_girador
from influx_client import consultar_tabelas_influx
from soldagem import resumir_soldagem
from turnos import horario_turnos

log = logging.getLogger(__name__)
bp = Blueprint("dashboard", __name__)

BASE_DIR = Path(__file__).resolve().parent

# Dia produtivo: começa às 06:30 e o 2º turno pode ir até 01:40 do dia seguinte.
INICIO_DIA_PRODUTIVO = time(6, 30)
FIM_DIA_PRODUTIVO = time(2, 0)  # do dia seguinte
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


def classificar_turno(dt):
    """Retorna (dia_produtivo, rótulo do turno) para um instante."""
    dia = (dt - timedelta(hours=2)).date()
    horarios = horario_turnos(dia)
    if not horarios:
        return dia, "Fora de turno"
    fim_turno_1 = datetime.combine(dia, horarios[1], tzinfo=TZ_LOCAL)
    return dia, ("1º turno" if dt < fim_turno_1 else "2º turno")


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


def _indexar_soldagem(registros):
    """Ordena registros de soldagem por horário para fatiar por ciclo."""
    itens = []
    for r in registros:
        try:
            itens.append((parse_iso_utc(r["time"]), r))
        except (KeyError, TypeError, ValueError):
            continue
    itens.sort(key=lambda x: x[0])
    return [i[0] for i in itens], [i[1] for i in itens]


def _fatiar(tempos, regs, ini, fim):
    lo = bisect_left(tempos, ini.astimezone(tempos[0].tzinfo)) if tempos else 0
    hi = bisect_left(tempos, fim.astimezone(tempos[0].tzinfo)) if tempos else 0
    return regs[lo:hi]


def _serializar_ciclo(c, arco_ciclo):
    ref = c["etapa_30"]["inicio"]
    dia, turno = classificar_turno(ref)
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
            # Etapa 10 é sintética (fixa 5 min) e a 60 é limitada a 5 min;
            # etapa encerrada à força também não representa duração real.
            "estimada": cod in (10, 60) or (forcado and e["fim"] == c["fim_ciclo"]),
        })

    return {
        "numero": c["numero"],
        "status": c["status"],
        "forcado": forcado,
        "dia": dia.isoformat(),
        "turno": turno,
        "inicio": _iso(c["inicio_ciclo"]),
        "fim": _iso(c["fim_ciclo"]),
        "duracao_s": c["duracao_ciclo"],
        "sequencia": c["sequencia"],
        "etapas": etapas,
        "arco": arco_ciclo,
    }


# ---------------------------------------------------------------------
# Cálculo principal
# ---------------------------------------------------------------------
def calcular_dashboard(dia_inicio, dia_fim):
    janela_inicio = datetime.combine(dia_inicio, INICIO_DIA_PRODUTIVO, tzinfo=TZ_LOCAL)
    janela_fim = datetime.combine(dia_fim + timedelta(days=1), FIM_DIA_PRODUTIVO, tzinfo=TZ_LOCAL)

    reg_soldagem, reg_girador = _carregar_registros(janela_inicio, janela_fim)

    ciclos = detectar_ciclos_por_ancora(reg_girador, janela_inicio, janela_fim)
    resumo_total, _ = resumir_soldagem(reg_soldagem, janela_inicio, janela_fim)
    _, _, lacunas_girador, _ = resumir_girador(reg_girador, janela_inicio, janela_fim)

    tempos_s, regs_s = _indexar_soldagem(reg_soldagem)
    agora = datetime.now(TZ_LOCAL)

    # Ciclos + arco por ciclo
    ciclos_json = []
    for c in ciclos:
        ini = c["inicio_ciclo"]
        fim = c["fim_ciclo"] or min(janela_fim, agora)
        arco = {}
        if fim > ini and tempos_s:
            resumo, _ = resumir_soldagem(_fatiar(tempos_s, regs_s, ini, fim), ini, fim)
            arco = {
                maq: {"arco_s": d["tempo_arco_s"], "corrente_A": d["corrente_media_A"]}
                for maq, d in resumo.items()
            }
        ciclos_json.append(_serializar_ciclo(c, arco))

    # Paradas entre ciclos consecutivos (já sem sobreposição)
    paradas = []
    for atual, prox in zip(ciclos, ciclos[1:]):
        if not atual["fim_ciclo"]:
            continue
        ocioso = (prox["inicio_ciclo"] - atual["fim_ciclo"]).total_seconds()
        if ocioso > 0:
            dia, turno = classificar_turno(atual["fim_ciclo"])
            paradas.append({
                "inicio": _iso(atual["fim_ciclo"]),
                "fim": _iso(prox["inicio_ciclo"]),
                "duracao_s": ocioso,
                "dia": dia.isoformat(),
                "turno": turno,
                "entre": [atual["numero"], prox["numero"]],
            })

    # Lacunas de dados (sensor/rede) — não são paradas de produção
    lacunas = [
        {"fonte": "Girador", "inicio": _iso(a), "fim": _iso(b), "duracao_s": s}
        for a, b, s in lacunas_girador
    ]
    for maq, d in resumo_total.items():
        for a, b, s in d["lacunas"]:
            lacunas.append({"fonte": maq, "inicio": _iso(a), "fim": _iso(b), "duracao_s": s})
    lacunas.sort(key=lambda x: x["inicio"])

    maquinas = {
        maq: {
            "arco_s": d["tempo_arco_s"],
            "corrente_A": d["corrente_media_A"],
            "registros": d["registros"],
        }
        for maq, d in resumo_total.items()
    }

    return {
        "periodo": {
            "inicio": _iso(janela_inicio),
            "fim": _iso(janela_fim),
            "dia_inicio": dia_inicio.isoformat(),
            "dia_fim": dia_fim.isoformat(),
        },
        "gerado_em": _iso(agora),
        "ciclos": ciclos_json,
        "paradas": paradas,
        "lacunas_dados": lacunas,
        "maquinas_periodo": maquinas,
    }


# ---------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------
@bp.route("/dashboard")
def pagina():
    hoje = datetime.now(TZ_LOCAL).date()
    # Antes das 06:30 ainda é o dia produtivo anterior.
    if datetime.now(TZ_LOCAL).time() < INICIO_DIA_PRODUTIVO:
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

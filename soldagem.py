from utils import recortar_intervalo, calcular_uniao_e_corrente
from dados import unix_ms_para_local, parse_iso_utc
from config import TZ_LOCAL

CORRENTE_MINIMA_ARCO_A = 25.0

def resumir_soldagem(registros, janela_inicio, janela_fim):
    por_maquina = {}
    erros = []
    for r in registros:
        maquina = str(r.get("device", "Desconhecida"))
        por_maquina.setdefault(maquina, {
            "intervalos": [],
            "marcadores_zero": 0,
            "corrente_zero": 0,
            "registros_no_periodo": 0,
            "correntes_validas": 0,
            "primeiro_registro": None,
            "ultimo_registro": None,
            "tempos_registro": [],
        })

        try:
            dt_registro = parse_iso_utc(r["time"]).astimezone(TZ_LOCAL)
            ini = unix_ms_para_local(r["start_ts"])
            fim = unix_ms_para_local(r["end_ts"])
            corrente = float(r.get("avg_curr", 0) or 0) * 100.0
        except (KeyError, TypeError, ValueError, OSError) as exc:
            erros.append(f"Registro de soldagem ignorado ({maquina}): {exc}")
            continue

        if not (janela_inicio <= dt_registro < janela_fim):
            continue

        info = por_maquina[maquina]
        info["registros_no_periodo"] += 1
        info["tempos_registro"].append(dt_registro)
        info["primeiro_registro"] = min(info["primeiro_registro"], dt_registro) if info["primeiro_registro"] else dt_registro
        info["ultimo_registro"] = max(info["ultimo_registro"], dt_registro) if info["ultimo_registro"] else dt_registro

        if fim <= ini:
            info["marcadores_zero"] += 1
            continue

        if corrente <= CORRENTE_MINIMA_ARCO_A:
            info["corrente_zero"] += 1
            continue

        recortado = recortar_intervalo(ini, fim, janela_inicio, janela_fim)
        if recortado:
            ini_rec, fim_rec = recortado
            info["intervalos"].append((
                ini_rec.timestamp(),
                fim_rec.timestamp(),
                corrente,
            ))
            info["correntes_validas"] += 1

    resultado = {}
    for maquina, info in por_maquina.items():
        if not info["registros_no_periodo"]:
            continue
        tempo_arco, corrente_media = calcular_uniao_e_corrente(info["intervalos"])
        tempos = sorted(info["tempos_registro"])
        lacunas = []
        for a, b in zip(tempos, tempos[1:]):
            segundos = (b - a).total_seconds()
            if segundos > 90:
                lacunas.append((a, b, segundos))

        resultado[maquina] = {
            "registros": info["registros_no_periodo"],
            "marcadores_zero": info["marcadores_zero"],
            "corrente_zero": info["corrente_zero"],
            "intervalos_positivos": len(info["intervalos"]),
            "tempo_arco_s": tempo_arco,
            "corrente_media_A": corrente_media,
            "primeiro": min(tempos) if tempos else None,
            "ultimo": max(tempos) if tempos else None,
            "lacunas": lacunas,
        }
    return resultado, erros

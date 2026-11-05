from bisect import bisect_right
from collections import Counter
from dados import parse_iso_utc

from datetime import datetime, timedelta
from config import TZ_LOCAL

def resumir_girador(registros, janela_inicio, janela_fim):
    contagem = Counter()
    tempos = []
    erros = []
    for r in registros:
        try:
            dt = parse_iso_utc(r["time"]).astimezone(TZ_LOCAL)
            angulo = angulo_girador_registro(r)
        except (KeyError, TypeError, ValueError) as exc:
            erros.append(f"Registro do girador ignorado: {exc}")
            continue

        if janela_inicio <= dt < janela_fim:
            contagem[angulo] += 1
            tempos.append(dt)

    tempos.sort()
    lacunas = []
    for a, b in zip(tempos, tempos[1:]):
        segundos = (b - a).total_seconds()
        if segundos > 120:
            lacunas.append((a, b, segundos))
    return contagem, tempos, lacunas, erros

def angulo_girador_registro(registro):
    if registro.get("graus") not in (None, ""):
        valor = float(registro["graus"])
        return int(round(valor / 90.0) * 90) % 360

    q = int(float(registro["quadrante"]))
    if q not in (0, 1, 2, 3):
        raise ValueError(f"Quadrante inválido: {q}; esperado 0, 1, 2 ou 3")
    return q * 90

def _primeiro_arco(arcos, fins, ini, fim):
    """Início do primeiro arco de solda entre `ini` e `fim` (datetimes) ou None.

    `arcos`: intervalos de arco já unidos e ordenados, [(ini_ts, fim_ts)];
    `fins`: lista dos fim_ts (mesma ordem). Se um arco já estava aberto em
    `ini`, o início considerado é o próprio `ini`.
    """
    ini_ts, fim_ts = ini.timestamp(), fim.timestamp()
    k = bisect_right(fins, ini_ts)  # primeiro arco que termina depois de `ini`
    if k >= len(arcos) or arcos[k][0] >= fim_ts:
        return None
    return datetime.fromtimestamp(max(arcos[k][0], ini_ts), tz=TZ_LOCAL)


def detectar_ciclos_por_ancora(registros, janela_inicio, janela_fim, arcos=None):
    """
    Detecta ciclos completos (Etapas 10 a 60) pelas âncoras laterais.

    Regras:
    - Etapas 30 e 40: ângulos opostos (90° e 270°).
    - Ruído geral: estado intermediário com duração <= 60 segundos.
    - Ruído específico: 0° -> 270° (<= 90 s) -> 180°.
    - Uma passagem temporária pela posição da etapa 50, seguida do
      retorno à âncora 40, não deve ser considerada etapa 50.
    - Etapa 20: começa no INÍCIO DO PRIMEIRO ARCO de solda depois que o girador
      chega em 0°/180° (e antes da etapa 30). Sem `arcos` (ou sem solda nesse
      trecho), usa o instante em que o girador chegou na posição.
    - Etapa 10: cinco minutos antes da etapa 20.
    - Etapa 60: limitada a cinco minutos.
    """
    LIMITE_RUIDO_270_SEGUNDOS = 90
    fins_arcos = [a[1] for a in arcos] if arcos else []

    # ---------------------------------------------------------
    # 1. Ler registros
    # ---------------------------------------------------------
    leituras = []
    margem_inicio = janela_inicio - timedelta(hours=6)
    margem_fim = janela_fim + timedelta(hours=2)

    for r in registros:
        try:
            dt = parse_iso_utc(r["time"]).astimezone(TZ_LOCAL)
            ang = angulo_girador_registro(r)
        except (KeyError, TypeError, ValueError):
            continue

        if margem_inicio <= dt <= margem_fim:
            leituras.append((dt, ang))

    leituras.sort(key=lambda x: x[0])

    # ---------------------------------------------------------
    # 2. Compactar mudanças de ângulo
    # ---------------------------------------------------------
    estados = []

    for dt, ang in leituras:
        if not estados or estados[-1]["angulo"] != ang:
            estados.append({"dt": dt, "angulo": ang})

    # ---------------------------------------------------------
    # 3. Remover ruído específico 0 -> 270 -> 180
    # ---------------------------------------------------------
    i_estado = 1

    while i_estado < len(estados) - 1:
        anterior = estados[i_estado - 1]
        atual = estados[i_estado]
        seguinte = estados[i_estado + 1]

        duracao = (seguinte["dt"] - atual["dt"]).total_seconds()

        if (
            anterior["angulo"] == 0
            and atual["angulo"] == 270
            and seguinte["angulo"] == 180
            and 0 <= duracao <= LIMITE_RUIDO_270_SEGUNDOS
        ):
            estados.pop(i_estado)
            continue

        i_estado += 1

    # ---------------------------------------------------------
    # 4. Remover ruídos gerais e fragmentações (e recompactar)
    # ---------------------------------------------------------
    LIMITE_RUIDO_TRANSICAO = 60
    LIMITE_FRAGMENTACAO = 600  # 10 minutos

    mudou = True

    while mudou and len(estados) >= 3:
        mudou = False
        i_estado = 1

        while i_estado < len(estados) - 1:
            anterior = estados[i_estado - 1]
            atual = estados[i_estado]
            seguinte = estados[i_estado + 1]

            duracao = (seguinte["dt"] - atual["dt"]).total_seconds()

            # Regra 1: Ruído rápido de transição (<= 60s) afeta qualquer ângulo
            eh_ruido_transicao = (duracao <= LIMITE_RUIDO_TRANSICAO)
            
            # Regra 2: Fragmentação "Bate e volta" (A -> B -> A)
            # Protege 0º e 180º, garantindo que etapas válidas curtas não sejam apagadas
            eh_fragmentacao = (
                anterior["angulo"] == seguinte["angulo"] 
                and duracao <= LIMITE_FRAGMENTACAO
                and atual["angulo"] not in (0, 180)
            )

            if eh_ruido_transicao or eh_fragmentacao:
                estados.pop(i_estado)
                mudou = True
                break # Interrompe para recompactar imediatamente

            i_estado += 1

        # Recompacta a lista fundindo blocos do mesmo ângulo
        if mudou:
            estados_recompactados = []
            for est in estados:
                if not estados_recompactados or estados_recompactados[-1]["angulo"] != est["angulo"]:
                    estados_recompactados.append(est)
            estados = estados_recompactados

    # ---------------------------------------------------------
    # 5. Auxiliar: extrair etapa real
    # ---------------------------------------------------------
    def get_etapa_real(idx):
        if idx is None or idx < 0 or idx >= len(estados):
            return None

        dt_inicio = estados[idx]["dt"]
        dt_fim = (
            estados[idx + 1]["dt"]
            if idx + 1 < len(estados)
            else None
        )

        return {
            "angulo": estados[idx]["angulo"],
            "inicio": dt_inicio,
            "fim": dt_fim,
            "duracao": (
                (dt_fim - dt_inicio).total_seconds()
                if dt_fim else None
            ),
            "status": "Finalizada" if dt_fim else "Em andamento"
        }

    # ---------------------------------------------------------
    # 6. Detectar ciclos
    # ---------------------------------------------------------
    ciclos = []
    i = 0
    numero_ciclo = 1

    while i < len(estados):
        ang_30 = estados[i]["angulo"]

        if ang_30 not in (90, 270):
            i += 1
            continue

        idx_30 = i
        idx_20 = idx_30 - 1

        # Etapa 20 deve estar em 0° ou 180°.
        if (
            idx_20 < 0
            or estados[idx_20]["angulo"] not in (0, 180)
        ):
            i += 1
            continue

        ang_esperado_20 = estados[idx_20]["angulo"]
        ang_oposto_20 = 180 if ang_esperado_20 == 0 else 0

        # Procurar a primeira âncora lateral oposta: etapa 40.
        idx_40 = -1
        ang_40 = 270 if ang_30 == 90 else 90

        for j in range(idx_30 + 1, len(estados)):
            if estados[j]["angulo"] in (90, 270):
                if estados[j]["angulo"] == ang_40:
                    idx_40 = j
                break

        if idx_40 == -1:
            i += 1
            continue

        # -----------------------------------------------------
        # 7. Procurar as etapas 50 e 60
        #
        # Regra fundamental:
        # Se a posição candidata à etapa 50 for seguida de um
        # retorno à âncora 40, ela é uma passagem temporária.
        # Continuar procurando a próxima ocorrência da posição.
        # -----------------------------------------------------
        idx_50 = -1
        idx_60 = -1

        j = idx_40 + 1

        while j < len(estados):
            ang = estados[j]["angulo"]

            # Encontrou a posição esperada para a etapa 50.
            if ang == ang_oposto_20:
                # Se a próxima posição for novamente a âncora 40,
                # esta passagem é temporária: não é a etapa 50.
                if (
                    j + 1 < len(estados)
                    and estados[j + 1]["angulo"] == ang_40
                ):
                    j += 2
                    continue

                # Etapa 50 confirmada.
                idx_50 = j

                # Procurar a etapa 60, correspondente à posição
                # esperada da etapa 20.
                k = idx_50 + 1

                while k < len(estados):
                    ang_k = estados[k]["angulo"]

                    if ang_k == ang_esperado_20:
                        idx_60 = k
                        break

                    # Uma nova posição lateral indica outro
                    # movimento; não atravessar outra âncora.
                    if ang_k in (90, 270):
                        break

                    k += 1

                break

            # Retorno à posição da etapa 20: etapa 50 foi pulada.
            if ang == ang_esperado_20:
                idx_60 = j
                break

            # Uma posição lateral diferente interrompe a busca.
            if ang in (90, 270):
                break

            j += 1

        # -----------------------------------------------------
        # 8. Construir dados das etapas
        # -----------------------------------------------------
        e_20 = get_etapa_real(idx_20)
        e_30 = get_etapa_real(idx_30)
        e_40 = get_etapa_real(idx_40)
        e_50 = get_etapa_real(idx_50)
        e_60 = get_etapa_real(idx_60)

        # Etapa 20 passa a começar no primeiro arco de solda, não no giro.
        if arcos and e_20 and e_20["fim"]:
            inicio_arco = _primeiro_arco(arcos, fins_arcos, e_20["inicio"], e_20["fim"])
            if inicio_arco:
                e_20["inicio_giro"] = e_20["inicio"]
                e_20["inicio"] = inicio_arco
                e_20["duracao"] = (e_20["fim"] - inicio_arco).total_seconds()

        # Etapa 10 sintética: cinco minutos antes da etapa 20.
        e_10 = None

        if e_20 and e_20["inicio"]:
            e_10 = {
                "angulo": ang_oposto_20,
                "inicio": e_20["inicio"] - timedelta(minutes=5),
                "fim": e_20["inicio"],
                "duracao": 300.0,
                "status": "Finalizada"
            }

        # Limitar a etapa 60 a cinco minutos.
        if e_60 and e_60["inicio"]:
            limite_fim = e_60["inicio"] + timedelta(minutes=5)

            if not e_60["fim"] or e_60["fim"] > limite_fim:
                e_60["fim"] = limite_fim
                e_60["duracao"] = 300.0
                e_60["status"] = "Finalizada"

        seq_angles = [
            f'{estados[idx]["angulo"]}°'
            for idx in (idx_20, idx_30, idx_40, idx_50, idx_60)
            if idx is not None and idx >= 0
        ]

        inicio_ciclo = (
            e_10["inicio"]
            if e_10
            else e_20["inicio"] if e_20
            else e_30["inicio"]
        )

        fim_ciclo = (
            e_60["fim"]
            if e_60 and e_60["fim"]
            else e_60["inicio"] if e_60
            else None
        )

        ciclos.append({
            "numero": numero_ciclo,
            "inicio_ciclo": inicio_ciclo,
            "fim_ciclo": fim_ciclo,
            "duracao_ciclo": (
                (fim_ciclo - inicio_ciclo).total_seconds()
                if inicio_ciclo and fim_ciclo else None
            ),
            "sequencia": "→".join(seq_angles),
            "etapa_10": e_10,
            "etapa_20": e_20,
            "etapa_30": e_30,
            "etapa_40": e_40,
            "etapa_50": e_50,
            "etapa_60": e_60,
            "status": "Completo" if idx_60 != -1 else "Em andamento"
        })

        numero_ciclo += 1

        # Avançar para evitar reutilizar as âncoras do mesmo ciclo.
        i = idx_60 if idx_60 != -1 else idx_40
        i += 1

    # ---------------------------------------------------------
    # 8.1 Finalizar ciclos sem etapa 60 quando já existe um próximo
    #
    # Se um novo ciclo foi identificado, o anterior obrigatoriamente
    # terminou: encerra 5 minutos antes do início do próximo ciclo.
    # (Feito antes do filtro de janela, para que o próximo ciclo
    # seja considerado mesmo que esteja fora da janela.)
    # ---------------------------------------------------------
    for atual, proximo in zip(ciclos, ciclos[1:]):
        if atual["fim_ciclo"] is not None:
            continue

        # Última etapa detectada do ciclo (50 > 40 > 30 ...).
        ultima = next(
            (atual[k] for k in ("etapa_50", "etapa_40", "etapa_30", "etapa_20")
             if atual[k]),
            None,
        )

        limite = proximo["inicio_ciclo"] - timedelta(minutes=5)
        if ultima and limite < ultima["inicio"]:
            # Ciclos muito próximos: não permitir fim antes do início da última etapa.
            limite = ultima["inicio"]

        if ultima and (ultima["fim"] is None or ultima["fim"] > limite):
            ultima["fim"] = limite
            ultima["duracao"] = (limite - ultima["inicio"]).total_seconds()
            ultima["status"] = "Finalizada"

        atual["fim_ciclo"] = limite
        atual["duracao_ciclo"] = (limite - atual["inicio_ciclo"]).total_seconds()
        atual["status"] = "Completo"
        atual["fim_forcado"] = True

    # ---------------------------------------------------------
    # 8.2 Garantir que ciclos consecutivos não se sobreponham
    #
    # A etapa 10 do próximo ciclo é sintética (5 min antes da etapa 20)
    # e pode invadir a etapa 60 / última etapa real do ciclo anterior.
    # Nesse caso, ela é encurtada (ou removida) para começar no fim
    # do ciclo anterior.
    # ---------------------------------------------------------
    for atual, proximo in zip(ciclos, ciclos[1:]):
        fim_atual = atual["fim_ciclo"]
        if fim_atual is None or fim_atual <= proximo["inicio_ciclo"]:
            continue

        e10 = proximo["etapa_10"]
        e20 = proximo["etapa_20"]
        if e10 and fim_atual < e20["inicio"]:
            e10["inicio"] = fim_atual
            e10["duracao"] = (e10["fim"] - fim_atual).total_seconds()
            proximo["inicio_ciclo"] = fim_atual
        else:
            proximo["etapa_10"] = None
            proximo["inicio_ciclo"] = e20["inicio"]

        fim_prox = proximo["fim_ciclo"]
        proximo["duracao_ciclo"] = (
            (fim_prox - proximo["inicio_ciclo"]).total_seconds()
            if fim_prox else None
        )

    # ---------------------------------------------------------
    # 9. Filtrar pela janela solicitada
    # ---------------------------------------------------------
    return [
        c for c in ciclos
        if (
            c["etapa_30"]
            and janela_inicio <= c["etapa_30"]["inicio"] <= janela_fim
        )
    ]
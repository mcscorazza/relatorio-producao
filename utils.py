from datetime import datetime, date, time, timedelta
from config import TZ_LOCAL

def formatar_datahora(dt):
    return dt.astimezone(TZ_LOCAL).strftime("%d/%m/%Y %H:%M:%S") if dt else "—"


def formatar_hms(segundos):
    if segundos is None: return "—"
    segundos = max(0, int(round(segundos)))
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    if horas > 0:
        return f"{horas:02d}:{minutos:02d}:{segundos:02d}"
    return f"{minutos:02d}:{segundos:02d}"


def numero_br(valor, casas=1):
    return f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def montar_intervalo_local(data_str, hora_inicio, hora_fim):
    dia = date.fromisoformat(data_str)
    h_ini = time.fromisoformat(hora_inicio)
    h_fim = time.fromisoformat(hora_fim)
    inicio = datetime.combine(dia, h_ini, tzinfo=TZ_LOCAL)
    fim = datetime.combine(dia, h_fim, tzinfo=TZ_LOCAL)
    if fim <= inicio:
        fim += timedelta(days=1)
    return inicio, fim


def recortar_intervalo(inicio, fim, janela_inicio, janela_fim):
    inicio = max(inicio, janela_inicio)
    fim = min(fim, janela_fim)
    return (inicio, fim) if fim > inicio else None


def calcular_uniao_e_corrente(intervalos):
    eventos = []
    for ini, fim, corrente in intervalos:
        if fim <= ini:
            continue
        eventos.append((ini, 1, corrente))
        eventos.append((fim, -1, corrente))

    if not eventos:
        return 0.0, None

    eventos.sort(key=lambda item: item[0])
    ativos = []
    duracao_total = 0.0
    ampere_segundos = 0.0
    instante_anterior = eventos[0][0]
    i = 0

    while i < len(eventos):
        instante = eventos[i][0]
        trecho = instante - instante_anterior
        if trecho > 0 and ativos:
            corrente_media_trecho = sum(ativos) / len(ativos)
            duracao_total += trecho
            ampere_segundos += corrente_media_trecho * trecho

        mesmo_instante = []
        while i < len(eventos) and eventos[i][0] == instante:
            mesmo_instante.append(eventos[i])
            i += 1

        for _, tipo, corrente in mesmo_instante:
            if tipo == -1:
                try:
                    ativos.remove(corrente)
                except ValueError:
                    pass
        for _, tipo, corrente in mesmo_instante:
            if tipo == 1:
                ativos.append(corrente)

        instante_anterior = instante

    corrente_media = ampere_segundos / duracao_total if duracao_total else None
    return duracao_total, corrente_media

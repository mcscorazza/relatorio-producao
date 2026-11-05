"""
Jornada produtiva: turnos menos pausas (almoço/janta).

Só o tempo dentro dos segmentos produtivos entra nos totais do dashboard.
Fora dos turnos (noite, finais de semana) e durante as pausas, o tempo é
descartado.

As pausas de refeição duram 1h, mas o horário não é fixo (almoço entre 11h e
14h, janta entre 19h e 21h). Regra:
  1. Se não houve solda no horário padrão (almoço 12:00-13:00, janta
     19:30-20:30), usa-se o horário padrão.
  2. Caso contrário, a pausa é inferida pelos dados: dentro da janela de
     refeição, escolhe-se a hora corrida com MENOS arco aberto (união das
     máquinas); em empate, a do meio do trecho empatado.
"""
from bisect import bisect_left, bisect_right
from datetime import datetime, time, timedelta

from config import TZ_LOCAL
from soldagem import CORRENTE_MINIMA_ARCO_A
from turnos import fim_turno_2, horario_turnos
from utils import calcular_uniao_e_corrente

# (nome, início da janela possível, fim da janela possível, início padrão)
REFEICOES = (
    ("Almoço", time(11, 0), time(14, 0), time(12, 0)),
    ("Janta", time(19, 0), time(21, 0), time(19, 30)),
)
DURACAO_REFEICAO_S = 3600
# Arco (s) tolerado no horário padrão para ainda considerá-lo "sem solda".
TOLERANCIA_ARCO_PADRAO_S = 5.0
PASSO_BUSCA_S = 60


# ---------------------------------------------------------------------
# Arco: intervalos por máquina
# ---------------------------------------------------------------------
def intervalos_de_arco(registros):
    """{máquina: [(ini_ts, fim_ts, corrente_A)]} com os mesmos filtros de soldagem.py."""
    por_maquina = {}
    for r in registros:
        try:
            ini = float(r["start_ts"]) / 1000.0
            fim = float(r["end_ts"]) / 1000.0
            corrente = float(r.get("avg_curr", 0) or 0) * 100.0
        except (KeyError, TypeError, ValueError):
            continue
        if fim <= ini or corrente <= CORRENTE_MINIMA_ARCO_A:
            continue
        por_maquina.setdefault(str(r.get("device", "Desconhecida")), []).append((ini, fim, corrente))
    return por_maquina


def _uniao(intervalos):
    """Une intervalos (ini, fim) sobrepostos; retorna lista ordenada."""
    unidos = []
    for ini, fim in sorted((i[0], i[1]) for i in intervalos):
        if unidos and ini <= unidos[-1][1]:
            if fim > unidos[-1][1]:
                unidos[-1][1] = fim
        else:
            unidos.append([ini, fim])
    return unidos


def uniao_de_arcos(arcos_por_maquina):
    """Arcos de todas as máquinas unidos e ordenados: [(ini_ts, fim_ts)]."""
    return [
        (a, b)
        for a, b in _uniao([(i[0], i[1]) for ivs in arcos_por_maquina.values() for i in ivs])
    ]


class IndiceArco:
    """Intervalos de uma máquina, com busca rápida por janela."""

    def __init__(self, intervalos):
        self.itens = sorted(intervalos)
        self.inicios = [i[0] for i in self.itens]
        self.maior = max((i[1] - i[0] for i in self.itens), default=0.0)

    def arco_em(self, trechos):
        """trechos: [(ini_ts, fim_ts)]. Retorna (segundos de arco, corrente média A)."""
        cortados = []
        for t_ini, t_fim in trechos:
            lo = bisect_left(self.inicios, t_ini - self.maior)
            hi = bisect_right(self.inicios, t_fim)
            for ini, fim, corrente in self.itens[lo:hi]:
                a, b = max(ini, t_ini), min(fim, t_fim)
                if b > a:
                    cortados.append((a, b, corrente))
        return calcular_uniao_e_corrente(cortados)


# ---------------------------------------------------------------------
# Jornada
# ---------------------------------------------------------------------
def _arco_na_janela(uniao, a, b):
    total = 0.0
    for ini, fim in uniao:
        if fim <= a:
            continue
        if ini >= b:
            break
        total += min(fim, b) - max(ini, a)
    return total


def detectar_pausa(uniao_arco, ini_janela, fim_janela, inicio_padrao=None):
    """Pausa de 1h dentro de [ini_janela, fim_janela] (datetimes).

    Usa o horário padrão se não houve solda nele; senão, a hora corrida com
    menos arco da janela.
    """
    a_ts, b_ts = ini_janela.timestamp(), fim_janela.timestamp()
    relevantes = [i for i in uniao_arco if i[1] > a_ts and i[0] < b_ts]

    if inicio_padrao is not None:
        p_ts = inicio_padrao.timestamp()
        if _arco_na_janela(relevantes, p_ts, p_ts + DURACAO_REFEICAO_S) <= TOLERANCIA_ARCO_PADRAO_S:
            return inicio_padrao, inicio_padrao + timedelta(seconds=DURACAO_REFEICAO_S)

    melhor, empatados = None, []
    t = a_ts
    while t + DURACAO_REFEICAO_S <= b_ts + 1e-6:
        valor = _arco_na_janela(relevantes, t, t + DURACAO_REFEICAO_S)
        if melhor is None or valor < melhor - 1.0:
            melhor, empatados = valor, [t]
        elif abs(valor - melhor) <= 1.0:
            empatados.append(t)
        t += PASSO_BUSCA_S

    inicio_ts = empatados[len(empatados) // 2]
    inicio = datetime.fromtimestamp(inicio_ts, tz=TZ_LOCAL)
    return inicio, inicio + timedelta(seconds=DURACAO_REFEICAO_S)


class Jornada:
    def __init__(self, dia_inicio, dia_fim, arcos_por_maquina, agora):
        uniao_arco = _uniao(
            [(i[0], i[1]) for ivs in arcos_por_maquina.values() for i in ivs]
        )
        self.turnos = []    # (ini, fim, dia, rótulo) — turno completo
        self.pausas = []    # (ini, fim, dia, nome)
        self.segmentos = []  # (ini, fim, ini_ts, fim_ts, dia, rótulo) — tempo produtivo

        dia = dia_inicio
        while dia <= dia_fim:
            h = horario_turnos(dia)
            if h:
                ini1 = datetime.combine(dia, h[0], tzinfo=TZ_LOCAL)
                fim1 = datetime.combine(dia, h[1], tzinfo=TZ_LOCAL)
                fim2 = fim_turno_2(dia)
                turnos_dia = [(ini1, fim1, dia, "1º turno"), (fim1, fim2, dia, "2º turno")]
                self.turnos.extend(turnos_dia)

                pausas_dia = []
                for nome, a, b, padrao in REFEICOES:
                    ra = datetime.combine(dia, a, tzinfo=TZ_LOCAL)
                    rb = datetime.combine(dia, b, tzinfo=TZ_LOCAL)
                    if agora < rb:  # refeição ainda não terminou: não dá para inferir
                        continue
                    p_ini, p_fim = detectar_pausa(
                        uniao_arco, ra, rb, datetime.combine(dia, padrao, tzinfo=TZ_LOCAL)
                    )
                    pausas_dia.append((p_ini, p_fim, dia, nome))
                self.pausas.extend(pausas_dia)

                for t_ini, t_fim, d, rotulo in turnos_dia:
                    cursor = t_ini
                    for p_ini, p_fim, _, _ in sorted(pausas_dia):
                        if p_fim <= cursor or p_ini >= t_fim:
                            continue
                        if p_ini > cursor:
                            self._add(cursor, p_ini, d, rotulo)
                        cursor = max(cursor, p_fim)
                    if cursor < t_fim:
                        self._add(cursor, t_fim, d, rotulo)
            dia += timedelta(days=1)

        self.segmentos.sort(key=lambda s: s[2])
        self._fins = [s[3] for s in self.segmentos]

    def _add(self, ini, fim, dia, rotulo):
        self.segmentos.append((ini, fim, ini.timestamp(), fim.timestamp(), dia, rotulo))

    def fatiar(self, ini, fim):
        """Trechos produtivos dentro de [ini, fim]: [(ini, fim, dia, rótulo)]."""
        a_ts, b_ts = ini.timestamp(), fim.timestamp()
        saida = []
        k = bisect_right(self._fins, a_ts)
        while k < len(self.segmentos) and self.segmentos[k][2] < b_ts:
            s_ini, s_fim, s_ini_ts, s_fim_ts, dia, rotulo = self.segmentos[k]
            a, b = max(s_ini_ts, a_ts), min(s_fim_ts, b_ts)
            if b > a:
                saida.append((
                    datetime.fromtimestamp(a, tz=TZ_LOCAL),
                    datetime.fromtimestamp(b, tz=TZ_LOCAL),
                    dia, rotulo,
                ))
            k += 1
        return saida

    def trechos_ts(self, ini, fim):
        return [(a.timestamp(), b.timestamp()) for a, b, _, _ in self.fatiar(ini, fim)]

    def segundos(self, ini, fim):
        if not ini or not fim or fim <= ini:
            return 0.0
        return sum((b - a).total_seconds() for a, b, _, _ in self.fatiar(ini, fim))

    def turno_de(self, dt):
        """(dia produtivo, rótulo) do instante; 'Fora de turno' se fora dos turnos."""
        for t_ini, t_fim, dia, rotulo in self.turnos:
            if t_ini <= dt < t_fim:
                return dia, rotulo
        return (dt - timedelta(hours=2)).date(), "Fora de turno"

    def disponivel(self, ate):
        """{(dia, rótulo): segundos produtivos} até o instante 'ate'."""
        por_turno = {}
        for ini, fim, _, _, dia, rotulo in self.segmentos:
            fim = min(fim, ate)
            if fim > ini:
                por_turno[(dia, rotulo)] = por_turno.get((dia, rotulo), 0.0) + (fim - ini).total_seconds()
        return por_turno

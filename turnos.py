from datetime import time, datetime, timedelta
from config import TZ_LOCAL

def horario_turnos(dia):
    """Retorna os horários dos dois turnos do dia de produção."""
    if dia.weekday() == 4:  # sexta-feira
        return (time(6, 30), time(14, 20), time(22, 0))
    if dia.weekday() < 4:  # segunda a quinta-feira
        return (time(6, 30), time(16, 20), time(1, 40))
    return None


def fim_turno_2(dia):
    horarios = horario_turnos(dia)
    if not horarios:
        return datetime.combine(dia, time(0, 0), tzinfo=TZ_LOCAL)
    fim = datetime.combine(dia, horarios[2], tzinfo=TZ_LOCAL)
    if dia.weekday() < 4:
        fim += timedelta(days=1)
    return fim

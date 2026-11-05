import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from config import TZ_LOCAL

log = logging.getLogger(__name__)

# Arquivo de feriados / dias sem turno (editável a qualquer momento; é relido
# sozinho quando muda, sem precisar reiniciar). Pode ser trocado pela variável
# de ambiente FERIADOS_FILE.
ARQUIVO_FERIADOS = Path(
    os.environ.get("FERIADOS_FILE") or Path(__file__).resolve().parent / "feriados.txt"
)

_cache_feriados = {"mtime": None, "dados": {}}


def _parse_data(texto):
    """Aceita AAAA-MM-DD ou DD/MM/AAAA."""
    texto = texto.strip()
    if "/" in texto:
        d, m, a = texto.split("/")
        return date(int(a), int(m), int(d))
    return date.fromisoformat(texto)


def carregar_feriados():
    """{data: descrição} lido de feriados.txt (cache por data de modificação).

    Formato, uma entrada por linha ('#' inicia comentário):
        2026-11-02  Finados
        15/11/2026  Proclamação da República
        2026-12-24..2026-12-26  Recesso de fim de ano     (intervalo)
    """
    try:
        mtime = ARQUIVO_FERIADOS.stat().st_mtime
    except OSError:
        return {}
    if _cache_feriados["mtime"] == mtime:
        return _cache_feriados["dados"]

    dados = {}
    with ARQUIVO_FERIADOS.open(encoding="utf-8-sig") as arquivo:
        for numero, linha in enumerate(arquivo, start=1):
            linha = linha.split("#", 1)[0].strip()
            if not linha:
                continue
            token, _, descricao = linha.partition(" ")
            descricao = descricao.strip() or "Feriado"
            try:
                if ".." in token:
                    a, b = token.split("..", 1)
                    inicio, fim = _parse_data(a), _parse_data(b)
                    if fim < inicio:
                        raise ValueError("intervalo invertido")
                    dia = inicio
                    while dia <= fim:
                        dados[dia] = descricao
                        dia += timedelta(days=1)
                else:
                    dados[_parse_data(token)] = descricao
            except ValueError:
                log.warning("%s linha %d ignorada (data inválida): %r", ARQUIVO_FERIADOS.name, numero, linha)

    _cache_feriados["mtime"], _cache_feriados["dados"] = mtime, dados
    return dados


def feriado(dia):
    """Descrição do feriado em `dia` (date) ou None."""
    return carregar_feriados().get(dia)


def horario_turnos(dia):
    """Retorna os horários dos dois turnos do dia de produção.

    Sábados, domingos e dias listados em feriados.txt não têm turno (None).
    """
    if feriado(dia):
        return None
    if dia.weekday() == 4:  # sexta-feira
        return (time(6, 30), time(14, 20), time(21, 40))
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

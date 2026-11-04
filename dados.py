import json
from datetime import datetime
from config import TZ_LOCAL, TZ_UTC

def ler_jsonl(caminho):
    """Lê um arquivo JSONL, ignorando linhas em branco."""
    registros = []
    with open(caminho, "r", encoding="utf-8-sig") as arquivo:
        for numero, linha in enumerate(arquivo, start=1):
            linha = linha.strip()
            if not linha:
                continue
            try:
                registros.append(json.loads(linha))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSON inválido em {caminho}, linha {numero}: {exc}"
                ) from exc
    return registros

def parse_iso_utc(valor):
    """Converte ISO timestamp para datetime consciente de fuso."""
    dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_UTC)
    return dt.astimezone(TZ_UTC)


def unix_ms_para_local(valor):
    return datetime.fromtimestamp(float(valor) / 1000.0, tz=TZ_UTC).astimezone(TZ_LOCAL)


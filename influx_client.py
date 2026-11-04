import urllib.error
import urllib.request
import json
from config import TZ_UTC

def consultar_influx(api_url, banco, token, sql):
    """Consulta a API SQL do InfluxDB 3 e retorna registros JSONL como dicionários."""
    endpoint = api_url.rstrip("/") + "/api/v3/query_sql?format=json"
    payload = json.dumps({"db": banco, "q": sql}).encode("utf-8")
    requisicao = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(requisicao, timeout=90) as resposta:
            texto = resposta.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Erro HTTP {exc.code} ao consultar InfluxDB: {detalhe}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Não foi possível acessar {endpoint}. Verifique se o túnel SSH está aberto. "
            f"Detalhe: {exc.reason}"
        ) from exc

    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = None

    if isinstance(dados, dict):
        for chave in ("data", "records", "results"):
            if isinstance(dados.get(chave), list):
                dados = dados[chave]
                break
        else:
            return [dados]

    if isinstance(dados, list):
        if all(isinstance(item, dict) for item in dados):
            return dados
        if not dados:
            return []
        raise ValueError(
            "A API retornou uma lista, mas seus elementos não são objetos JSON. "
            f"Primeiro elemento recebido: {str(dados[0])[:300]}"
        )

    registros = []
    for numero, linha in enumerate(texto.splitlines(), start=1):
        linha = linha.strip()
        if not linha:
            continue
        try:
            item = json.loads(linha)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"A API não retornou JSON/JSONL válido na linha {numero}: {linha[:200]}"
            ) from exc
        if isinstance(item, dict):
            registros.append(item)
        elif isinstance(item, list) and all(isinstance(x, dict) for x in item):
            registros.extend(item)
        else:
            raise ValueError(
                "A API retornou um registro que não é objeto JSON. "
                f"Linha {numero}: {linha[:300]}"
            )
    return registros


def consultar_tabelas_influx(api_url, banco, token, janela_inicio, janela_fim):
    """Busca diretamente as duas tabelas para a janela local selecionada."""
    inicio_utc = janela_inicio.astimezone(TZ_UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    fim_utc = janela_fim.astimezone(TZ_UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    filtro_tempo = (
        f"time >= '{inicio_utc}' AND time < '{fim_utc}'"
    )
    sql_soldagem = f"SELECT * FROM welding_events WHERE {filtro_tempo}"
    sql_girador = f"SELECT * FROM posicao_girador WHERE {filtro_tempo}"

    print(f"Consultando InfluxDB em {api_url} (banco: {banco})...")
    registros_soldagem = consultar_influx(api_url, banco, token, sql_soldagem)
    print(f"  welding_events: {len(registros_soldagem)} registros")
    registros_girador = consultar_influx(api_url, banco, token, sql_girador)
    print(f"  posicao_girador: {len(registros_girador)} registros")
    return registros_soldagem, registros_girador


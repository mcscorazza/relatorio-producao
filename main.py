#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
from datetime import date, datetime, time
import os
from pathlib import Path
from dados import ler_jsonl
from girador import detectar_ciclos_por_ancora, resumir_girador
from periodos import intervalos_de_arco, uniao_de_arcos
from html_renderer import gerar_html_novo
from influx_client import consultar_tabelas_influx
from soldagem import resumir_soldagem
from ssh_tunnel import abrir_tunel_ssh, carregar_env

from utils import formatar_datahora, formatar_hms, numero_br
from config import TZ_LOCAL

def main():
    parser = argparse.ArgumentParser(
        description="VERSÃO V15 — Ciclos com sequência angular flexível, resumo de arco aberto e posições do girador."
    )
    parser.add_argument("--soldagem", help="Arquivo JSONL exportado de welding_events (opcional)")
    parser.add_argument("--girador", help="Arquivo JSONL exportado de posicao_girador (opcional)")
    parser.add_argument("--data", help="Data única no formato AAAA-MM-DD (compatibilidade; use --data-inicio/--data-fim para intervalo)")
    parser.add_argument("--data-inicio", help="Data inicial da janela (AAAA-MM-DD)")
    parser.add_argument("--data-fim", help="Data final da janela (AAAA-MM-DD)")
    parser.add_argument("--inicio", default="06:30", help="Início da janela local (HH:MM); padrão 06:30")
    parser.add_argument("--fim", default="11:00", help="Fim da janela local (HH:MM); padrão 11:00")
    parser.add_argument("--saida", default="relatorio_producao_v15.html", help="Arquivo HTML de saída")
    parser.add_argument("--api-url", default=None, help="URL local da API InfluxDB")
    parser.add_argument("--ssh-key", default=None, help="Caminho da chave SSH")
    parser.add_argument("--bastion", default=None, help="Usuário@DNS/IP do bastion")
    parser.add_argument("--private-host", default=None, help="IP/host privado")
    parser.add_argument("--local-port", type=int, default=None, help="Porta local do túnel")
    parser.add_argument("--remote-port", type=int, default=None, help="Porta do InfluxDB na EC2 privada")
    parser.add_argument("--no-ssh-tunnel", action="store_true", help="Não abrir o túnel automaticamente")
    parser.add_argument("--db", default="manufatura", help="Nome do banco InfluxDB; padrão: manufatura")
    parser.add_argument("--token-env", default="INFLUX_TOKEN", help="Nome da variável ambiente com o token")
    args = parser.parse_args()

    carregar_env()
    chave_ssh = args.ssh_key or os.environ.get("SSH_KEY_PATH", r"C:\.ssh\vm-ec2-key.pem")
    bastion = args.bastion or os.environ.get(
        "SSH_BASTION", "ubuntu@ec2-54-233-198-254.sa-east-1.compute.amazonaws.com"
    )
    host_privado = args.private_host or os.environ.get("SSH_PRIVATE_HOST", "10.20.0.239")
    usuario_privado = os.environ.get("SSH_PRIVATE_USER", "ubuntu")
    porta_local = args.local_port or int(os.environ.get("SSH_LOCAL_PORT", "18181"))
    porta_remota = args.remote_port or int(os.environ.get("SSH_REMOTE_PORT", "8181"))
    args.api_url = args.api_url or f"http://127.0.0.1:{porta_local}"

    data_inicio_str = args.data_inicio or args.data
    data_fim_str = args.data_fim or args.data
    if not data_inicio_str or not data_fim_str:
        parser.error("Informe --data para um único dia ou --data-inicio e --data-fim para um intervalo.")
    try:
        dia_inicio = date.fromisoformat(data_inicio_str)
        dia_fim = date.fromisoformat(data_fim_str)
    except ValueError:
        parser.error("Datas inválidas. Use o formato AAAA-MM-DD.")
    if dia_fim < dia_inicio:
        parser.error("A data final deve ser igual ou posterior à data inicial.")
    janela_inicio = datetime.combine(dia_inicio, time.fromisoformat(args.inicio), tzinfo=TZ_LOCAL)
    janela_fim = datetime.combine(dia_fim, time.fromisoformat(args.fim), tzinfo=TZ_LOCAL)
    if janela_fim <= janela_inicio:
        parser.error("O fim da janela deve ser posterior ao início.")

    if bool(args.soldagem) != bool(args.girador):
        parser.error("Informe --soldagem e --girador juntos, ou não informe nenhum para consultar o InfluxDB.")

    if args.soldagem and args.girador:
        registros_soldagem = ler_jsonl(args.soldagem)
        registros_girador = ler_jsonl(args.girador)
    else:
        token = os.environ.get(args.token_env)
        if not token:
            parser.error(
                f"Token não encontrado na variável de ambiente {args.token_env}. "
                f"Defina-a no arquivo .env ao lado do script ou no ambiente do sistema."
            )
        if not args.no_ssh_tunnel:
            abrir_tunel_ssh(
                chave_ssh, bastion, usuario_privado, host_privado, porta_local, porta_remota
            )
        registros_soldagem, registros_girador = consultar_tabelas_influx(
            args.api_url, args.db, token, janela_inicio, janela_fim
        )

    # Chamada com as duas variáveis preservadas para evitar erro unpacking
    resumo_soldagem, erros_soldagem = resumir_soldagem(registros_soldagem, janela_inicio, janela_fim)
    resumo_girador, tempos_girador, lacunas_girador, erros_girador = resumir_girador(registros_girador, janela_inicio, janela_fim)

    # Executar nova lógica analítica baseada em âncoras (30 e 40)
    ciclos = detectar_ciclos_por_ancora(
        registros_girador, janela_inicio, janela_fim,
        arcos=uniao_de_arcos(intervalos_de_arco(registros_soldagem)),
    )

    # Geração do HTML em tabela única e expansível
    gerar_html_novo(
        args.saida, args.data, janela_inicio, janela_fim, ciclos,
        resumo_soldagem, resumo_girador, tempos_girador, lacunas_girador
    )

    print(f"\nRelatório gerado: {Path(args.saida).resolve()}")
    print(f"Janela analisada: {formatar_datahora(janela_inicio)} até {formatar_datahora(janela_fim)}")
    print(f"Ciclos totais identificados na janela: {len(ciclos)}")

    for c in ciclos:
        estado = c['status']
        print(f"  Ciclo {c['numero']} ({estado}): Início da Lateral 1: {formatar_datahora(c['etapa_30']['inicio'])}")

    print("\nResumo da Soldagem:")
    for maquina, dados in sorted(resumo_soldagem.items()):
        corrente = "n/d" if dados["corrente_media_A"] is None else f'{numero_br(dados["corrente_media_A"])} A'
        print(f"  {maquina}: arco {formatar_hms(dados['tempo_arco_s'])}; corrente média {corrente}")


if __name__ == "__main__":
    main()

import os
import socket
import subprocess
import atexit
import time as time_module
from pathlib import Path


def carregar_env(caminho=None):
    """Carrega variáveis de um arquivo .env sem sobrescrever variáveis existentes."""
    arquivo_env = Path(caminho) if caminho else Path(__file__).resolve().with_name(".env")
    if not arquivo_env.is_file():
        return

    for linha in arquivo_env.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.startswith("export "):
            linha = linha[7:].strip()
        if "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        valor = valor.strip()
        if not chave or chave in os.environ:
            continue
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("\"", "'"):
            valor = valor[1:-1]
        elif " #" in valor:
            valor = valor.split(" #", 1)[0].rstrip()
        os.environ[chave] = valor


def porta_local_aberta(host, porta, timeout=0.5):
    """Retorna True se já existe um serviço escutando na porta local."""
    try:
        with socket.create_connection((host, int(porta)), timeout=timeout):
            return True
    except OSError:
        return False


def abrir_tunel_ssh(chave, bastion, usuario_privado, host_privado, porta_local, porta_remota, espera=20):
    """Abre um túnel SSH pelo bastion e devolve o processo criado."""
    chave = str(Path(chave).expanduser())
    if not Path(chave).is_file():
        raise FileNotFoundError(
            f"Chave SSH não encontrada: {chave}. Ajuste SSH_KEY_PATH no .env."
        )

    if porta_local_aberta("127.0.0.1", porta_local):
        print(f"Já existe um serviço acessível em 127.0.0.1:{porta_local}; usando a conexão existente.")
        return None

    proxy = (
        f"ssh -i {chave} -o IdentitiesOnly=yes "
        f"-o StrictHostKeyChecking=accept-new -W %h:%p {bastion}"
    )
    comando = [
        "ssh", "-i", chave,
        "-o", "IdentitiesOnly=yes",
        "-o", f"ProxyCommand={proxy}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N", "-L", f"127.0.0.1:{porta_local}:127.0.0.1:{porta_remota}",
        f"{usuario_privado}@{host_privado}",
    ]
    print(f"Abrindo túnel SSH: 127.0.0.1:{porta_local} → {host_privado}:127.0.0.1:{porta_remota} via {bastion}...")
    try:
        processo = subprocess.Popen(
            comando, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Não encontrei o executável ssh. Instale/ative o cliente OpenSSH do Windows "
            "ou abra o PowerShell onde o comando ssh funciona."
        ) from exc

    limite = time_module.monotonic() + espera
    while time_module.monotonic() < limite:
        if processo.poll() is not None:
            raise RuntimeError(
                "O processo SSH encerrou antes de abrir o túnel. Confira a chave, o bastion, "
                "a conectividade e se a primeira conexão já teve a chave do host confirmada."
            )
        if porta_local_aberta("127.0.0.1", porta_local):
            print("Túnel SSH estabelecido.")
            atexit.register(_encerrar_tunel, processo)
            return processo
        time_module.sleep(0.25)

    _encerrar_tunel(processo)
    raise TimeoutError(
        f"O túnel SSH não abriu a porta local {porta_local} em {espera} segundos. "
        "Confira se a chave do host do bastion/EC2 já foi aceita numa conexão SSH manual."
    )


def _encerrar_tunel(processo):
    """Encerra apenas o processo SSH iniciado por este script."""
    if processo is not None and processo.poll() is None:
        processo.terminate()
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            processo.kill()
            processo.wait(timeout=5)


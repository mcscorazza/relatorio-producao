import os
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, send_file, after_this_request
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SCRIPT = BASE_DIR / "main.py"

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

INFLUX_URL = os.getenv("INFLUX_URL", "http://10.20.0.239:8181")
INFLUX_DB = os.getenv("INFLUX_DB", "manufatura")


@app.route("/", methods=["GET", "POST"])
def index():
    erro = None

    hoje = datetime.now().strftime("%Y-%m-%d")
    valores = {
        "data_inicio": hoje,
        "data_fim": hoje,
        "inicio": "06:30",
        "fim": "11:00",
    }

    if request.method == "POST":
        valores = {
            "data_inicio": request.form.get("data_inicio", "").strip(),
            "data_fim": request.form.get("data_fim", "").strip(),
            "inicio": request.form.get("inicio", "").strip(),
            "fim": request.form.get("fim", "").strip(),
        }

        try:
            data_inicio = datetime.strptime(valores["data_inicio"], "%Y-%m-%d")
            data_fim = datetime.strptime(valores["data_fim"], "%Y-%m-%d")
            inicio = datetime.strptime(valores["inicio"], "%H:%M")
            fim = datetime.strptime(valores["fim"], "%H:%M")

            if data_fim.date() < data_inicio.date():
                raise ValueError("A data final deve ser igual ou posterior à data inicial.")
            if fim <= inicio:
                raise ValueError("O horário final deve ser posterior ao inicial.")

            if not SCRIPT.is_file():
                raise RuntimeError("O script de relatório não foi encontrado.")

        except ValueError as exc:
            erro = str(exc) or "Data ou horário inválido."

        except RuntimeError as exc:
            erro = str(exc)

        if erro is None:
            pasta_temp = tempfile.mkdtemp(prefix="relatorio-producao-")
            arquivo_saida = Path(pasta_temp) / "relatorio_producao.html"

            comando = [
                sys.executable,
                str(SCRIPT),
                "--data-inicio", valores["data_inicio"],
                "--data-fim", valores["data_fim"],
                "--inicio", valores["inicio"],
                "--fim", valores["fim"],
                "--saida", str(arquivo_saida),
                "--api-url", INFLUX_URL,
                "--db", INFLUX_DB,
                "--no-ssh-tunnel",
            ]

            try:
                resultado = subprocess.run(
                    comando,
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )

                if resultado.returncode != 0 or not arquivo_saida.is_file():
                    app.logger.error(
                        "Falha no relatório. stdout=%s stderr=%s",
                        resultado.stdout[-4000:],
                        resultado.stderr[-4000:],
                    )
                    erro = (
                        "Não foi possível gerar o relatório. "
                        "Verifique os logs da aplicação."
                    )
                    shutil.rmtree(pasta_temp, ignore_errors=True)
                else:
                    @after_this_request
                    def remover_arquivo(response):
                        shutil.rmtree(pasta_temp, ignore_errors=True)
                        return response

                    return send_file(
                        arquivo_saida,
                        mimetype="text/html",
                        as_attachment=False,
                        download_name="relatorio_producao.html",
                    )

            except subprocess.TimeoutExpired:
                shutil.rmtree(pasta_temp, ignore_errors=True)
                erro = "A geração excedeu o limite de 180 segundos."
            except Exception:
                app.logger.exception("Erro inesperado ao gerar relatório.")
                shutil.rmtree(pasta_temp, ignore_errors=True)
                erro = "Ocorreu um erro interno ao gerar o relatório."

    return render_template("index.html", valores=valores, erro=erro)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
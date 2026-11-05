from pathlib import Path
import html
import base64

from turnos import horario_turnos
from utils import formatar_hms, formatar_datahora, numero_br
from collections import Counter
from datetime import date, datetime, timedelta
from config import TZ_LOCAL

def imagem_base64(caminho, mime_type="image/png"):
    caminho = Path(caminho)

    with caminho.open("rb") as arquivo:
        conteudo = base64.b64encode(arquivo.read()).decode("ascii")

    return f"data:{mime_type};base64,{conteudo}"

def obter_dia_produtivo(data_hora):
    return (data_hora - timedelta(hours=2)).date()

def html_tabela_ciclos(ciclos):
    if not ciclos:
        return '<tr><td colspan="6" style="text-align:center; padding: 20px;">Nenhum ciclo reconhecido no período.</td></tr>'

    linhas = []

    for c in ciclos:
        status_css = "status-complete" if c["status"] == "Completo" else "status-pending"
        dur_str = formatar_hms(c["duracao_ciclo"])

        # Linha Principal do Ciclo
        linhas.append(
            f"<tr class='beam-row' tabindex='0' role='button' aria-expanded='false' title='Clique para ver as etapas'>"
            f"<td><strong>{c['numero']}</strong></td>"
            f"<td class='{status_css}'>{c['status']}</td>"
            f"<td>{formatar_datahora(c['inicio_ciclo'])}</td>"
            f"<td>{formatar_datahora(c['fim_ciclo'])}</td>"
            f"<td><strong>{dur_str}</strong></td>"
            f"<td>{c['sequencia']}</td>"
            f"</tr>"
        )

        # Construção da Sub-tabela de Etapas
        etapas_nomes = [
            (10, "Posicionamento (Início)", c["etapa_10"]),
            (20, "Solda Plana da Emenda", c["etapa_20"]),
            (30, "Solda Vertical 1 + Nervuras", c["etapa_30"]),
            (40, "Solda Vertical 2 + Nervuras", c["etapa_40"]),
            (50, "Limpeza", c["etapa_50"]),
            (60, "Finalização (Viga pronta)", c["etapa_60"])
        ]

        detalhes = ""
        for cod, nome, info in etapas_nomes:
            if info:
                ang = f"{info['angulo']}" if info['angulo'] == "—" else f"{info['angulo']}°"
                dt_ini = info['inicio'].astimezone(TZ_LOCAL).strftime("%H:%M:%S") if info['inicio'] else "—"
                dt_fim = info['fim'].astimezone(TZ_LOCAL).strftime("%H:%M:%S") if info['fim'] else "—"
                dur = formatar_hms(info['duracao'])

                # Regras de Status
                if cod == 30 and info['angulo'] == 270:
                    status = "Finalizada; condição 0° → 180°"
                elif cod == 30 and info['angulo'] == 90:
                    status = "Finalizada; condição 180° → 0°"
                elif cod == 40:
                    status = "Finalizada pela saída para 0°/180°" if info['fim'] else "Em andamento"
                else:
                    status = info['status']
                estilo = ""
            else:
                ang, dt_ini, dt_fim, dur = "—", "—", "—", "—"
                status = "Opcional (pulada)" if cod == 50 else "Indeterminada (falta de evidência)"
                estilo = "style='color:#9ca3af;'" if cod == 50 else ""

            detalhes += f"<tr {estilo}><td><strong>{cod}</strong></td><td>{nome}</td><td>{dt_ini}</td><td>{dt_fim}</td><td>{dur}</td><td>{ang}</td></tr>"

        linhas.append(
            f"<tr class='detail-row' hidden><td colspan='6'>"
            f"<div class='table-wrap stage-wrap'>"
            f"<table class='stage-table'>"
            f"<thead><tr><th>Etapa</th><th>Descrição</th><th>Início</th><th>Fim</th><th>Duração</th><th>Posição do girador</th></tr></thead>"
            f"<tbody>{detalhes}</tbody>"
            f"</table></div></td></tr>"
        )

    return "\n".join(linhas)

def html_producao_diaria(ciclos, inicio, fim):
    """Monta gráfico diário contando ciclos completos pela data de início da etapa 30."""
    por_dia = Counter()
    dia = inicio.date()
    ultimo_dia = (fim - timedelta(microseconds=1)).date()
    while dia <= ultimo_dia:
        por_dia[dia.isoformat()] += 0
        dia += timedelta(days=1)
    for ciclo in ciclos:
        if ciclo.get("status") != "Completo":
            continue
        etapa30 = ciclo.get("etapa_30") or {}
        dt = etapa30.get("inicio")
        if dt:
            dt_local = dt.astimezone(TZ_LOCAL)
            chave = obter_dia_produtivo(dt_local).isoformat()
            if chave in por_dia:
                por_dia[chave] += 1
    maximo = max(por_dia.values(), default=0)
    itens = []
    for data_iso, quantidade in por_dia.items():
        data_obj = date.fromisoformat(data_iso)
        altura = (quantidade / maximo * 100) if maximo else 0
        itens.append(
            f"<div class='day-column' title='{data_obj.strftime('%d/%m/%Y')}: {quantidade} ciclos completos'>"
            f"<div class='day-count'>{quantidade}</div>"
            f"<div class='day-chart-area'><div class='day-bar' style='height:{altura:.1f}%'></div></div>"
            f"<div class='day-label'>{data_obj.strftime('%d/%m')}<br><span>{['seg','ter','qua','qui','sex','sáb','dom'][data_obj.weekday()]}</span></div>"
            f"</div>"
        )
    return "".join(itens) or "<p class='small'>Sem dias no período selecionado.</p>"

def html_linha_tempo_producao(ciclos, dia_inicio, dia_fim, janela_inicio, janela_fim):
    """Timeline por dia produtivo, com dois turnos e ciclos recortados nos limites do turno."""
    nomes_dias = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']
    linhas = []
    dia = dia_inicio
    while dia <= dia_fim:
        turnos = horario_turnos(dia)
        if turnos:
            t1_ini, t2_ini, t2_fim_hora = turnos
            ini1 = datetime.combine(dia, t1_ini, tzinfo=TZ_LOCAL)
            fim1 = datetime.combine(dia, t2_ini, tzinfo=TZ_LOCAL)
            fim2 = datetime.combine(dia, t2_fim_hora, tzinfo=TZ_LOCAL)
            if dia.weekday() < 4:
                fim2 += timedelta(days=1)
            blocos = []
            for ini_turno, fim_turno, rotulo in [(ini1, fim1, '1º turno'), (fim1, fim2, '2º turno')]:
                duracao = (fim_turno - ini_turno).total_seconds()
                segmentos = []
                for ciclo in ciclos:
                    ini_ciclo = ciclo.get('inicio_ciclo')
                    if not ini_ciclo:
                        continue
                    fim_ciclo = ciclo.get('fim_ciclo') or janela_fim
                    ini = max(ini_ciclo, ini_turno, janela_inicio)
                    fim = min(fim_ciclo, fim_turno, janela_fim)
                    if fim <= ini:
                        continue
                    esquerda = (ini - ini_turno).total_seconds() / duracao * 100
                    largura = (fim - ini).total_seconds() / duracao * 100
                    cor = '#f59e0b' if ciclo.get('status') != 'Completo' or not ciclo.get('fim_ciclo') else '#16a34a'
                    titulo = f"Ciclo {ciclo.get('numero', '?')} · {ini.strftime('%H:%M')}–{fim.strftime('%H:%M')} · {ciclo.get('status', 'Em andamento')}"
                    segmentos.append(f"<div class='timeline-cycle' style='left:{esquerda:.4f}%;width:{largura:.4f}%;background:{cor}' title='{html.escape(titulo, quote=True)}'><span>{html.escape(str(ciclo.get('numero', '')))}</span></div>")
                blocos.append(f"<div class='timeline-shift'><div class='timeline-shift-head'><strong>{rotulo}</strong><span>{ini_turno.strftime('%H:%M')}–{fim_turno.strftime('%H:%M')}</span></div><div class='timeline-track'><div class='timeline-gridline' style='left:25%'></div><div class='timeline-gridline' style='left:50%'></div><div class='timeline-gridline' style='left:75%'></div>{''.join(segmentos)}</div></div>")
            linhas.append(f"<div class='timeline-day'><div class='timeline-date'><strong>{dia.strftime('%d/%m')}</strong><span>{nomes_dias[dia.weekday()]}</span></div><div class='timeline-shifts'>{''.join(blocos)}</div></div>")
        dia += timedelta(days=1)
    return ''.join(linhas) or "<p class='small'>Não há turnos produtivos definidos para o intervalo selecionado.</p>"

def gerar_html_novo(caminho_saida, data_str, inicio, fim, ciclos, resumo_soldagem, contagem_girador, tempos_girador, lacunas_girador):

    logo = imagem_base64(
        Path(__file__).resolve().parent / "gbmx.png"
    )
    conteudo = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relatório de Produção - Ciclos</title>
<style>
:root{{--ink:#172033;--muted:#667085;--line:#dfe5ee;--green:#166534;--bg:#f4f6fa}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 Arial,sans-serif}}
main{{max-width:1400px;margin:0 auto;padding:22px}}
header,section {{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:12px}}
header {{background:#166534;color:#ffffff}}
h1{{font-size:24px;margin:0 0 8px}}
.muted{{color:var(--muted);font-size:12px}}
.table-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:760px}}
th,td{{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}}
th{{background:#14532d;color:white}}
.beam-row{{background:#f0fdf4; border-top: 3px solid var(--line);cursor:pointer;transition:background .15s}}
.beam-row:hover,.beam-row:focus{{background:#dcfce7;outline:none}}
.beam-row td:first-child::before{{content:'＋';display:inline-block;width:20px;color:#166534;font-weight:700}}
.beam-row[aria-expanded='true'] td:first-child::before{{content:'−'}}
.status-complete{{color:#166534;font-weight:700}}
.status-pending{{color:#92400e;font-weight:700}}
.detail-row > td {{padding: 0; background: #fff; border-bottom: 1px solid var(--line);}}
.stage-wrap{{padding:4px 4px 4px 12px}}
.stage-table thead th {{background-color:#f6f6f6; color:black }}
.stage-table{{min-width:100%;font-size:11px;background:#fff;border:none}}
.stage-table td{{padding:4px 8px;border-bottom:1px solid #edf0f5}}
.stage-table tr:last-child td{{border-bottom:none}}
.stage-table td:first-child{{width:65px;color:#14532d;font-weight:700}}
.note{{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px;margin-top:14px; font-size:13px; color: #1e3a8a;}}
.cycle-section-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px;flex-wrap:wrap}}
.cycle-section-head h2{{margin:0;font-size:19px}}
.cycle-count{{display:flex;align-items:center;gap:12px;background:linear-gradient(135deg,#14532d,#15803d);color:#fff;border-radius:12px;padding:12px 18px;box-shadow:0 5px 14px rgba(20,83,45,.18)}}
.cycle-count .count-number{{font-size:30px;font-weight:800;line-height:1}}
.cycle-count .count-label{{font-size:12px;line-height:1.3;max-width:100px}}
.metric-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}}
.metric{{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:14px;padding:18px;background:linear-gradient(145deg,#fff,#f8fafc);box-shadow:0 3px 10px rgba(23,32,51,.05)}}
.metric:before{{content:'';position:absolute;left:0;top:0;bottom:0;width:5px;background:#16a34a}}
.metric h3{{margin:0 0 12px;font-size:15px;color:#475467}}
.metric .metric-value{{font-size:30px;line-height:1.1;font-weight:800;letter-spacing:-.5px;color:#14532d;margin-bottom:8px}}
.metric .metric-caption{{font-size:12px;color:var(--muted)}}
.metric .metric-detail{{margin:12px 0 0;padding-top:10px;border-top:1px solid #e9edf3;font-size:13px}}
.metric .metric-foot{{margin-top:8px;font-size:11px;color:var(--muted)}}
.small{{font-size:12px;color:var(--muted)}}
.position-bar{{height:9px;min-width:100px;background:#e9eef5;border-radius:99px;overflow:hidden;margin-top:5px}}
.position-bar-fill{{height:100%;background:linear-gradient(90deg,#16a34a,#86efac);border-radius:99px}}
.position-percent{{min-width:70px;font-weight:700;color:#344054}}
.daily-chart{{display:grid;grid-template-columns:repeat(auto-fit,minmax(64px,1fr));gap:12px;align-items:end;min-height:210px;padding:12px 4px 0}}
.day-column{{display:flex;flex-direction:column;align-items:center;min-width:0}}
.day-count{{font-size:15px;font-weight:800;color:#14532d;margin-bottom:6px}}
.day-chart-area{{height:130px;width:100%;display:flex;align-items:flex-end;justify-content:center;background:linear-gradient(to top,#f1f5f9 1px,transparent 1px);background-size:100% 25%;border-bottom:1px solid var(--line)}}
.day-bar{{width:min(42px,70%);min-height:3px;background:linear-gradient(180deg,#4ade80,#15803d);border-radius:7px 7px 0 0;transition:height .2s}}
.day-label{{text-align:center;font-size:12px;font-weight:700;margin-top:8px}}
.day-label span{{color:var(--muted);font-weight:400}}

.timeline-legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 16px;font-size:12px;color:var(--muted)}}
.timeline-key{{display:inline-flex;align-items:center;gap:6px}}.timeline-swatch{{width:12px;height:12px;border-radius:3px;display:inline-block}}
.timeline-day{{display:grid;grid-template-columns:64px minmax(0,1fr);gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}}
.timeline-date{{display:flex;flex-direction:column;align-items:flex-start;justify-content:center}}.timeline-date span{{font-size:12px;color:var(--muted)}}
.timeline-shifts{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;min-width:0}}
.timeline-shift{{min-width:0}}.timeline-shift-head{{display:flex;justify-content:space-between;gap:6px;font-size:11px;color:#475467;margin-bottom:5px}}
.timeline-track{{height:28px;position:relative;background:#e5e7eb;border-radius:5px;overflow:hidden;background-image:linear-gradient(to right,transparent calc(25% - 1px),#cbd5e1 25%,transparent calc(25% + 1px),transparent calc(50% - 1px),#cbd5e1 50%,transparent calc(50% + 1px),transparent calc(75% - 1px),#cbd5e1 75%,transparent calc(75% + 1px))}}
.timeline-cycle{{position:absolute;top:3px;bottom:3px;border-radius:3px;min-width:2px;overflow:hidden;color:white;font-size:10px;font-weight:700;text-align:center;line-height:22px;opacity:.96}}.timeline-cycle span{{padding:0 2px}}
.timeline-note{{font-size:12px;color:var(--muted);margin-top:10px}}

.topo {{ width: 100%; display: flex; flex-direction: column; }}
.topo .logo {{ align-self: flex-end; width: 120px; max-height: 40px; object-fit: contain; margin-bottom:12px }}
.topo header {{ width: 100%; box-sizing: border-box; }}

@media(max-width:760px){{.timeline-day{{grid-template-columns:48px minmax(0,1fr);gap:7px}}.timeline-shifts{{grid-template-columns:1fr}}.timeline-track{{height:24px}}.timeline-cycle{{line-height:18px}}}}

</style>
</head>
<body>
<main>
<div class="topo">
    <img src="{logo}" alt="Logo" class="logo">
    <header>
        <h1>Girador da Viga Central HTT</h1>
        <div>
            Período: <strong>{formatar_datahora(inicio)} até {formatar_datahora(fim)}</strong>
        </div>
    </header>
</div>
<section>
  <h2>Linha do tempo da produção</h2>
  <div class="timeline-legend"><span class="timeline-key"><i class="timeline-swatch" style="background:#16a34a"></i>Ciclo completo</span><span class="timeline-key"><i class="timeline-swatch" style="background:#f59e0b"></i>Ciclo incompleto / cortado pela janela</span><span class="timeline-key"><i class="timeline-swatch" style="background:#e5e7eb;border:1px solid #cbd5e1"></i>Intervalo sem ciclo detectado</span></div>
  <div class="timeline-chart">{html_linha_tempo_producao(ciclos, inicio.date(), (fim - timedelta(hours=6)).date(), inicio, fim)}</div>
  <p class="timeline-note">Turnos de segunda a quinta: 06:30–16:20 e 16:20–01:40 do dia seguinte. Sexta-feira: 06:30–14:20 e 14:20–21:40. Feriados (feriados.txt) e finais de semana não têm turno.</p>
</section>
<section>
  <div class="cycle-section-head">
    <h2>Ciclos identificados</h2>
    <div class="cycle-count"><span class="count-number">{sum(1 for c in ciclos if c['status'] == 'Completo')}</span><span class="count-label">ciclos completos</span></div>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Nº</th>
          <th>Status do ciclo</th>
          <th>Início detectado</th>
          <th>Fim detectado</th>
          <th>Duração observada</th>
          <th>Sequência do girador</th>
        </tr>
      </thead>
      <tbody>
        {html_tabela_ciclos(ciclos)}
      </tbody>
    </table>
  </div>
</section>
<section>
  <h2>Produção diária</h2>
  <p class="small">Quantidade de ciclos completos por dia, agrupados pela data de início da etapa 30.</p>
  <div class="daily-chart">{html_producao_diaria(ciclos, inicio, fim)}</div>
</section>
<section>
  <h2>Tempo de arco aberto por máquina</h2>
  <div class="metric-grid">
    {''.join(f"<div class='metric'><h3>{html.escape(maquina)}</h3><div class='metric-value'>{formatar_hms(dados['tempo_arco_s'])}</div><div class='metric-caption'>Tempo total de arco aberto</div><div class='metric-detail'>Corrente média durante o arco: <strong>{('n/d' if dados['corrente_media_A'] is None else numero_br(dados['corrente_media_A']) + ' A')}</strong></div><div class='metric-foot'>Registros: {dados['registros']} · Intervalos positivos: {dados['intervalos_positivos']}</div></div>" for maquina, dados in sorted(resumo_soldagem.items())) or "<div class='metric'>Nenhum dado de soldagem no período.</div>"}
  </div>
</section>
<section>
  <h2>Posições do girador</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Posição</th><th>Leituras</th><th>Percentual das leituras</th></tr></thead>
      <tbody>
        {''.join(f"<tr><td><strong>{angulo}°</strong></td><td>{quantidade}</td><td><div class='position-percent'>{numero_br(100*quantidade/sum(contagem_girador.values()),1) if sum(contagem_girador.values()) else '0,0'}%</div><div class='position-bar'><div class='position-bar-fill' style='width:{100*quantidade/sum(contagem_girador.values()) if sum(contagem_girador.values()) else 0:.1f}%'></div></div></td></tr>" for angulo, quantidade in sorted(contagem_girador.items())) or "<tr><td colspan='3'>Nenhuma leitura do girador no período.</td></tr>"}
      </tbody>
    </table>
  </div>
</section>
</main>
<script>
document.querySelectorAll('.beam-row').forEach(function(row) {{
  function toggleDetails() {{
    const detail = row.nextElementSibling;
    if (!detail || !detail.classList.contains('detail-row')) return;
    const opening = detail.hidden;
    detail.hidden = !opening;
    row.setAttribute('aria-expanded', String(opening));
  }}
  row.addEventListener('click', toggleDetails);
  row.addEventListener('keydown', function(event) {{
    if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); toggleDetails(); }}
  }});
}});
</script>
</body>
</html>"""
    Path(caminho_saida).write_text(conteudo, encoding="utf-8")


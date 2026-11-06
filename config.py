from datetime import timezone
from zoneinfo import ZoneInfo

TZ_LOCAL = ZoneInfo("America/Sao_Paulo")
TZ_UTC = timezone.utc

# Ciclo que dura mais que isso (já terminado ou ainda "em andamento") é descartado:
# na prática indica falha do sensor do girador, não uma viga real.
DURACAO_MAXIMA_CICLO_S = 10 * 3600
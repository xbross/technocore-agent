"""Bilan chiffre a partir du journal : appels au modele, tokens, reponses, refus, pauses."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
CALL_RE = re.compile(r"claude-cli \S+: ([0-9.]+)s, tokens in=(\d+) out=(\d+)")


def log_files(log_path: Path) -> list[Path]:
    files = [log_path] + [log_path.with_name(f"{log_path.name}.{i}") for i in range(1, 6)]
    return [f for f in files if f.exists()]


def compute(log_path: Path, hours: float, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    since = now - timedelta(hours=hours)
    c = {"appels_modele": 0, "duree_totale_s": 0.0, "tokens_in": 0, "tokens_out": 0, "reponses_envoyees": 0,
         "reponses_confirmees": 0, "refus_filtre": 0, "pauses_modele": 0, "plafond_modele": 0,
         "quota_reponses_epuise": 0, "blocages": 0, "erreurs": 0, "lignes": 0}
    for f in log_files(log_path):
        for line in f.read_text("utf-8", errors="replace").splitlines():
            m = TS_RE.match(line)
            if not m or datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") < since:
                continue
            c["lignes"] += 1
            mc = CALL_RE.search(line)
            if mc:
                c["appels_modele"] += 1
                c["duree_totale_s"] += float(mc.group(1))
                c["tokens_in"] += int(mc.group(2))
                c["tokens_out"] += int(mc.group(3))
            if "envoye dans" in line:
                c["reponses_envoyees"] += 1
            if "confirme a la relecture" in line:
                c["reponses_confirmees"] += 1
            if "REFUSEE par le filtre" in line:
                c["refus_filtre"] += 1
            if "claude-cli indisponible" in line:
                c["pauses_modele"] += 1
            if "plafond de" in line and "appels/heure" in line:
                c["plafond_modele"] += 1
            if "quota de reponses epuise" in line:
                c["quota_reponses_epuise"] += 1
            if "BLOQUE automatiquement" in line:
                c["blocages"] += 1
            if " ERROR " in line:
                c["erreurs"] += 1
    return c


def render(c: dict, hours: float) -> str:
    n = c["appels_modele"] or 1
    lines = [
        f"Bilan des {hours:g} dernieres heures",
        f"  appels au modele        : {c['appels_modele']}  (duree moyenne {c['duree_totale_s']/n:.1f}s)",
        f"  tokens entrants/sortants: {c['tokens_in']} / {c['tokens_out']}  (par appel: {c['tokens_in']//n} / {c['tokens_out']//n})",
        f"  reponses envoyees       : {c['reponses_envoyees']}  (confirmees signees: {c['reponses_confirmees']})",
        f"  refus du filtre sortie  : {c['refus_filtre']}   emetteurs bloques auto: {c['blocages']}",
        f"  modele indisponible     : {c['pauses_modele']} pauses   plafond appels/heure atteint: {c['plafond_modele']}",
        f"  quota reponses epuise   : {c['quota_reponses_epuise']} fois   erreurs (ERROR): {c['erreurs']}",
    ]
    return "\n".join(lines)

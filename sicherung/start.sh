#!/bin/sh
# Sicherungsordner (oft ein Ordner auf dem Server oder NAS) für Benutzer 1000 beschreibbar machen,
# dann ohne Root-Rechte weiterlaufen.
set -e
mkdir -p "${ZIEL:-/sicherungen}"
chown 1000:1000 "${ZIEL:-/sicherungen}" 2>/dev/null || echo "Hinweis: Besitzer von ${ZIEL:-/sicherungen} konnte nicht gesetzt werden (z. B. NAS-Freigabe) – Schreibrechte für UID 1000 prüfen."
exec setpriv --reuid=1000 --regid=1000 --clear-groups python /app/sicherung.py "$@"

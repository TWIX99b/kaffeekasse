FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin

WORKDIR /app
COPY sicherung.py start.sh ./
# „sicherung jetzt“, „sicherung liste“ … als Befehl im Container
RUN printf '#!/bin/sh\nexec setpriv --reuid=1000 --regid=1000 --clear-groups python /app/sicherung.py "$@"\n' > /usr/local/bin/sicherung \
 && chmod +x /usr/local/bin/sicherung /app/start.sh \
 && useradd --uid 1000 --no-create-home kaffeekasse

# Startet als root, gibt dem Sicherungsordner den richtigen Besitzer und läuft dann als Benutzer 1000
# (derselbe wie die Kaffeekasse – so passen die Dateirechte auf dem Daten-Volume).
ENTRYPOINT ["/app/start.sh"]
CMD ["dienst"]

"""Kaffeekasse – Sicherungs-Dienst (zweiter Container).

Macht zu festen Uhrzeiten und auf Zuruf eine vollständige Kopie der Datenbank (inklusive Produktfotos),
prüft sie und legt sie gepackt in einem eigenen Ordner ab – getrennt vom Daten-Volume der Kaffeekasse,
z. B. auf einem NAS. Eine Sicherung lässt sich mit einem Befehl wieder einspielen.

Die Kopie entsteht mit der Sicherungsfunktion von SQLite selbst: Sie ist eine echte Momentaufnahme,
auch wenn gerade eingekauft wird, und die Kaffeekasse muss dafür nicht angehalten werden.

Befehle (im Ordner mit der docker-compose.yml):
  docker compose exec sicherung sicherung jetzt [Notiz]     Sicherung sofort erstellen
  docker compose exec sicherung sicherung liste             alle Sicherungen anzeigen
  docker compose exec sicherung sicherung pruefen [Datei]   Sicherung prüfen (ohne Datei: die neueste)
  docker compose exec sicherung sicherung einspielen Datei  Sicherung zurückspielen (fragt nach)
"""
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "/data/kaffeekasse.db")
ZIEL = os.environ.get("ZIEL", "/sicherungen")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
UHRZEITEN = os.environ.get("SICHERUNG_UHR", "02:30")          # mehrere mit Komma: "02:30,12:00"
ANZAHL = int(os.environ.get("SICHERUNG_ANZAHL", "30") or 30)  # so viele automatische Sicherungen bleiben liegen
MELDE_URL = os.environ.get("MELDE_URL", "").strip()          # optional: ntfy/Gotify-Adresse für Fehlermeldungen
NAME_RE = re.compile(r"^kaffeekasse_(\d{4}-\d{2}-\d{2}_\d{6})_(auto|manuell|vor-einspielen)\.db\.gz$")


def jetzt_txt():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")


def sag(text):
    print(f"[{jetzt_txt()}] {text}", flush=True)


def melden(titel, text):
    """Nur bei Fehlern: kurze Meldung an ntfy/Gotify (wenn MELDE_URL gesetzt ist)."""
    if not MELDE_URL:
        return
    try:
        if MELDE_URL.rstrip("/").split("?")[0].endswith("/message"):          # Gotify
            daten = json.dumps({"title": titel, "message": text, "priority": 7}).encode()
            req = urllib.request.Request(MELDE_URL, daten, {"Content-Type": "application/json"})
        else:                                                                 # ntfy & Co.
            req = urllib.request.Request(MELDE_URL, text.encode(), {"Title": titel.encode("ascii", "replace").decode()})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as ex:   # noqa: BLE001 – die Meldung darf die Sicherung nie stören
        sag(f"Meldung konnte nicht gesendet werden: {ex}")


def ordner_pruefen():
    os.makedirs(ZIEL, exist_ok=True)
    if not os.access(ZIEL, os.W_OK):
        raise SystemExit(f"Der Sicherungsordner {ZIEL} ist nicht beschreibbar. "
                         f"Auf dem Server: sudo chown -R 1000:1000 <Ordner>")
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Keine Datenbank unter {DB_PATH} – ist das Volume der Kaffeekasse eingebunden?")


def kennzahlen(pfad):
    """Ein paar Zahlen zur Kontrolle: Stimmt die Sicherung grob mit dem überein, was man erwartet?"""
    c = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        z = lambda q: c.execute(q).fetchone()[0]   # noqa: E731
        return {"ok": ok == "ok", "pruefung": ok,
                "konten": z("SELECT COUNT(*) FROM konten WHERE entfernt=0"),
                "verkaeufe": z("SELECT COUNT(*) FROM verkaeufe"),
                "kasse": z("SELECT COALESCE(SUM(betrag),0) FROM kasse") / 100,
                "guthaben": z("SELECT COALESCE(SUM(guthaben),0) FROM konten WHERE entfernt=0") / 100,
                "letzte_buchung": z("SELECT MAX(zeit) FROM journal")}
    finally:
        c.close()


def kennzahlen_txt(k):
    letzte = datetime.fromtimestamp(k["letzte_buchung"] / 1000, TZ).strftime("%d.%m.%Y %H:%M") if k["letzte_buchung"] else "–"
    eur = lambda x: f"{x:.2f}".replace(".", ",") + " €"   # noqa: E731
    return (f"{k['konten']} Konten · {k['verkaeufe']} Verkäufe · Kasse {eur(k['kasse'])} · "
            f"Guthaben gesamt {eur(k['guthaben'])} · letzte Änderung {letzte}")


def sichern(art, notiz=""):
    """Momentaufnahme mit der SQLite-Sicherungsfunktion → prüfen → packen → ablegen."""
    ordner_pruefen()
    stempel = datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S")
    name = f"kaffeekasse_{stempel}_{art}.db.gz"
    with tempfile.TemporaryDirectory(dir=ZIEL) as tmp:
        roh = os.path.join(tmp, "kopie.db")
        quelle = sqlite3.connect(DB_PATH, timeout=60)
        ziel = sqlite3.connect(roh)
        try:
            quelle.backup(ziel)          # konsistente Kopie, Einkäufe laufen währenddessen weiter
        finally:
            ziel.close()
            quelle.close()
        k = kennzahlen(roh)
        if not k["ok"]:
            raise RuntimeError(f"Die Kopie ist beschädigt: {k['pruefung']}")
        teil = os.path.join(ZIEL, name + ".teil")
        with open(roh, "rb") as ein, gzip.open(teil, "wb", compresslevel=6) as aus:
            shutil.copyfileobj(ein, aus, 1024 * 1024)
        os.replace(teil, os.path.join(ZIEL, name))           # erst fertig, dann sichtbar
    if notiz:
        with open(os.path.join(ZIEL, name[:-6] + ".txt"), "w", encoding="utf-8") as f:
            f.write(notiz.strip() + "\n")
    groesse = os.path.getsize(os.path.join(ZIEL, name)) / 1e6
    sag(f"Sicherung erstellt: {name} ({groesse:.1f} MB) – {kennzahlen_txt(k)}")
    aufraeumen()
    return name


def dateien():
    out = []
    for n in sorted(os.listdir(ZIEL), reverse=True) if os.path.isdir(ZIEL) else []:
        m = NAME_RE.match(n)
        if m:
            out.append((n, m.group(2)))
    return out


def aufraeumen():
    """Automatische Sicherungen: die neuesten ANZAHL behalten. Manuelle und „vor-einspielen“ bleiben liegen
    (bis zu 50 – wer mehr will, räumt von Hand auf)."""
    for art, behalten in (("auto", ANZAHL), ("manuell", 50), ("vor-einspielen", 50)):
        alt = [n for n, a in dateien() if a == art][max(1, behalten):]
        for n in alt:
            for pfad in (os.path.join(ZIEL, n), os.path.join(ZIEL, n[:-6] + ".txt")):
                try:
                    os.remove(pfad)
                except FileNotFoundError:
                    pass
            sag(f"Alte Sicherung entfernt: {n}")


def finde(name):
    liste = dateien()
    if not liste:
        raise SystemExit("Es gibt noch keine Sicherung.")
    if not name or name == "neueste":
        return liste[0][0]
    name = os.path.basename(name)
    if not NAME_RE.match(name) or not os.path.isfile(os.path.join(ZIEL, name)):
        raise SystemExit(f"Sicherung „{name}“ nicht gefunden. Mit „sicherung liste“ anzeigen lassen.")
    return name


def entpacken(name, ziel_ordner):
    roh = os.path.join(ziel_ordner, "entpackt.db")
    try:
        with gzip.open(os.path.join(ZIEL, name), "rb") as ein, open(roh, "wb") as aus:
            shutil.copyfileobj(ein, aus, 1024 * 1024)
        kennzahlen(roh)                       # lässt sich die Datei überhaupt als Datenbank öffnen?
    except (OSError, EOFError, sqlite3.DatabaseError, Exception) as ex:   # noqa: BLE001 – zlib.error u. a.
        raise SystemExit(f"{name} ist beschädigt und lässt sich nicht verwenden ({ex}).")
    return roh


def cmd_liste(_args):
    liste = dateien()
    if not liste:
        print("Noch keine Sicherungen.")
        return
    for n, art in liste:
        notiz = ""
        txt = os.path.join(ZIEL, n[:-6] + ".txt")
        if os.path.exists(txt):
            notiz = " · " + open(txt, encoding="utf-8").read().strip()
        print(f"{n}  {os.path.getsize(os.path.join(ZIEL, n)) / 1e6:6.1f} MB{notiz}")


def cmd_pruefen(args):
    name = finde(args[0] if args else None)
    with tempfile.TemporaryDirectory(dir=ZIEL) as tmp:
        k = kennzahlen(entpacken(name, tmp))
    print(f"{name}: {'in Ordnung' if k['ok'] else 'BESCHÄDIGT – ' + k['pruefung']}")
    print("  " + kennzahlen_txt(k))
    if not k["ok"]:
        sys.exit(1)


def cmd_einspielen(args):
    ja = "--ja" in args
    args = [a for a in args if a != "--ja"]
    if not args:
        raise SystemExit("Bitte angeben, welche Sicherung: sicherung einspielen <Datei>  (oder „neueste“)")
    name = finde(args[0])
    ordner_pruefen()
    with tempfile.TemporaryDirectory(dir=ZIEL) as tmp:
        roh = entpacken(name, tmp)
        k = kennzahlen(roh)
        if not k["ok"]:
            raise SystemExit(f"Die Sicherung ist beschädigt und wird nicht eingespielt: {k['pruefung']}")
        print(f"Einspielen: {name}\n  {kennzahlen_txt(k)}")
        print(f"  Jetzt:     {kennzahlen_txt(kennzahlen(DB_PATH))}")
        print("Alles, was seit dieser Sicherung gebucht wurde, ist danach weg.\n"
              "Vorher wird der aktuelle Stand automatisch als „vor-einspielen“ gesichert.")
        if not ja:
            if not sys.stdin.isatty():
                raise SystemExit("Zur Bestätigung bitte mit --ja aufrufen (oder mit „docker compose exec -it“).")
            if input("Wirklich einspielen? Zum Bestätigen EINSPIELEN tippen: ").strip() != "EINSPIELEN":
                raise SystemExit("Abgebrochen – nichts geändert.")
        vorher = sichern("vor-einspielen", f"automatisch vor dem Einspielen von {name}")
        quelle = sqlite3.connect(roh)
        ziel = sqlite3.connect(DB_PATH, timeout=60)
        try:
            # Die SQLite-Sicherungsfunktion überschreibt die laufende Datenbank in EINEM Schritt.
            # Die Kaffeekasse öffnet für jede Anfrage eine neue Verbindung und sieht danach den eingespielten Stand.
            quelle.backup(ziel)
            ziel.execute("DELETE FROM sitzungen")   # alle Anmeldungen beenden: jeder meldet sich neu an
            ziel.commit()
        finally:
            ziel.close()
            quelle.close()
    sag(f"Eingespielt: {name}. Der vorherige Stand liegt in {vorher}. Alle Geräte müssen sich neu anmelden.")


def naechster_termin():
    jetzt = datetime.now(TZ)
    termine = []
    for teil in UHRZEITEN.split(","):
        try:
            h, m = (int(x) for x in teil.strip().split(":")[:2])
            t = jetzt.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            sag(f"Uhrzeit „{teil.strip()}“ in SICHERUNG_UHR ist ungültig und wird übersprungen (Format HH:MM).")
            continue
        termine.append(t if t > jetzt else t + timedelta(days=1))
    return min(termine) if termine else None


def cmd_dienst(_args):
    ordner_pruefen()
    sag(f"Sicherungs-Dienst läuft · Uhrzeiten {UHRZEITEN} · behalten: {ANZAHL} automatische · Ziel {ZIEL}")
    while True:
        t = naechster_termin()
        if not t:
            sag("Keine gültige Uhrzeit in SICHERUNG_UHR – automatische Sicherung aus. Manuell geht weiter.")
            time.sleep(86400)
            continue
        sag(f"Nächste Sicherung: {t:%d.%m.%Y %H:%M}")
        while datetime.now(TZ) < t:
            time.sleep(min(60, max(1, (t - datetime.now(TZ)).total_seconds())))
        try:
            sichern("auto")
        except Exception as ex:   # noqa: BLE001
            sag(f"FEHLER bei der Sicherung: {ex}")
            melden("Kaffeekasse: Sicherung fehlgeschlagen", str(ex))
            time.sleep(300)


BEFEHLE = {"dienst": cmd_dienst, "jetzt": lambda a: sichern("manuell", " ".join(a)), "liste": cmd_liste,
           "pruefen": cmd_pruefen, "einspielen": cmd_einspielen}

if __name__ == "__main__":
    befehl, rest = (sys.argv[1] if len(sys.argv) > 1 else "dienst"), sys.argv[2:]
    if befehl not in BEFEHLE:
        print(__doc__)
        sys.exit(2)
    BEFEHLE[befehl](rest)

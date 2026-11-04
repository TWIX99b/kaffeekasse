"""Kaffeekasse – Server für Shop und Verwaltung.

Login mit 8-stelliger Personalnummer. Kunden melden sich mit einer PIN (4–8 Ziffern) an,
Shopverantwortliche und Administratoren mit einem Passwort. Konten legt nur das Team an – keine Selbstregistrierung.
Rollen: kunde, personal, inhaber.
Alle Daten liegen in einer SQLite-Datenbank unter DATA_DIR (Standard /data).
"""
import base64
import hmac
import csv
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

# ------------------------------------------------------------------ Konfiguration
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "kaffeekasse.db")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
DEMO = os.environ.get("DEMO", "false").lower() in ("1", "true", "yes", "ja")
NUM_RE = re.compile(r"^\d{8}$")
MAX_FEHLER = 5
SPERRE_MS = 15 * 60_000
MIN_PW = {"kunde": 4, "personal": 10, "inhaber": 10}
PIN_RE = re.compile(r"^\d{4,8}$")
ROLLEN = ("kunde", "personal", "inhaber")
ROLLEN_NAME = {"kunde": "Mitglied", "personal": "Shopverantwortliche(r)", "inhaber": "Administrator"}
MAX_BILD = 800 * 1024
MAX_BUCHUNG = int(float(os.environ.get("MAX_BUCHUNG_EUR", "500")) * 100)   # größte Aufladung/Abbuchung
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

os.makedirs(DATA_DIR, exist_ok=True)


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # sofort nur für den Besitzer lesbar
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=_secret_key(),
    # Eigener Cookie-Name: Andere Apps auf demselben Server (anderer Port) nutzen oft "session" –
    # Browser trennen Cookies nicht nach Port, dann würden sich die Sitzungen gegenseitig stören.
    SESSION_COOKIE_NAME=os.environ.get("COOKIE_NAME", "kaffeekasse_sitzung"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,  # Sicherungen mit Fotos
)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").lower()
# Anzahl vertrauenswürdiger Reverse Proxys davor (0 = keiner). Nur dann werden X-Forwarded-* Header ausgewertet.
PROXY_ANZAHL = int(os.environ.get("PROXY_ANZAHL", "0") or 0)
if PROXY_ANZAHL > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_ANZAHL, x_proto=PROXY_ANZAHL, x_host=PROXY_ANZAHL)


class Sitzung(SecureCookieSessionInterface):
    """Sitzung als signiertes Cookie UND zusätzlich im Header "X-Sitzung".

    Manche Umgebungen verwerfen das Cookie (Secure-Cookie über http, strenge Safari-/Datenschutz-
    Einstellungen, Proxys, gleichnamige Cookies anderer Apps). Die Oberfläche merkt sich deshalb den
    Header-Wert und schickt ihn bei jeder Anfrage mit – dann klappt die Anmeldung trotzdem.
    """

    @staticmethod
    def _https():
        return request.is_secure

    def get_cookie_secure(self, app):
        # "auto": Secure nur, wenn die Seite wirklich per HTTPS läuft – sonst würde der Browser das Cookie verwerfen
        if COOKIE_SECURE == "true":
            return True
        if COOKIE_SECURE == "false":
            return False
        return self._https()

    def open_session(self, app, req):
        token = req.headers.get("X-Sitzung", "")
        if token:
            s = self.get_signing_serializer(app)
            try:
                return self.session_class(s.loads(token, max_age=int(app.permanent_session_lifetime.total_seconds())))
            except Exception:
                pass
        return super().open_session(app, req)

    def save_session(self, app, sess, response):
        super().save_session(app, sess, response)
        if request.path.startswith("/api/"):
            response.headers["X-Sitzung"] = self.get_signing_serializer(app).dumps(dict(sess)) if sess else ""


app.session_interface = Sitzung()


# ------------------------------------------------------------------ Hilfen
class Fehler(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.text, self.status = text, status


@app.errorhandler(Fehler)
def _fehler(e):
    return jsonify(fehler=e.text), e.status


def ms():
    return int(time.time() * 1000)


def cents(wert):
    """Euro-Betrag ("1,20", "1.2", 1.2) → ganze Cent. Exakt gerechnet (Decimal), höchstens zwei Nachkommastellen."""
    from decimal import Decimal, InvalidOperation
    try:
        v = Decimal(str(wert).replace(",", ".").strip())
    except (TypeError, ValueError, InvalidOperation):
        raise Fehler("Ungültiger Betrag.")
    if not v.is_finite() or abs(v) > 100000:
        raise Fehler("Ungültiger Betrag.")
    if v != v.quantize(Decimal("0.01")):
        raise Fehler("Bitte Beträge mit höchstens zwei Nachkommastellen eingeben (z. B. 1,25).")
    return int(v * 100)


def eur_txt(c):
    s = f"{abs(c) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if c < 0 else "") + s + " €"


def zeit_txt(t):
    return datetime.fromtimestamp(t / 1000, TZ).strftime("%d.%m.%Y %H:%M:%S")


def einmal_passwort():
    z = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne 0/O, 1/l/I
    p = "".join(secrets.choice(z) for _ in range(10))
    return p[:5] + "-" + p[5:]


def einmal_pin():
    while True:
        p = "".join(secrets.choice("0123456789") for _ in range(6))
        if not pin_zu_einfach(p):
            return p


def pin_zu_einfach(p):
    if len(set(p)) == 1:
        return True
    d = [int(b) - int(a) for a, b in zip(p, p[1:])]
    return all(x == 1 for x in d) or all(x == -1 for x in d)


def einmal_zugang(rolle):
    return einmal_pin() if rolle == "kunde" else einmal_passwort()


def pruefe_geheimnis(rolle, neu):
    """PIN für Mitglieder, Passwort für Shopverantwortliche/Administratoren."""
    if rolle == "kunde":
        if not PIN_RE.match(neu):
            raise Fehler("Die PIN besteht aus 4 bis 8 Ziffern.")
        if pin_zu_einfach(neu):
            raise Fehler("Diese PIN ist zu leicht zu erraten (z. B. 1111 oder 1234).")
    else:
        if len(neu) < MIN_PW[rolle]:
            raise Fehler(f"Das Passwort braucht mindestens {MIN_PW[rolle]} Zeichen.")
        if len(neu) > 200:
            raise Fehler("Das Passwort ist zu lang.")


def beleg(vid):
    return f"V-{1000 + vid}"


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def _close(_e):
    c = g.pop("db", None)
    if c is not None:
        c.close()


class tx:
    """Schreibtransaktion: `with tx() as c:`"""

    def __enter__(self):
        self.c = db()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, typ, *_):
        self.c.execute("ROLLBACK" if typ else "COMMIT")
        return False


def log(c, wer, was, detail=""):
    c.execute("INSERT INTO journal(zeit, wer, was, detail) VALUES (?,?,?,?)", (ms(), wer, was, detail))


# ------------------------------------------------------------------ Datenbank
SCHEMA = """
CREATE TABLE IF NOT EXISTS einstellungen (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS konten (
  id INTEGER PRIMARY KEY, nummer TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  rolle TEXT NOT NULL CHECK (rolle IN ('kunde','personal','inhaber')),
  pw_hash TEXT NOT NULL, muss_aendern INTEGER NOT NULL DEFAULT 1, zurueckgesetzt_von TEXT,
  guthaben INTEGER NOT NULL DEFAULT 0, fehler INTEGER NOT NULL DEFAULT 0,
  gesperrt_bis INTEGER NOT NULL DEFAULT 0, angelegt INTEGER NOT NULL, pw_geaendert INTEGER,
  entfernt INTEGER NOT NULL DEFAULT 0,
  pin_hash TEXT, pin_muss_aendern INTEGER NOT NULL DEFAULT 0,
  tel TEXT NOT NULL DEFAULT '', mail TEXT NOT NULL DEFAULT '', raum TEXT NOT NULL DEFAULT '',
  vorname TEXT NOT NULL DEFAULT '', nachname TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS produkte (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kat TEXT NOT NULL, preis INTEGER NOT NULL CHECK (preis >= 0),
  bestand INTEGER NOT NULL DEFAULT 0, form TEXT NOT NULL DEFAULT 'dose', ml INTEGER NOT NULL DEFAULT 330,
  farbe TEXT, farbe2 TEXT, bild BLOB, bild_typ TEXT, bild_v INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0, geloescht INTEGER NOT NULL DEFAULT 0, shop_id INTEGER);
CREATE TABLE IF NOT EXISTS verkaeufe (
  id INTEGER PRIMARY KEY, konto_id INTEGER NOT NULL REFERENCES konten(id), name TEXT NOT NULL,
  zeit INTEGER NOT NULL, summe INTEGER NOT NULL, storniert INTEGER NOT NULL DEFAULT 0, shop_id INTEGER);
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, ort TEXT NOT NULL DEFAULT '', aktiv INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0, angelegt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS shop_team (
  shop_id INTEGER NOT NULL, konto_id INTEGER NOT NULL, PRIMARY KEY (shop_id, konto_id));
CREATE TABLE IF NOT EXISTS bestand (
  produkt_id INTEGER NOT NULL, shop_id INTEGER NOT NULL, menge INTEGER NOT NULL DEFAULT 0,
  gefuehrt INTEGER NOT NULL DEFAULT 1, lager INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (produkt_id, shop_id));
CREATE TABLE IF NOT EXISTS lagerbuch (
  id INTEGER PRIMARY KEY, zeit INTEGER NOT NULL, shop_id INTEGER NOT NULL, produkt_id INTEGER NOT NULL,
  name TEXT NOT NULL, art TEXT NOT NULL, menge INTEGER NOT NULL, ek INTEGER, von TEXT NOT NULL,
  notiz TEXT NOT NULL DEFAULT '', ort TEXT NOT NULL DEFAULT 'kuehlschrank');
CREATE INDEX IF NOT EXISTS i_lager ON lagerbuch(shop_id, zeit);
CREATE TABLE IF NOT EXISTS posten (
  id INTEGER PRIMARY KEY, verkauf_id INTEGER NOT NULL REFERENCES verkaeufe(id), pid INTEGER,
  name TEXT NOT NULL, kat TEXT NOT NULL, preis INTEGER NOT NULL, menge INTEGER NOT NULL,
  storno INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kassenbuch (
  id INTEGER PRIMARY KEY, konto_id INTEGER NOT NULL, zeit INTEGER NOT NULL, art TEXT NOT NULL,
  betrag INTEGER NOT NULL, von TEXT NOT NULL, notiz TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS kasse (
  id INTEGER PRIMARY KEY, zeit INTEGER NOT NULL, art TEXT NOT NULL, betrag INTEGER NOT NULL, von TEXT NOT NULL,
  kategorie TEXT NOT NULL DEFAULT '', zweck TEXT NOT NULL DEFAULT '', beleg TEXT NOT NULL DEFAULT '',
  konto_id INTEGER, pruefung_id INTEGER, storno_von INTEGER);
CREATE INDEX IF NOT EXISTS i_kasse ON kasse(zeit);
CREATE TABLE IF NOT EXISTS kassenpruefungen (
  id INTEGER PRIMARY KEY, zeit INTEGER NOT NULL, soll INTEGER NOT NULL, ist INTEGER NOT NULL, differenz INTEGER NOT NULL,
  pruefer TEXT NOT NULL, pruefer2 TEXT NOT NULL DEFAULT '', zaehlung TEXT NOT NULL DEFAULT '{}',
  bemerkung TEXT NOT NULL DEFAULT '', gebucht INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sitzungen (
  id TEXT PRIMARY KEY, konto_id INTEGER NOT NULL, modus TEXT NOT NULL, erstellt INTEGER NOT NULL,
  gesehen INTEGER NOT NULL, bleiben INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS i_sitz_konto ON sitzungen(konto_id);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY, zeit INTEGER NOT NULL, wer TEXT NOT NULL, was TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS i_verk_konto ON verkaeufe(konto_id, zeit);
CREATE INDEX IF NOT EXISTS i_verk_zeit ON verkaeufe(zeit);
CREATE INDEX IF NOT EXISTS i_posten ON posten(verkauf_id);
CREATE INDEX IF NOT EXISTS i_kb ON kassenbuch(konto_id, zeit);
CREATE INDEX IF NOT EXISTS i_journal ON journal(zeit);
"""

TABELLEN = ["einstellungen", "konten", "shops", "shop_team", "produkte", "bestand", "verkaeufe", "posten",
            "kassenbuch", "lagerbuch", "kasse", "kassenpruefungen", "journal"]
STANDARD_SHOP = os.environ.get("ERSTER_SHOP", "Halle 1")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default).replace(",", "."))
    except ValueError:
        return float(default)


DEFAULT_EINSTELLUNGEN = {
    "shopName": os.environ.get("SHOP_NAME", "Kaffeekasse"),
    "limit": _env_float("LIMIT_EUR", "30"),
    "autoLogout": int(_env_float("AUTO_LOGOUT_SECONDS", "300")),
    "autoLogoutTeam": int(_env_float("AUTO_LOGOUT_TEAM_SECONDS", "900")),
    "minBestand": int(_env_float("MIN_BESTAND", "5")),
    "adresse": os.environ.get("ADRESSE", os.environ.get("KIOSK_ADRESSE", "http://kaffeekasse.lokal")),
    # Nachfüll-Benachrichtigung per Webhook (z. B. ntfy oder Home Assistant)
    "meldeUrl": os.environ.get("MELDE_URL", ""),
    "meldeFormat": os.environ.get("MELDE_FORMAT", "ntfy"),
    # Hinweis im Einkauf, wenn das Guthaben unter diesen Betrag fällt (0 = aus)
    "warnGuthaben": _env_float("WARN_GUTHABEN_EUR", "5"),
    # Ansprechpartner für den Datenschutzhinweis (frei einstellbar)
    "datenschutzKontakt": os.environ.get("DATENSCHUTZ_KONTAKT", ""),
    # Aussehen der ganzen Kaffeekasse (alle Geräte). Hell/Dunkel wählt weiterhin jede Person selbst.
    "stil": os.environ.get("STIL", "standard") if os.environ.get("STIL", "standard") in ("standard", "hangar", "dashboard") else "standard",
}
# Verfügbare Stile: Schlüssel → Name in der Verwaltung. Neue Stile: hier eintragen + Block in app.css.
STILE = {"standard": "Standard", "hangar": "Hangar (Glas & Remove Before Flight)", "dashboard": "Dashboard (wie Home Assistant)"}

DEFAULT_PRODUKTE = [
    ("Coca-Cola 0,33 l", "Softdrinks", 120, 24, "dose", 330, "#D2122E", "#8E0C1F"),
    ("Coca-Cola Zero 0,33 l", "Softdrinks", 120, 24, "dose", 330, "#1A1A1A", "#D2122E"),
    ("Fanta Orange 0,33 l", "Softdrinks", 120, 18, "dose", 330, "#F07E13", "#C4560A"),
    ("Fanta Exotic 0,33 l", "Softdrinks", 120, 3, "dose", 330, "#E0357F", "#A31E5A"),
    ("Fanta Zitrone 0,33 l", "Softdrinks", 120, 12, "dose", 330, "#F2C314", "#C99A08"),
    ("Pepsi Cola 0,33 l", "Softdrinks", 120, 20, "dose", 330, "#0E4C9B", "#08306B"),
    ("Pepsi Cola Max 0,33 l", "Softdrinks", 120, 20, "dose", 330, "#17181C", "#0E4C9B"),
    ("7-Up 0,33 l", "Softdrinks", 120, 14, "dose", 330, "#2FA84F", "#1C6B33"),
    ("7-Up Zero 0,33 l", "Softdrinks", 120, 14, "dose", 330, "#1F6B4A", "#98A4AC"),
    ("Monster Energy 0,5 l", "Energy", 180, 16, "dose", 500, "#1B1B1B", "#4CC93F"),
    ("Monster Energy Ultra White 0,5 l", "Energy", 180, 10, "dose", 500, "#E8ECEF", "#B0BBC3"),
    ("Mars", "Riegel", 110, 22, "riegel", 0, "#3A1F16", "#C8102E"),
    ("Snickers", "Riegel", 110, 22, "riegel", 0, "#4A2A14", "#8C5A24"),
    ("Twix", "Riegel", 110, 4, "riegel", 0, "#C79A4B", "#5A3A1C"),
]


def _seed_produkte(c, shop_ids):
    """Standard-Katalog anlegen und in den angegebenen Shops führen. Bestand 0 – außer im Demo-Modus."""
    for i, (name, kat, preis, menge, form, ml, f1, f2) in enumerate(DEFAULT_PRODUKTE):
        pid = c.execute("INSERT INTO produkte(name, kat, preis, bestand, form, ml, farbe, farbe2, sort) "
                        "VALUES (?,?,?,0,?,?,?,?,?)", (name, kat, preis, form, ml, f1, f2, i)).lastrowid
        for sid in shop_ids:
            c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) VALUES (?,?,?,1)",
                      (pid, sid, menge if DEMO else 0))


def katalog_migrieren(c):
    """Bis Version 'Shops 1': jedes Produkt gehörte genau einem Shop (produkte.shop_id, produkte.bestand).
    Jetzt: ein gemeinsamer Katalog + Bestand je Shop. Gleichnamige Produkte (Name + Kategorie) werden
    zusammengeführt, ihr Bestand wandert in die Tabelle bestand, Verkäufe zeigen auf das zusammengeführte Produkt."""
    if c.execute("SELECT COUNT(*) FROM bestand").fetchone()[0]:
        return
    erster = c.execute("SELECT id FROM shops ORDER BY sort, id LIMIT 1").fetchone()[0]
    c.execute("UPDATE produkte SET shop_id=? WHERE shop_id IS NULL", (erster,))
    gruppen = {}
    for p in c.execute("SELECT * FROM produkte WHERE geloescht=0 ORDER BY id").fetchall():
        gruppen.setdefault((p["name"].strip().lower(), p["kat"].strip().lower()), []).append(p)
    for mitglieder in gruppen.values():
        haupt = mitglieder[0]
        for p in mitglieder:
            c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) VALUES (?,?,?,1) "
                      "ON CONFLICT(produkt_id, shop_id) DO UPDATE SET menge = menge + excluded.menge",
                      (haupt["id"], p["shop_id"], p["bestand"]))
            if p["id"] != haupt["id"]:
                if not haupt["bild_typ"] and p["bild_typ"]:
                    c.execute("UPDATE produkte SET bild=?, bild_typ=?, bild_v=bild_v+1 WHERE id=?",
                              (p["bild"], p["bild_typ"], haupt["id"]))
                c.execute("UPDATE posten SET pid=? WHERE pid=?", (haupt["id"], p["id"]))
                c.execute("DELETE FROM produkte WHERE id=?", (p["id"],))
    c.execute("UPDATE produkte SET shop_id=NULL, bestand=0")


def name_teilen(name):
    """'Vorname Nachname' oder 'Nachname, Vorname' → (vorname, nachname)."""
    name = " ".join(str(name or "").split())
    if "," in name:
        nach, vor = (x.strip() for x in name.split(",", 1))
        return vor, nach
    teile = name.split(" ")
    return " ".join(teile[:-1]), teile[-1] if teile else ""


def anzeigename(vor, nach):
    return f"{vor} {nach}".strip()


def namen_aufteilen(c):
    """Ältere Konten hatten nur ein Namensfeld → einmalig in Vor- und Nachname aufteilen."""
    for r in c.execute("SELECT id, name FROM konten WHERE nachname='' AND name!=''").fetchall():
        vor, nach = name_teilen(r["name"])
        c.execute("UPDATE konten SET vorname=?, nachname=? WHERE id=?", (vor, nach, r["id"]))


def shops_sicherstellen(c):
    """Mindestens ein Shop muss existieren; Produkte/Verkäufe ohne Shop gehören zum ersten Shop."""
    erster = c.execute("SELECT id FROM shops ORDER BY sort, id LIMIT 1").fetchone()
    if not erster:
        sid = c.execute("INSERT INTO shops(name, sort, angelegt) VALUES (?,0,?)", (STANDARD_SHOP, ms())).lastrowid
    else:
        sid = erster[0]
    c.execute("UPDATE verkaeufe SET shop_id=? WHERE shop_id IS NULL", (sid,))
    return sid


def _seed_demo(c):
    t, tag = ms(), 86_400_000
    demo = [
        ("20000001", "Anna Behrens", "kunde", "4826", 1460, 0),
        ("20000002", "Ben Kowalski", "kunde", "4826", -830, 0),
        ("20000003", "Clara Mensah", "kunde", "4826", -3120, 0),
        ("20000004", "Deniz Yilmaz", "kunde", "4826", 310, 1),
        ("30000001", "Sabine Krüger", "personal", "probe123456", 520, 0),
    ]
    for nr, name, rolle, geheim, gh, muss in demo:
        pw_hash = generate_password_hash(geheim) if rolle != "kunde" else "!"
        pin_hash = generate_password_hash(geheim if rolle == "kunde" else "4826")
        c.execute("INSERT OR IGNORE INTO konten(nummer, name, rolle, pw_hash, muss_aendern, pin_hash, pin_muss_aendern, "
                  "zurueckgesetzt_von, guthaben, angelegt) VALUES (?,?,?,?,0,?,?,?,?,?)",
                  (nr, name, rolle, pw_hash, pin_hash, muss,
                   "Sabine Krüger" if muss else None, gh, t - 40 * tag))
    k = {r["nummer"]: r["id"] for r in c.execute("SELECT id, nummer FROM konten")}
    p = {r["name"]: r for r in c.execute("SELECT * FROM produkte WHERE geloescht=0")}
    kaeufe = [("20000001", "Anna Behrens", 2 * tag, [("Coca-Cola 0,33 l", 2), ("Snickers", 1)]),
              ("20000002", "Ben Kowalski", tag, [("Monster Energy 0,5 l", 2), ("Mars", 1)]),
              ("20000004", "Deniz Yilmaz", 5 * 3_600_000, [("Fanta Orange 0,33 l", 1), ("Pepsi Cola Max 0,33 l", 1)]),
              ("20000001", "Anna Behrens", 2 * 3_600_000, [("Monster Energy Ultra White 0,5 l", 1), ("Fanta Zitrone 0,33 l", 1)])]
    h1 = c.execute("SELECT id FROM shops ORDER BY sort, id LIMIT 1").fetchone()[0]
    for nr, name, alt, pos in kaeufe:
        summe = sum(p[n]["preis"] * m for n, m in pos)
        vid = c.execute("INSERT INTO verkaeufe(konto_id, name, zeit, summe, shop_id) VALUES (?,?,?,?,?)",
                        (k[nr], name, t - alt, summe, h1)).lastrowid
        for n, m in pos:
            c.execute("INSERT INTO posten(verkauf_id, pid, name, kat, preis, menge, ek) VALUES (?,?,?,?,?,?,?)",
                      (vid, p[n]["id"], n, p[n]["kat"], p[n]["preis"], m, round(p[n]["preis"] * 0.55)))
    c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
              (k["20000001"], t - 30 * tag, "Aufladung", 2000, "Sabine Krüger", "bar"))
    c.execute("UPDATE produkte SET ek = CAST(preis * 0.55 AS INTEGER)")
    c.execute("UPDATE bestand SET lager = 24")                       # Vorrat im Lagerraum
    # zweite Halle mit demselben Katalog (eigener Bestand), Sabine ist dort verantwortlich
    h2 = c.execute("INSERT INTO shops(name, ort, sort, angelegt) VALUES ('Halle 2', 'Werkstatt', 1, ?)", (t,)).lastrowid
    c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) SELECT produkt_id, ?, menge, 1 FROM bestand WHERE shop_id=?",
              (h2, h1))
    c.execute("INSERT INTO shop_team(shop_id, konto_id) VALUES (?,?)", (h2, k["30000001"]))


def _migrieren(c):
    """Ältere Datenbanken: Kunden-PIN lag in pw_hash → in eigene Spalte pin_hash verschieben."""
    spalten = {r[1] for r in c.execute("PRAGMA table_info(konten)")}
    if "pin_hash" not in spalten:
        c.execute("ALTER TABLE konten ADD COLUMN pin_hash TEXT")
        c.execute("ALTER TABLE konten ADD COLUMN pin_muss_aendern INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE konten SET pin_hash=pw_hash, pin_muss_aendern=muss_aendern, pw_hash='!', muss_aendern=0 "
                  "WHERE rolle='kunde'")
    spalten = {r[1] for r in c.execute("PRAGMA table_info(konten)")}
    for sp in ("tel", "mail", "raum", "vorname", "nachname"):   # dienstliche Erreichbarkeit, getrennte Namen
        if sp not in spalten:
            c.execute(f"ALTER TABLE konten ADD COLUMN {sp} TEXT NOT NULL DEFAULT ''")
    for tab in ("produkte", "verkaeufe"):        # mehrere Shops
        if "shop_id" not in {r[1] for r in c.execute(f"PRAGMA table_info({tab})")}:
            c.execute(f"ALTER TABLE {tab} ADD COLUMN shop_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS i_prod_shop ON produkte(shop_id)")
    if "ek" not in {r[1] for r in c.execute("PRAGMA table_info(produkte)")}:
        c.execute("ALTER TABLE produkte ADD COLUMN ek INTEGER")        # letzter Einkaufspreis je Stück (Cent)
    if "ek" not in {r[1] for r in c.execute("PRAGMA table_info(posten)")}:
        c.execute("ALTER TABLE posten ADD COLUMN ek INTEGER")          # Einkaufspreis zum Verkaufszeitpunkt
    c.execute("CREATE INDEX IF NOT EXISTS i_verk_shop ON verkaeufe(shop_id, zeit)")
    # Bestand je Shop: menge = Kühlschrank (wird verkauft), lager = Vorrat im Lagerraum der Halle
    if "lager" not in {r[1] for r in c.execute("PRAGMA table_info(bestand)")}:
        c.execute("ALTER TABLE bestand ADD COLUMN lager INTEGER NOT NULL DEFAULT 0")
    if "sort" not in {r[1] for r in c.execute("PRAGMA table_info(bestand)")}:
        c.execute("ALTER TABLE bestand ADD COLUMN sort INTEGER")          # eigene Reihenfolge je Shop
    if "ort" not in {r[1] for r in c.execute("PRAGMA table_info(lagerbuch)")}:
        c.execute("ALTER TABLE lagerbuch ADD COLUMN ort TEXT NOT NULL DEFAULT 'kuehlschrank'")
    if "stillgelegt" not in {r[1] for r in c.execute("PRAGMA table_info(konten)")}:
        c.execute("ALTER TABLE konten ADD COLUMN stillgelegt INTEGER NOT NULL DEFAULT 0")   # Zeitpunkt, 0 = aktiv
    # E-Mail wird nicht mehr verwendet: gespeicherte Adressen und die Einstellung löschen (Datensparsamkeit)
    c.execute("UPDATE konten SET mail='' WHERE mail!=''")
    c.execute("DELETE FROM einstellungen WHERE key='meldeMail'")


def init_db():
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("BEGIN")
    _migrieren(c)
    for k, v in DEFAULT_EINSTELLUNGEN.items():
        c.execute("INSERT OR IGNORE INTO einstellungen(key, value) VALUES (?,?)", (k, json.dumps(v)))
    neu = c.execute("SELECT COUNT(*) FROM produkte").fetchone()[0] == 0
    sid = shops_sicherstellen(c)
    if neu:
        _seed_produkte(c, [sid])
    else:
        katalog_migrieren(c)
    # Kein Standard-Inhaber: Beim ersten Aufruf fragt die Oberfläche Name, Personalnummer,
    # Passwort und PIN ab (siehe /api/einrichtung).
    if DEMO and c.execute("SELECT COUNT(*) FROM konten").fetchone()[0] == 0:
        _seed_demo(c)
    namen_aufteilen(c)
    if not c.execute("SELECT 1 FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone():
        print("Kaffeekasse: Ersteinrichtung offen – bitte die Weboberfläche öffnen und den Administrator anlegen.",
              flush=True)
    bilder_pruefen_einmalig(c)
    c.execute("COMMIT")
    c.close()


def einstellungen():
    return {r["key"]: json.loads(r["value"]) for r in db().execute("SELECT * FROM einstellungen")}


# ------------------------------------------------------------------ Sitzung
# Jede Person hat EIN Konto (Personalnummer, Guthaben).
#  - Anmeldung mit PIN      → Modus "kunde":      Shop, eigenes Konto
#  - Anmeldung mit Passwort → Modus "verwaltung": nur Shopverantwortliche/Administratoren, Verwaltungsbereich
def modus():
    return session.get("modus", "kunde")


def eff_rolle(k):
    return "kunde" if modus() == "kunde" else k["rolle"]


def konto_public(k, selbst=False):
    d = {"id": k["id"], "nummer": k["nummer"], "benutzer": k["nummer"], "name": k["name"],
         "rolle": k["rolle"], "guthaben": k["guthaben"] / 100,
         "pinMussAendern": bool(k["pin_muss_aendern"]), "pwMussAendern": bool(k["muss_aendern"]),
         "mussAendern": bool(k["pin_muss_aendern"] or (k["rolle"] != "kunde" and k["muss_aendern"])),
         "hatPin": bool(k["pin_hash"]), "zurueckgesetztVon": k["zurueckgesetzt_von"],
         "gesperrt": k["gesperrt_bis"] > ms(), "angelegt": k["angelegt"],
         "tel": k["tel"], "raum": k["raum"],
         "vorname": k["vorname"], "nachname": k["nachname"] or k["name"],
         "stillgelegt": k["stillgelegt"] if "stillgelegt" in k.keys() else 0}
    if selbst:
        d["echteRolle"] = k["rolle"]
        d["rolle"] = eff_rolle(k)
        d["modus"] = modus()
    return d


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


BLEIBEN_MS = 30 * 86_400_000


def _sitzung_beenden():
    sid = session.get("sid")
    if sid:
        db().execute("DELETE FROM sitzungen WHERE id=?", (sid,))
    for key in ("uid", "sid", "modus", "bleiben", "pending", "pending_modus", "pending_zeit"):
        session.pop(key, None)


def sitzungen_beenden(c, kid, ausser=None):
    """Alle Anmeldungen eines Kontos beenden (nach PIN-/Passwortänderung, Zurücksetzen, Rollenwechsel, Löschen)."""
    c.execute("DELETE FROM sitzungen WHERE konto_id=? AND id IS NOT ?", (kid, ausser))


def aktueller_nutzer():
    """Die Sitzung wird auf dem Server geführt (Tabelle sitzungen): Abmelden, Zeitlimit und
    PIN-/Passwortänderungen wirken sofort – auch für kopierte oder alte Cookies."""
    if "user" in g:
        return g.user
    g.user = None
    uid, sid = session.get("uid"), session.get("sid")
    if not uid or not sid:
        return None
    c = db()
    s = c.execute("SELECT * FROM sitzungen WHERE id=?", (sid,)).fetchone()
    if not s or s["konto_id"] != uid:
        _sitzung_beenden()
        return None
    k = c.execute("SELECT * FROM konten WHERE id=? AND entfernt=0", (uid,)).fetchone()
    m = s["modus"]
    ungueltig = (not k or k["stillgelegt"] or (m == "kunde" and (not k["pin_hash"] or k["pin_muss_aendern"]))
                 or (m == "verwaltung" and (k["rolle"] == "kunde" or k["muss_aendern"])))
    e = einstellungen()
    limit = BLEIBEN_MS if s["bleiben"] and m == "kunde" else \
        (e["autoLogout"] if m == "kunde" else e["autoLogoutTeam"]) * 1000 + 15_000
    jetzt = ms()
    if ungueltig or jetzt - s["gesehen"] > limit:
        _sitzung_beenden()
        return None
    if jetzt - s["gesehen"] > 5_000:      # nicht bei jeder Anfrage schreiben
        c.execute("UPDATE sitzungen SET gesehen=? WHERE id=?", (jetzt, sid))
    session["modus"] = m
    g.user = k
    return k


def starte_sitzung(k, bleiben, m):
    csrf = session.get("csrf")
    alt = session.get("sid")
    session.clear()
    bleiben = bool(bleiben) and m == "kunde"
    sid = secrets.token_urlsafe(32)
    jetzt = ms()
    c = db()
    if alt:
        c.execute("DELETE FROM sitzungen WHERE id=?", (alt,))
    c.execute("DELETE FROM sitzungen WHERE gesehen < ?", (jetzt - BLEIBEN_MS,))   # Aufräumen
    c.execute("INSERT INTO sitzungen(id, konto_id, modus, erstellt, gesehen, bleiben) VALUES (?,?,?,?,?,?)",
              (sid, k["id"], m, jetzt, jetzt, int(bleiben)))
    session["csrf"] = csrf or secrets.token_urlsafe(24)
    session["uid"] = k["id"]
    session["sid"] = sid
    session["modus"] = m
    session["bleiben"] = bleiben
    session.permanent = bleiben


def api(rollen=ROLLEN):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            k = aktueller_nutzer()
            if not k:
                raise Fehler("Bitte neu anmelden.", 401)
            if eff_rolle(k) not in rollen:
                raise Fehler("Dafür fehlt dir die Berechtigung. Für die Verwaltung mit Passwort anmelden.", 403)
            return fn(k, *a, **kw)
        return wrapper
    return deco


TEAM = ("personal", "inhaber")
INHABER = ("inhaber",)


def _gleiche_herkunft():
    """Origin/Referer zeigt auf diesen Server (auch hinter Reverse Proxy)."""
    from urllib.parse import urlparse
    quelle = request.headers.get("Origin") or request.headers.get("Referer") or ""
    host = urlparse(quelle).netloc
    erlaubt = {request.host, request.headers.get("X-Forwarded-Host", "")}
    return bool(host) and host in erlaubt


@app.before_request
def _csrf():
    # Schutz gegen fremde Seiten: Token aus der Sitzung ODER Anfrage von derselben Herkunft
    # (+ eigener Header, den fremde Seiten ohne CORS-Freigabe nicht setzen können).
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith("/api/"):
        token = request.headers.get("X-CSRF")
        if not token:
            raise Fehler("Ungültige Anfrage – bitte Seite neu laden.", 400)
        if not hmac.compare_digest(token, str(session.get("csrf", ""))) and not _gleiche_herkunft():
            raise Fehler("Sitzung abgelaufen – bitte Seite neu laden.", 400)


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def body():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise Fehler("Ungültige Anfrage.")
    return d


# ------------------------------------------------------------------ Seiten
def _version():
    """Versionskennung aus den Dateien – ändert sich bei jedem Update, damit Browser nichts Altes aus dem Cache nehmen."""
    import hashlib
    h = hashlib.sha1()
    for n in ("app.js", "app.css", "index.html"):
        try:
            with open(os.path.join(STATIC, n), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


VERSION = _version()
# Sichtbare Versionsnummer (Fußzeile). Beim Bau über GitHub wird sie aus dem Git-Tag gesetzt, z. B. "v1.3.0".
APP_VERSION = re.sub(r"^main-([0-9a-f]{7})[0-9a-f]+$", r"main-\1",          # Commit-Kennung kürzen
                     "".join(ch for ch in os.environ.get("KAFFEEKASSE_VERSION", "") if ch.isalnum() or ch in ".-+_")[:60]) \
    or f"Entwicklungsstand {VERSION}"
# Alles vor 1.0 (v0.x, Vorabversionen wie v0.3.0-rc.1, Entwicklungsstände) gilt als Beta.
IST_BETA = not re.match(r"^v?[1-9]\d*\.\d+\.\d+$", APP_VERSION)
APP_VERSION_TXT = APP_VERSION + (" · Beta" if IST_BETA else "")


def _csp():
    """Nur eigene Skripte + das eine Inline-Skript in index.html (per Hash). Schützt vor eingeschleustem Code."""
    import hashlib
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    hashes = [f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
              for s in re.findall(r"<script>(.*?)</script>", html, re.S)]
    return ("default-src 'self'; script-src 'self' " + " ".join(hashes) + "; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


CSP = _csp()


@app.get("/")
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/app.css", f"/static/app.css?v={VERSION}").replace("/static/app.js", f"/static/app.js?v={VERSION}")
    html = html.replace("__APP_VERSION__", APP_VERSION_TXT)
    # Stil schon im HTML setzen – so blitzt beim Laden nicht kurz das Standard-Design auf
    stil = einstellungen().get("stil", "standard")
    html = html.replace('<html lang="de">', f'<html lang="de" data-stil="{stil if stil in STILE else "standard"}">', 1)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/static/<path:name>")
def static_files(name):
    resp = send_from_directory(STATIC, name)
    # mit ?v=… lange cachen (neue Version = neue Adresse), ohne immer neu prüfen
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "no-cache"
    return resp


@app.get("/healthz")
def healthz():
    db().execute("SELECT 1")
    return "ok"


@app.get("/bild/<int:pid>")
def bild(pid):
    r = db().execute("SELECT bild, bild_typ FROM produkte WHERE id=?", (pid,)).fetchone()
    if not r or not r["bild"] or r["bild_typ"] not in BILD_TYPEN:
        raise Fehler("Kein Bild", 404)
    # Auch wenn doch einmal etwas anderes in der Datenbank stünde: nur als Bild, nie als Seite oder Skript
    return Response(r["bild"], mimetype=r["bild_typ"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
                             "Content-Disposition": f'inline; filename="produkt-{pid}"',
                             "Cross-Origin-Resource-Policy": "same-origin"})


# ------------------------------------------------------------------ Ersteinrichtung
EINRICHTUNGS_CODE = os.environ.get("EINRICHTUNGS_CODE", "").strip()


def einrichtung_offen():
    return not db().execute("SELECT 1 FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone()


@app.post("/api/einrichtung")
def einrichtung():
    """Legt beim allerersten Start den Administrator an. Danach ist dieser Weg gesperrt."""
    d = body()
    if EINRICHTUNGS_CODE and str(d.get("code", "")).strip() != EINRICHTUNGS_CODE:
        raise Fehler("Der Einrichtungs-Code stimmt nicht.", 403)
    vor, nach, name = _namen(d)
    kasse = " ".join(str(d.get("shopName", "")).split())[:40]
    erster = " ".join(str(d.get("ersterShop", "")).split())[:40]
    # Die Oberfläche fragt beides ab; fehlt es (ältere Aufrufe), bleiben die Startwerte
    kasse = kasse or einstellungen().get("shopName") or "Kaffeekasse"
    pw, pin = str(d.get("passwort", "")), str(d.get("pin", ""))
    pruefe_geheimnis("inhaber", pw)
    pruefe_geheimnis("kunde", pin)
    with tx() as c:
        if c.execute("SELECT 1 FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone():
            raise Fehler("Die Kaffeekasse ist bereits eingerichtet – bitte neu laden.", 409)
        nr = _nummer(c, d)
        kid = c.execute("INSERT INTO konten(nummer, name, rolle, pw_hash, muss_aendern, pin_hash, pin_muss_aendern, "
                        "guthaben, angelegt, pw_geaendert) VALUES (?,?,?,?,0,?,0,0,?,?)",
                        (nr, name, "inhaber", generate_password_hash(pw), generate_password_hash(pin), ms(), ms())).lastrowid
        c.execute("UPDATE konten SET vorname=?, nachname=? WHERE id=?", (vor, nach, kid))
        c.execute("INSERT OR REPLACE INTO einstellungen(key, value) VALUES ('shopName', ?)", (json.dumps(kasse),))
        sid = shops_sicherstellen(c)
        erster = erster or c.execute("SELECT name FROM shops WHERE id=?", (sid,)).fetchone()[0]
        c.execute("UPDATE shops SET name=? WHERE id=?", (erster, sid))
        if not DEMO:                           # frischer Start: alles leer, Bestand kommt über Wareneingang/Inventur
            c.execute("UPDATE bestand SET menge=0, lager=0")
        log(c, name, "Ersteinrichtung", f"Kaffeekasse „{kasse}“, erster Shop „{erster}“, Administrator {name} · {nr} angelegt"
            + ("" if DEMO else " · alle Bestände auf 0"))
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    starte_sitzung(k, False, "verwaltung")
    return jsonify(user=konto_public(k, True), csrf=session["csrf"])


# ------------------------------------------------------------------ Anmeldung
@app.get("/api/me")
def me():
    k = aktueller_nutzer()
    e = einstellungen()
    demo = []
    if DEMO:
        demo = [["20000001", "4826", "pin", "Kundin, Guthaben im Plus"],
                ["20000003", "4826", "pin", "Kundin, Konto gesperrt"],
                ["20000004", "4826", "pin", "Kunde, PIN zurückgesetzt"],
                ["30000001", "4826", "pin", "Shopverantwortliche(r) – einkaufen (PIN)"],
                ["30000001", "probe123456", "passwort", "Shopverantwortliche(r) – verwalten (Passwort)"]]
    einrichtung = None
    if einrichtung_offen():
        einrichtung = {"code": bool(EINRICHTUNGS_CODE)}
    shops = [{"id": r["id"], "name": r["name"], "ort": r["ort"]} for r in alle_shops() if r["aktiv"]]
    return jsonify(user=konto_public(k, True) if k else None, csrf=csrf_token(),
                   bleiben=bool(session.get("bleiben")), shopName=e["shopName"], demo=demo,
                   einrichtung=einrichtung, shops=shops, stil=e.get("stil", "standard"), stile=STILE,
                   datenschutzKontakt=e.get("datenschutzKontakt", ""))


@app.post("/api/ping")
def ping():
    return jsonify(ok=bool(aktueller_nutzer()))


DUMMY_HASH = generate_password_hash(secrets.token_hex(8))
LOGIN_IP_MAX = int(os.environ.get("LOGIN_SPERRE_IP", "30") or 0)   # Fehlversuche je IP in 15 Minuten
_ip_fehler = {}
PENDING_MS = 10 * 60_000     # so lange darf man nach der Einmal-PIN die eigene PIN festlegen


def _ip():
    return request.remote_addr or "?"


def _ip_gesperrt():
    if LOGIN_IP_MAX <= 0:
        return False
    jetzt = ms()
    liste = [t for t in _ip_fehler.get(_ip(), []) if jetzt - t < SPERRE_MS]
    _ip_fehler[_ip()] = liste
    return len(liste) >= LOGIN_IP_MAX


def _ip_fehlversuch():
    if LOGIN_IP_MAX > 0:
        _ip_fehler.setdefault(_ip(), []).append(ms())
        if len(_ip_fehler) > 5000:                     # Speicher begrenzen
            _ip_fehler.clear()


@app.post("/api/login")
def login():
    d = body()
    if _ip_gesperrt():
        raise Fehler("Zu viele Fehlversuche von diesem Gerät/Netz. Bitte 15 Minuten warten.", 429)
    nr = str(d.get("nummer", "")).strip()
    geheim = str(d.get("passwort", ""))
    # Ein Eingabefeld: 4–8 Ziffern = PIN (Einkauf), alles andere = Passwort (Verwaltung + Einkauf).
    # Passwörter haben mindestens 10 Zeichen, eine Verwechslung ist daher ausgeschlossen.
    wunsch = d.get("modus")
    if wunsch not in ("pin", "passwort"):
        wunsch = "pin" if PIN_RE.match(geheim) else "passwort"
    m = "verwaltung" if wunsch == "passwort" else "kunde"
    wort = "Passwort" if m == "verwaltung" else "PIN"
    if not NUM_RE.match(nr):
        raise Fehler("Die Personalnummer hat genau 8 Ziffern.")
    if not geheim:
        raise Fehler(f"Bitte Personalnummer und {wort} eingeben.")
    with tx() as c:
        k = c.execute("SELECT * FROM konten WHERE nummer=? AND entfernt=0", (nr,)).fetchone()
        jetzt = ms()
        if k and k["gesperrt_bis"] > jetzt:
            raise Fehler("Zu viele Fehlversuche. Erneut möglich in "
                         f"{math.ceil((k['gesperrt_bis'] - jetzt) / 60000)} Minuten.", 429)
        hash_ = None
        if k:
            hash_ = k["pin_hash"] if m == "kunde" else (k["pw_hash"] if k["rolle"] != "kunde" else None)
        passt = check_password_hash(hash_ or DUMMY_HASH, geheim) and bool(hash_)
        if not passt:
            _ip_fehlversuch()
            if k:
                f = k["fehler"] + 1
                if f >= MAX_FEHLER:
                    c.execute("UPDATE konten SET fehler=0, gesperrt_bis=? WHERE id=?", (jetzt + SPERRE_MS, k["id"]))
                    log(c, k["name"], "Zugang vorübergehend gesperrt", "fünf Fehlversuche")
                else:
                    c.execute("UPDATE konten SET fehler=? WHERE id=?", (f, k["id"]))
                    log(c, k["name"], "Anmeldung fehlgeschlagen", f"{wort} · Versuch {f} von {MAX_FEHLER}")
            fehler_text = "Personalnummer oder PIN/Passwort stimmt nicht."
        elif k["stillgelegt"]:
            # erst NACH richtiger PIN verraten – sonst ließe sich herausfinden, welche Nummern es gibt
            fehler_text = "Dieses Konto ist stillgelegt. Bitte bei den Shopverantwortlichen melden."
            log(c, k["name"], "Anmeldung abgelehnt", "Konto stillgelegt")
        else:
            fehler_text = None
            muss = k["pin_muss_aendern"] if m == "kunde" else k["muss_aendern"]
            c.execute("UPDATE konten SET fehler=0, gesperrt_bis=0 WHERE id=?", (k["id"],))
            if not muss:
                log(c, k["name"], "Anmeldung", "Einkauf (PIN)" if m == "kunde" else "Verwaltung (Passwort)")
    if fehler_text:
        raise Fehler(fehler_text, 403 if k and k["stillgelegt"] and fehler_text.startswith("Dieses Konto") else 401)
    bleiben = bool(d.get("bleiben"))
    if (k["pin_muss_aendern"] if m == "kunde" else k["muss_aendern"]):
        csrf = csrf_token()
        session.clear()
        session.update(csrf=csrf, pending=k["id"], pending_modus=m, pending_zeit=ms(), bleiben=bleiben)
        return jsonify(pending=True, art="pin" if m == "kunde" else "passwort",
                       konto=konto_public(k), csrf=csrf)
    starte_sitzung(k, bleiben, m)
    return jsonify(user=konto_public(k, True), csrf=session["csrf"])


def freigabe_pruefen(u, d, wofuer):
    """Vier-Augen-Prinzip: Eine zweite Person (jedes aktive Konto außer dem eigenen) bestätigt mit Personalnummer und
    ihrer PIN. Eine noch nicht geänderte Einmal-PIN zählt nicht – die kennt ja, wer sie vergeben hat.
    Fehlversuche zählen wie bei der Anmeldung (Kontosperre, IP-Sperre).
    Gibt das Konto der zweiten Person zurück oder wirft Fehler."""
    if _ip_gesperrt():
        raise Fehler("Zu viele Fehlversuche von diesem Gerät/Netz. Bitte 15 Minuten warten.", 429)
    nr = str(d.get("freigabeNummer", "")).strip()
    geheim = str(d.get("freigabePasswort", ""))
    if not nr or not geheim:
        raise Fehler(f"{wofuer} muss eine zweite Person freigeben: bitte deren Personalnummer und PIN eingeben.", 403)
    if not NUM_RE.match(nr):
        raise Fehler("Die Personalnummer der zweiten Person hat genau 8 Ziffern.")
    if nr == u["nummer"]:
        raise Fehler("Die Freigabe muss eine andere Person geben (Vier-Augen-Prinzip).", 403)
    fehler_text = None
    with tx() as c:
        k = c.execute("SELECT * FROM konten WHERE nummer=? AND entfernt=0", (nr,)).fetchone()
        jetzt = ms()
        if k and k["gesperrt_bis"] > jetzt:
            fehler_text = ("Der Zugang der zweiten Person ist wegen Fehlversuchen gesperrt. Erneut möglich in "
                           f"{math.ceil((k['gesperrt_bis'] - jetzt) / 60000)} Minuten.")
        else:
            hash_ = k["pin_hash"] if k and PIN_RE.match(geheim) else None
            if not (check_password_hash(hash_ or DUMMY_HASH, geheim) and hash_):
                _ip_fehlversuch()
                if k:
                    f = k["fehler"] + 1
                    if f >= MAX_FEHLER:
                        c.execute("UPDATE konten SET fehler=0, gesperrt_bis=? WHERE id=?", (jetzt + SPERRE_MS, k["id"]))
                        log(c, k["name"], "Zugang vorübergehend gesperrt", "fünf Fehlversuche (Freigabe)")
                    else:
                        c.execute("UPDATE konten SET fehler=? WHERE id=?", (f, k["id"]))
                        log(c, k["name"], "Freigabe fehlgeschlagen", f"für {u['name']} · Versuch {f} von {MAX_FEHLER}")
                fehler_text = "Freigabe abgelehnt: Personalnummer oder PIN der zweiten Person stimmt nicht."
            elif k["pin_muss_aendern"]:
                fehler_text = ("Freigabe abgelehnt: Die zweite Person hat noch ihre Einmal-PIN. "
                               "Sie muss sich erst einmal anmelden und eine eigene PIN festlegen.")
            elif k["stillgelegt"]:
                fehler_text = "Freigabe abgelehnt: Das Konto der zweiten Person ist stillgelegt."
            else:
                c.execute("UPDATE konten SET fehler=0 WHERE id=?", (k["id"],))
    if fehler_text:
        raise Fehler(fehler_text, 403)
    return k


@app.post("/api/logout")
def logout():
    k = aktueller_nutzer()
    if k:
        with tx() as c:
            log(c, k["name"], "Abmeldung", "automatisch" if body().get("auto") else "")
    csrf = session.get("csrf")
    _sitzung_beenden()
    session.clear()
    session["csrf"] = csrf or secrets.token_urlsafe(24)
    return jsonify(ok=True, csrf=session["csrf"])


@app.post("/api/passwort")
def passwort():
    """Ändert PIN (art=pin) oder Passwort (art=passwort).
    - nach Einmal-PIN/-Passwort (pending): ohne alte Eingabe
    - im Einkauf (PIN-Anmeldung): nur PIN, bestätigt mit alter PIN
    - in der Verwaltung: Passwort oder eigene PIN, bestätigt mit dem Passwort"""
    d = body()
    neu = str(d.get("neu", ""))
    pending = session.get("pending")
    k = aktueller_nutzer()
    if pending and not k:
        if ms() - session.get("pending_zeit", 0) > PENDING_MS:
            _sitzung_beenden()
            raise Fehler("Zu lange gewartet – bitte erneut mit der Einmal-PIN bzw. dem Einmalpasswort anmelden.", 401)
        k = db().execute("SELECT * FROM konten WHERE id=? AND entfernt=0", (pending,)).fetchone()
        if not k:
            raise Fehler("Bitte neu anmelden.", 401)
        erzwungen = True
        art = "pin" if session.get("pending_modus") == "kunde" else "passwort"
    elif k:
        erzwungen = False
        alt = str(d.get("alt", ""))
        if modus() == "kunde":
            art = "pin"
            if not check_password_hash(k["pin_hash"], alt):
                raise Fehler("Die bisherige PIN stimmt nicht.")
        else:
            art = "pin" if d.get("art") == "pin" else "passwort"
            if not check_password_hash(k["pw_hash"], alt):
                raise Fehler("Das Passwort stimmt nicht.")
    else:
        raise Fehler("Bitte neu anmelden.", 401)
    if art == "pin":
        pruefe_geheimnis("kunde", neu)
        if k["pin_hash"] and check_password_hash(k["pin_hash"], neu):
            raise Fehler("Bitte eine andere PIN als die bisherige.")
    else:
        pruefe_geheimnis(k["rolle"], neu)
        if check_password_hash(k["pw_hash"], neu):
            raise Fehler("Bitte ein anderes als das bisherige Passwort.")
    with tx() as c:
        if art == "pin":
            c.execute("UPDATE konten SET pin_hash=?, pin_muss_aendern=0, zurueckgesetzt_von=NULL WHERE id=?",
                      (generate_password_hash(neu), k["id"]))
        else:
            c.execute("UPDATE konten SET pw_hash=?, muss_aendern=0, zurueckgesetzt_von=NULL, pw_geaendert=? WHERE id=?",
                      (generate_password_hash(neu), ms(), k["id"]))
        sitzungen_beenden(c, k["id"], session.get("sid"))
        log(c, k["name"], "PIN geändert" if art == "pin" else "Passwort geändert",
            "nach Zurücksetzung" if erzwungen else "selbst")
        if erzwungen:
            log(c, k["name"], "Anmeldung", "Einkauf (PIN)" if art == "pin" else "Verwaltung (Passwort)")
    if erzwungen:
        starte_sitzung(k, session.get("bleiben"), "kunde" if art == "pin" else "verwaltung")
        k = db().execute("SELECT * FROM konten WHERE id=?", (k["id"],)).fetchone()
        return jsonify(user=konto_public(k, True), csrf=session["csrf"])
    return jsonify(ok=True)


# ------------------------------------------------------------------ Daten laden
def alle_shops(c=None):
    return (c or db()).execute("SELECT * FROM shops ORDER BY sort, name COLLATE NOCASE, id").fetchall()


def erlaubte_shops(u):
    """Shops, deren Sortiment jemand verwalten darf: Administrator alle, Shopverantwortliche die zugeordneten."""
    if u["rolle"] == "inhaber":
        return [r["id"] for r in alle_shops()]
    return [r[0] for r in db().execute("SELECT shop_id FROM shop_team WHERE konto_id=?", (u["id"],))]


def darf_shop(u, sid):
    if sid not in erlaubte_shops(u):
        raise Fehler("Für diesen Shop bist du nicht als Shopverantwortliche(r) eingetragen.", 403)


def _get_shop(c, sid, aktiv=False):
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        raise Fehler("Bitte einen Shop auswählen.")
    r = c.execute("SELECT * FROM shops WHERE id=?" + (" AND aktiv=1" if aktiv else ""), (sid,)).fetchone()
    if not r:
        raise Fehler("Diesen Shop gibt es nicht (mehr) – bitte neu laden.", 409)
    return r


ORTE = {"kuehlschrank": "Kühlschrank", "lager": "Lager"}


def bestand_map(c=None):
    """{produkt_id: {shop_id: (kuehlschrank, lager, reihenfolge)}} – nur Shops, in denen das Produkt geführt wird."""
    out = {}
    for r in (c or db()).execute("SELECT produkt_id, shop_id, menge, lager, sort FROM bestand WHERE gefuehrt=1"):
        out.setdefault(r["produkt_id"], {})[r["shop_id"]] = (r["menge"], r["lager"], r["sort"])
    return out


def produkt_public(p, bestaende, verwaltung=False):
    b = bestaende.get(p["id"], {})
    d = {"id": p["id"], "name": p["name"], "kat": p["kat"], "preis": p["preis"] / 100,
         "bestand": {str(sid): m for sid, (m, _l, _s) in b.items()},
         "reihe": {str(sid): s for sid, (_m, _l, s) in b.items() if s is not None},
         "form": p["form"], "ml": p["ml"], "farbe": p["farbe"], "farbe2": p["farbe2"],
         "bild": f"/bild/{p['id']}?v={p['bild_v']}" if p["bild_typ"] else None}
    if verwaltung:
        d["ek"] = p["ek"] / 100 if p["ek"] is not None else None
        d["lager"] = {str(sid): lg for sid, (_m, lg, _s) in b.items()}
    return d


def _zeitraum():
    """?von=&bis= in Millisekunden (vom Browser in Ortszeit berechnet)."""
    try:
        von = int(request.args.get("von") or 0)
        bis = int(request.args.get("bis") or 0) or ms() + 60_000
    except ValueError:
        raise Fehler("Ungültiger Zeitraum.")
    return von, bis


def _shops_aus_anfrage(u):
    """?shop=<id> oder alle erlaubten Shops."""
    erlaubt = erlaubte_shops(u)
    s = request.args.get("shop", "")
    if s.isdigit():
        darf_shop(u, int(s))
        return [int(s)]
    return erlaubt


def _in(ids):
    return f"({','.join(str(int(x)) for x in ids) or 'NULL'})"


def verkaeufe_liste(where="", args=(), limit=300):
    c = db()
    vs = c.execute(f"SELECT * FROM verkaeufe {where} ORDER BY zeit DESC LIMIT ?", (*args, limit)).fetchall()
    if not vs:
        return []
    ids = [v["id"] for v in vs]
    ps = c.execute(f"SELECT * FROM posten WHERE verkauf_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
    by = {}
    for p in ps:
        by.setdefault(p["verkauf_id"], []).append(
            {"id": p["id"], "pid": p["pid"], "name": p["name"], "kat": p["kat"], "preis": p["preis"] / 100,
             "menge": p["menge"], "storno": bool(p["storno"])})
    nummern = {r["id"]: r["nummer"] for r in c.execute(
        f"SELECT id, nummer FROM konten WHERE id IN ({','.join(str(v['konto_id']) for v in vs)})")}
    return [{"id": v["id"], "beleg": beleg(v["id"]), "kontoId": v["konto_id"], "nummer": nummern.get(v["konto_id"], ""),
             "name": v["name"], "zeit": v["zeit"], "shop": v["shop_id"],
             "summe": v["summe"] / 100, "storniert": bool(v["storniert"]), "posten": by.get(v["id"], [])} for v in vs]


def kennzahlen(shop_ids, von, bis):
    c = db()
    q = (f"FROM posten p JOIN verkaeufe v ON v.id = p.verkauf_id WHERE p.storno = 0 "
         f"AND v.shop_id IN {_in(shop_ids)} AND v.zeit >= ? AND v.zeit < ?")
    a = (von, bis)
    r = c.execute(f"SELECT COALESCE(SUM(p.preis*p.menge),0) umsatz, COALESCE(SUM(p.menge),0) stueck, "
                  f"COALESCE(SUM(CASE WHEN p.ek IS NOT NULL THEN p.ek*p.menge END),0) einsatz, "
                  f"COALESCE(SUM(CASE WHEN p.ek IS NOT NULL THEN (p.preis-p.ek)*p.menge END),0) roh, "
                  f"COALESCE(SUM(CASE WHEN p.ek IS NULL THEN p.preis*p.menge END),0) ohne_ek, "
                  f"COUNT(DISTINCT v.id) anzahl {q}", a).fetchone()
    top = c.execute(f"SELECT p.name, SUM(p.menge) n, SUM(p.preis*p.menge) s {q} GROUP BY p.name ORDER BY n DESC LIMIT 8", a).fetchall()
    kat = c.execute(f"SELECT p.kat, SUM(p.preis*p.menge) s {q} GROUP BY p.kat ORDER BY s DESC", a).fetchall()
    tage = {}
    for t in c.execute(f"SELECT v.zeit, p.preis*p.menge s {q}", a):
        tag = datetime.fromtimestamp(t["zeit"] / 1000, TZ).strftime("%Y-%m-%d")
        tage[tag] = tage.get(tag, 0) + t["s"]
    pro_shop = c.execute(f"SELECT v.shop_id, SUM(p.preis*p.menge) s, COUNT(DISTINCT v.id) n {q} GROUP BY v.shop_id", a).fetchall()
    lq = f"FROM lagerbuch WHERE shop_id IN {_in(shop_ids)} AND zeit >= ? AND zeit < ?"
    schwund = c.execute(f"SELECT COALESCE(-SUM(menge),0) n, COALESCE(-SUM(menge*COALESCE(ek,0)),0) w {lq} "
                        f"AND art='Inventur' AND menge < 0", a).fetchone()
    mehr = c.execute(f"SELECT COALESCE(SUM(menge),0) n, COALESCE(SUM(menge*COALESCE(ek,0)),0) w {lq} "
                     f"AND art='Inventur' AND menge > 0", a).fetchone()
    eingang = c.execute(f"SELECT COALESCE(SUM(menge),0) n, COALESCE(SUM(menge*COALESCE(ek,0)),0) w {lq} "
                        f"AND art='Eingang'", a).fetchone()
    lager = c.execute(f"SELECT COALESCE(SUM(MAX(b.menge,0)+MAX(b.lager,0)),0) n, "
                      f"COALESCE(SUM((MAX(b.menge,0)+MAX(b.lager,0))*COALESCE(p.ek,0)),0) w FROM bestand b "
                      f"JOIN produkte p ON p.id=b.produkt_id WHERE p.geloescht=0 AND b.gefuehrt=1 "
                      f"AND b.shop_id IN {_in(shop_ids)}").fetchone()
    konten = c.execute("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN guthaben<0 THEN guthaben END),0) minus, "
                       "COALESCE(SUM(CASE WHEN guthaben>0 THEN guthaben END),0) plus FROM konten WHERE entfernt=0").fetchone()
    return {"umsatz": r["umsatz"] / 100, "stueck": r["stueck"], "verkaeufe": r["anzahl"],
            "wareneinsatz": r["einsatz"] / 100, "rohertrag": r["roh"] / 100, "ohneEk": r["ohne_ek"] / 100,
            "schwund": {"stueck": schwund["n"], "wert": schwund["w"] / 100},
            "mehrbestand": {"stueck": mehr["n"], "wert": mehr["w"] / 100},
            "eingang": {"stueck": eingang["n"], "wert": eingang["w"] / 100},
            "lager": {"stueck": lager["n"], "wert": lager["w"] / 100},
            "top": [[x["name"], x["n"], x["s"] / 100] for x in top],
            "kat": [[x["kat"], x["s"] / 100] for x in kat],
            "tage": sorted([[k_, v_ / 100] for k_, v_ in tage.items()]),
            "proShop": {str(x["shop_id"]): {"umsatz": x["s"] / 100, "verkaeufe": x["n"]} for x in pro_shop},
            "konten": konten["n"], "schulden": konten["minus"] / 100, "guthaben": konten["plus"] / 100,
            "kasse": kassenstand(c) / 100}


TABELLEN_NAME = {"einstellungen": "Einstellungen", "konten": "Mitglieder", "shops": "Shops",
                 "shop_team": "Zuordnung Shopverantwortliche", "produkte": "Produkte (Katalog)", "bestand": "Bestand je Shop",
                 "verkaeufe": "Verkäufe", "posten": "Verkaufte Artikel", "kassenbuch": "Kontobuchungen der Mitglieder",
                 "kasse": "Kassenbuch (Bargeld)", "kassenpruefungen": "Kassenprüfungen",
                 "lagerbuch": "Lagerbuch (Wareneingang, Inventur)", "journal": "Protokoll"}


def datenbank_info():
    c = db()
    tabs = []
    for t in TABELLEN:
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        tabs.append([TABELLEN_NAME.get(t, t), n, t])
    groesse = 0
    for suffix in ("", "-wal"):
        try:
            groesse += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    letzte = c.execute("SELECT MAX(zeit) FROM journal").fetchone()[0]
    return {"pfad": DB_PATH, "tabellen": tabs, "groesse": groesse, "gespeichert": letzte}


OEFFENTLICHE_EINSTELLUNGEN = ("shopName", "limit", "autoLogout", "autoLogoutTeam", "minBestand", "adresse", "stil",
                              "warnGuthaben", "datenschutzKontakt")


@app.get("/api/state")
@api()
def state(k):
    c = db()
    e = einstellungen()
    team_map = {}
    for r in c.execute("SELECT shop_id, konto_id FROM shop_team"):
        team_map.setdefault(r["shop_id"], []).append(r["konto_id"])
    verwaltung = eff_rolle(k) in TEAM
    shops = [{"id": r["id"], "name": r["name"], "ort": r["ort"], "aktiv": bool(r["aktiv"]),
              "team": team_map.get(r["id"], []) if verwaltung else []}
             for r in alle_shops(c) if r["aktiv"] or verwaltung]
    bm = bestand_map(c)
    produkte = []
    for p in c.execute("SELECT * FROM produkte WHERE geloescht=0 ORDER BY sort, id"):
        if verwaltung or bm.get(p["id"]):
            produkte.append(produkt_public(p, bm, verwaltung))
    if not (verwaltung and k["rolle"] == "inhaber"):
        e = {key: v for key, v in e.items() if key in OEFFENTLICHE_EINSTELLUNGEN}
    out = {"user": konto_public(k, True), "einstellungen": e, "produkte": produkte, "shops": shops,
           "letzterEinkauf": letzter_einkauf(c, k["id"])}
    if verwaltung:
        out["meineShops"] = erlaubte_shops(k)
        konten = []
        for x in c.execute("SELECT * FROM konten WHERE entfernt=0 ORDER BY nachname COLLATE NOCASE, vorname COLLATE NOCASE"):
            d = konto_public(x)
            d["shops"] = [sid for sid, ids in team_map.items() if x["id"] in ids]
            konten.append(d)
        out["konten"] = konten
        out["daten"] = datenbank_info()
        out["kasse"] = kassenstand(c) / 100
    return jsonify(out)


def letzter_einkauf(c, kid):
    """{shop_id: [{pid, menge}]} – der jeweils letzte Einkauf je Shop (ohne stornierte Posten)."""
    out = {}
    for v in c.execute("SELECT v.id, v.shop_id FROM verkaeufe v WHERE v.konto_id=? AND v.storniert=0 AND v.id = "
                       "(SELECT MAX(id) FROM verkaeufe v2 WHERE v2.konto_id=v.konto_id AND v2.shop_id=v.shop_id AND v2.storniert=0)",
                       (kid,)).fetchall():
        posten = [{"pid": p["pid"], "menge": p["menge"]} for p in c.execute(
            "SELECT pid, SUM(menge) menge FROM posten WHERE verkauf_id=? AND storno=0 AND pid IS NOT NULL GROUP BY pid ORDER BY MIN(id)",
            (v["id"],))]
        if posten:
            out[str(v["shop_id"])] = posten
    return out


@app.get("/api/kennzahlen")
@api(TEAM)
def api_kennzahlen(u):
    von, bis = _zeitraum()
    return jsonify(kennzahlen(_shops_aus_anfrage(u), von, bis))


@app.get("/api/verkaeufe")
@api(TEAM)
def api_verkaeufe(u):
    """Die neuesten VERKAEUFE_LISTE Einkäufe im Zeitraum + Anzahl und Summe über ALLE Einkäufe im Zeitraum.
    Die Summen rechnet der Server, damit sie auch stimmen, wenn die Liste gekürzt ist."""
    von, bis = _zeitraum()
    ids = _shops_aus_anfrage(u)
    where = f"WHERE shop_id IN {_in(ids)} AND zeit >= ? AND zeit < ?"
    liste = verkaeufe_liste(where, (von, bis), VERKAEUFE_LISTE)
    g_ = db().execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(summe),0) s, "
        f"COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM posten p WHERE p.verkauf_id=v.id AND p.storno=1) THEN 1 END),0) ns, "
        f"COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM posten p WHERE p.verkauf_id=v.id AND p.storno=1) THEN summe END),0) ss "
        f"FROM verkaeufe v {where}", (von, bis)).fetchone()
    return jsonify(verkaeufe=liste, anzahl=g_["n"], summe=g_["s"] / 100,
                   mitStorno=g_["ns"], summeMitStorno=g_["ss"] / 100, limit=VERKAEUFE_LISTE)


VERKAEUFE_LISTE = 2000


@app.get("/api/journal")
@api(TEAM)
def api_journal(_u):
    von, bis = _zeitraum()
    return jsonify([dict(r) for r in db().execute(
        "SELECT * FROM journal WHERE zeit >= ? AND zeit < ? ORDER BY id DESC LIMIT 3000", (von, bis))])


@app.get("/api/lagerbuch")
@api(TEAM)
def api_lagerbuch(u):
    von, bis = _zeitraum()
    ids = _shops_aus_anfrage(u)
    q, a = f"SELECT * FROM lagerbuch WHERE shop_id IN {_in(ids)} AND zeit >= ? AND zeit < ?", [von, bis]
    if request.args.get("produkt", "").isdigit():
        q += " AND produkt_id=?"
        a.append(int(request.args["produkt"]))
    rows = db().execute(q + " ORDER BY id DESC LIMIT 2000", a).fetchall()
    return jsonify([{**dict(r), "ek": r["ek"] / 100 if r["ek"] is not None else None} for r in rows])


@app.get("/api/bestellvorschlag")
@api(TEAM)
def api_bestellvorschlag(u):
    """Was muss nachbestellt werden? Aus dem Verbrauch der letzten `basis` Tage wird der Bedarf für die nächsten
    `tage` Tage geschätzt, plus die Nachfüll-Grenze als Puffer, abzüglich Kühlschrank + Lager."""
    try:
        tage = max(1, min(90, int(request.args.get("tage") or 14)))
        basis = max(7, min(180, int(request.args.get("basis") or 28)))
    except ValueError:
        raise Fehler("Ungültige Angabe.")
    ids = _shops_aus_anfrage(u)
    c = db()
    puffer = einstellungen()["minBestand"]
    seit = ms() - basis * 86_400_000
    verkauft = {(r["shop_id"], r["pid"]): r["n"] for r in c.execute(
        f"SELECT v.shop_id, p.pid, SUM(p.menge) n FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
        f"WHERE p.storno=0 AND v.zeit >= ? AND v.shop_id IN {_in(ids)} GROUP BY v.shop_id, p.pid", (seit,))}
    namen = {r["id"]: r["name"] for r in alle_shops(c)}
    zeilen = {}
    for r in c.execute(f"SELECT b.shop_id, b.menge, b.lager, p.id, p.name, p.kat, p.ek FROM bestand b "
                       f"JOIN produkte p ON p.id=b.produkt_id WHERE b.gefuehrt=1 AND p.geloescht=0 "
                       f"AND b.shop_id IN {_in(ids)} ORDER BY p.kat, p.name"):
        n = verkauft.get((r["shop_id"], r["id"]), 0)
        pro_tag = n / basis
        vorrat = max(0, r["menge"]) + max(0, r["lager"])
        bedarf = max(0, math.ceil(pro_tag * tage) + (puffer if n else 0) - vorrat) if n else 0
        z = zeilen.setdefault(r["id"], {"pid": r["id"], "name": r["name"], "kat": r["kat"],
                                        "ek": r["ek"] / 100 if r["ek"] is not None else None, "shops": [], "bedarf": 0})
        z["shops"].append({"shop": r["shop_id"], "shopName": namen.get(r["shop_id"], "?"), "verkauft": n,
                           "proWoche": round(pro_tag * 7, 1), "vorrat": vorrat, "kuehlschrank": r["menge"], "lager": r["lager"],
                           "reichweite": round(vorrat / pro_tag) if pro_tag else None, "bedarf": bedarf})
        z["bedarf"] += bedarf
    liste = sorted((z for z in zeilen.values() if z["bedarf"] > 0), key=lambda z: (z["kat"], z["name"]))
    kosten = sum(z["bedarf"] * z["ek"] for z in liste if z["ek"] is not None)
    return jsonify(tage=tage, basis=basis, puffer=puffer, shops=[{"id": i, "name": namen.get(i, "?")} for i in ids],
                   produkte=liste, kosten=round(kosten, 2), ohneEk=sum(1 for z in liste if z["ek"] is None))


@app.get("/api/abschluss")
@api(TEAM)
def api_abschluss(u):
    """Monats-/Jahresabschluss: Kasse, Verkäufe, Lager und Mitgliederkonten für einen Zeitraum – zum Ausdrucken."""
    von, bis = _zeitraum()
    ids = _shops_aus_anfrage(u)
    c = db()
    namen = {r["id"]: r["name"] for r in alle_shops(c)}
    zr = (von, bis)
    # Kasse (gemeinsam für alle Shops)
    anfang = c.execute("SELECT COALESCE(SUM(betrag),0) FROM kasse WHERE zeit < ?", (von,)).fetchone()[0]
    ende = c.execute("SELECT COALESCE(SUM(betrag),0) FROM kasse WHERE zeit < ?", (bis,)).fetchone()[0]
    kasse_arten = [{"art": r["art"], "kategorie": r["kategorie"], "anzahl": r["n"], "summe": r["s"] / 100} for r in c.execute(
        "SELECT art, kategorie, COUNT(*) n, SUM(betrag) s FROM kasse WHERE zeit >= ? AND zeit < ? GROUP BY art, kategorie "
        "ORDER BY SUM(betrag) DESC", zr)]
    pruefungen = [_pruefung_public(r) for r in c.execute(
        "SELECT * FROM kassenpruefungen WHERE zeit >= ? AND zeit < ? ORDER BY zeit", zr)]
    # Verkäufe je Shop
    vq = f"FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id WHERE v.shop_id IN {_in(ids)} AND v.zeit >= ? AND v.zeit < ?"
    je_shop = []
    for r in c.execute(f"SELECT v.shop_id, COUNT(DISTINCT CASE WHEN p.storno=0 THEN v.id END) n, "
                       f"COALESCE(SUM(CASE WHEN p.storno=0 THEN p.menge END),0) stueck, "
                       f"COALESCE(SUM(CASE WHEN p.storno=0 THEN p.preis*p.menge END),0) umsatz, "
                       f"COALESCE(SUM(CASE WHEN p.storno=1 THEN p.preis*p.menge END),0) storno, "
                       f"COALESCE(SUM(CASE WHEN p.storno=0 AND p.ek IS NOT NULL THEN p.ek*p.menge END),0) einsatz, "
                       f"COALESCE(SUM(CASE WHEN p.storno=0 AND p.ek IS NOT NULL THEN (p.preis-p.ek)*p.menge END),0) roh "
                       f"{vq} GROUP BY v.shop_id ORDER BY v.shop_id", zr):
        je_shop.append({"shop": r["shop_id"], "shopName": namen.get(r["shop_id"], "?"), "einkaeufe": r["n"], "stueck": r["stueck"],
                        "umsatz": r["umsatz"] / 100, "storno": r["storno"] / 100, "wareneinsatz": r["einsatz"] / 100,
                        "rohertrag": r["roh"] / 100})
    top = [[r["name"], r["n"], r["s"] / 100] for r in c.execute(
        f"SELECT p.name, SUM(p.menge) n, SUM(p.preis*p.menge) s {vq} AND p.storno=0 GROUP BY p.name ORDER BY n DESC LIMIT 10", zr)]
    kat = [[r["kat"], r["s"] / 100] for r in c.execute(
        f"SELECT p.kat, SUM(p.preis*p.menge) s {vq} AND p.storno=0 GROUP BY p.kat ORDER BY s DESC", zr)]
    # Lager je Shop
    lq = f"FROM lagerbuch WHERE shop_id IN {_in(ids)} AND zeit >= ? AND zeit < ?"
    lager = {}
    for r in c.execute(f"SELECT shop_id, art, SUM(CASE WHEN menge>0 THEN menge ELSE 0 END) plus, "
                       f"SUM(CASE WHEN menge<0 THEN -menge ELSE 0 END) minus, "
                       f"SUM(CASE WHEN menge>0 THEN menge*COALESCE(ek,0) ELSE 0 END) wplus, "
                       f"SUM(CASE WHEN menge<0 THEN -menge*COALESCE(ek,0) ELSE 0 END) wminus "
                       f"{lq} AND art IN ('Eingang','Inventur') GROUP BY shop_id, art", zr):
        z = lager.setdefault(r["shop_id"], {"shop": r["shop_id"], "shopName": namen.get(r["shop_id"], "?"),
                                            "eingang": 0, "eingangWert": 0, "schwund": 0, "schwundWert": 0,
                                            "mehr": 0, "mehrWert": 0})
        if r["art"] == "Eingang":
            z["eingang"] += r["plus"]; z["eingangWert"] += r["wplus"] / 100
        else:
            z["schwund"] += r["minus"]; z["schwundWert"] += r["wminus"] / 100
            z["mehr"] += r["plus"]; z["mehrWert"] += r["wplus"] / 100
    warenwert = c.execute(f"SELECT COALESCE(SUM((MAX(b.menge,0)+MAX(b.lager,0))*COALESCE(p.ek,0)),0) FROM bestand b "
                          f"JOIN produkte p ON p.id=b.produkt_id WHERE p.geloescht=0 AND b.gefuehrt=1 "
                          f"AND b.shop_id IN {_in(ids)}").fetchone()[0]
    # Mitgliederkonten zum Stichtag: vom heutigen Guthaben aus zurückgerechnet –
    # alles, was NACH dem Stichtag gebucht oder gekauft wurde, wird wieder herausgerechnet.
    # (So stimmt es auch bei Datenbanken aus älteren Versionen, in denen nicht jede Buchung im Verlauf steht.)
    stand = {r["id"]: r["guthaben"] for r in c.execute("SELECT id, guthaben FROM konten")}
    for r in c.execute("SELECT konto_id, SUM(betrag) s FROM kassenbuch WHERE zeit >= ? GROUP BY konto_id", (bis,)):
        stand[r["konto_id"]] = stand.get(r["konto_id"], 0) - r["s"]
    for r in c.execute("SELECT v.konto_id, SUM(p.preis*p.menge) s FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                       "WHERE v.zeit >= ? GROUP BY v.konto_id", (bis,)):
        stand[r["konto_id"]] = stand.get(r["konto_id"], 0) + r["s"]
    info = {r["id"]: r for r in c.execute("SELECT id, name, nummer, entfernt FROM konten")}
    plus = sum(v for v in stand.values() if v > 0)
    minus = sum(v for v in stand.values() if v < 0)
    schulden = sorted(([info[k]["name"], info[k]["nummer"] if not info[k]["entfernt"] else "gelöscht", v / 100]
                       for k, v in stand.items() if v < 0 and k in info), key=lambda x: x[2])
    kb = {r["art"]: r["s"] / 100 for r in c.execute(
        "SELECT art, SUM(betrag) s FROM kassenbuch WHERE zeit >= ? AND zeit < ? GROUP BY art", zr)}
    return jsonify(von=von, bis=bis, erstellt=ms(), von_wem=u["name"], shopName=einstellungen()["shopName"],
                   shops=[namen.get(i, "?") for i in ids], alleShops=len(ids) == len(namen),
                   kasse={"anfang": anfang / 100, "ende": ende / 100, "arten": kasse_arten, "pruefungen": pruefungen},
                   verkauf={"jeShop": je_shop, "top": top, "kategorien": kat},
                   lager={"jeShop": sorted(lager.values(), key=lambda z: z["shop"]), "warenwertJetzt": warenwert / 100},
                   konten={"guthaben": plus / 100, "schulden": minus / 100, "netto": (plus + minus) / 100,
                           "mitSchulden": schulden, "buchungen": kb,
                           "kasseOhneGuthaben": (ende - plus - minus) / 100})


# ------------------------------------------------------------------ Shop
@app.post("/api/kaufen")
@api()
def kaufen(k):
    d = body()
    wunsch = {}
    for p in d.get("posten") or []:
        try:
            pid, menge = int(p["pid"]), int(p["menge"])
        except (KeyError, TypeError, ValueError):
            raise Fehler("Ungültiger Warenkorb.")
        if menge < 1 or menge > 99:
            raise Fehler("Ungültige Menge.")
        wunsch[pid] = wunsch.get(pid, 0) + menge
    if not wunsch:
        raise Fehler("Der Warenkorb ist leer.")
    min_b = einstellungen()["minBestand"]
    knapp = []
    with tx() as c:
        shop = _get_shop(c, d.get("shop"), aktiv=True)
        konto = c.execute("SELECT * FROM konten WHERE id=?", (k["id"],)).fetchone()
        limit = cents(einstellungen()["limit"])
        zeilen, summe = [], 0
        for pid, menge in wunsch.items():
            p = c.execute("SELECT * FROM produkte WHERE id=? AND geloescht=0", (pid,)).fetchone()
            if not p:
                raise Fehler("Ein Produkt gibt es nicht mehr – bitte neu laden.", 409)
            b = c.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=? AND gefuehrt=1",
                          (pid, shop["id"])).fetchone()
            if not b:
                raise Fehler(f"{p['name']} gibt es im Shop {shop['name']} nicht – bitte neu laden.", 409)
            if b["menge"] < menge:
                raise Fehler(f"{p['name']}: nur noch {b['menge']} im Kühlschrank.", 409)
            zeilen.append((p, menge, b["menge"], b["lager"]))
            summe += p["preis"] * menge
        danach = konto["guthaben"] - summe
        if danach < -limit:
            raise Fehler(f"Dein Konto würde auf {eur_txt(danach)} fallen. Erlaubt sind höchstens {eur_txt(-limit)}.", 409)
        vid = c.execute("INSERT INTO verkaeufe(konto_id, name, zeit, summe, shop_id) VALUES (?,?,?,?,?)",
                        (k["id"], k["name"], ms(), summe, shop["id"])).lastrowid
        for p, menge, vorher, im_lager in zeilen:
            c.execute("INSERT INTO posten(verkauf_id, pid, name, kat, preis, menge, ek) VALUES (?,?,?,?,?,?,?)",
                      (vid, p["id"], p["name"], p["kat"], p["preis"], menge, p["ek"]))
            c.execute("UPDATE bestand SET menge = menge - ? WHERE produkt_id=? AND shop_id=?", (menge, p["id"], shop["id"]))
            if vorher > min_b >= vorher - menge:
                knapp.append((p["name"], vorher - menge, im_lager))
        c.execute("UPDATE konten SET guthaben=? WHERE id=?", (danach, k["id"]))
    if knapp:
        nachfuellen_melden(shop, knapp)
    return jsonify(beleg=beleg(vid), summe=summe / 100, guthaben=danach / 100, shop=shop["name"])


@app.get("/api/mein-verlauf")
@api()
def mein_verlauf(k):
    return jsonify(verlauf(k["id"]))


def verlauf(kid):
    c = db()
    zeilen = [{"zeit": r["zeit"], "txt": r["art"] + (" · " + r["notiz"] if r["notiz"] else "") + " · " + r["von"],
               "betrag": r["betrag"] / 100}
              for r in c.execute("SELECT * FROM kassenbuch WHERE konto_id=? ORDER BY zeit DESC LIMIT 200", (kid,))]
    for v in verkaeufe_liste("WHERE konto_id=?", (kid,), 200):
        artikel = ", ".join(f"{p['menge']}× {p['name']}" for p in v["posten"] if not p["storno"])
        brutto = sum(p["preis"] * p["menge"] for p in v["posten"])
        zeilen.append({"zeit": v["zeit"], "txt": f"Einkauf {v['beleg']}" + (f" · {artikel}" if artikel else "")
                       + (" (storniert)" if v["storniert"] else ""), "betrag": -round(brutto, 2)})
    zeilen.sort(key=lambda z: -z["zeit"])
    return zeilen[:300]


# ------------------------------------------------------------------ Konten (Shopverantwortliche)
def _get_konto(c, kid):
    k = c.execute("SELECT * FROM konten WHERE id=? AND entfernt=0", (kid,)).fetchone()
    if not k:
        raise Fehler("Konto nicht gefunden.", 404)
    return k


def _namen(d, alt=None):
    """Vorname (freiwillig) und Nachname (Pflicht). Ältere Aufrufe mit nur "name" werden aufgeteilt."""
    if "nachname" in d or "vorname" in d:
        vor = " ".join(str(d.get("vorname", alt["vorname"] if alt else "") or "").split())[:60]
        nach = " ".join(str(d.get("nachname", alt["nachname"] if alt else "") or "").split())[:60]
    elif "name" in d:
        vor, nach = name_teilen(str(d.get("name", ""))[:120])
    else:
        vor, nach = (alt["vorname"], alt["nachname"]) if alt else ("", "")
    if not nach:
        raise Fehler("Bitte den Nachnamen eingeben.")
    return vor, nach, anzeigename(vor, nach)


def _name(d):
    return _namen(d)[2]


def _nummer(c, d, eigene_id=None):
    nr = str(d.get("nummer", "")).strip()
    if not NUM_RE.match(nr):
        raise Fehler("Bitte die Personalnummer eintragen (genau 8 Ziffern).")
    x = c.execute("SELECT id FROM konten WHERE nummer=?", (nr,)).fetchone()
    if x and x["id"] != eigene_id:
        raise Fehler("Diese Personalnummer ist schon vergeben.")
    return nr


def _erreichbarkeit(d, alt=None):
    alt = alt or {"tel": "", "raum": ""}
    tel = str(d.get("tel", alt["tel"]) or "").strip()[:40]
    raum = str(d.get("raum", alt["raum"]) or "").strip()[:80]
    if tel and tel != alt["tel"] and not re.fullmatch(r"\d{5}", tel):
        raise Fehler("Die interne Rufnummer hat genau 5 Ziffern.")
    return tel, raum


def _darf_bearbeiten(u, k):
    """Shopverantwortliche verwalten Mitglieder und sich selbst, der Administrator alle."""
    return u["rolle"] == "inhaber" or k["rolle"] == "kunde" or k["id"] == u["id"]


@app.post("/api/konten")
@api(TEAM)
def konto_neu(u):
    d = body()
    start = cents(d.get("start", 0) or 0)
    if not 0 <= start <= MAX_BUCHUNG:
        raise Fehler(f"Das Startguthaben muss zwischen 0 und {eur_txt(MAX_BUCHUNG)} liegen.")
    rolle = d.get("rolle") if d.get("rolle") in ROLLEN else "kunde"
    if rolle != "kunde" and u["rolle"] != "inhaber":
        raise Fehler("Shopverantwortliche und Administratoren legt nur ein Administrator an.", 403)
    tel, raum = _erreichbarkeit(d)
    with tx() as c:
        (vor, nach, name), nr = _namen(d), _nummer(c, d)
        pin = einmal_pin()
        pw = einmal_passwort() if rolle != "kunde" else None
        kid = c.execute("INSERT INTO konten(nummer, name, rolle, pw_hash, muss_aendern, pin_hash, pin_muss_aendern, "
                        "zurueckgesetzt_von, guthaben, angelegt, tel, raum) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
                        (nr, name, rolle, generate_password_hash(pw) if pw else "!", 1 if pw else 0,
                         generate_password_hash(pin), u["name"], start, ms(), tel, raum)).lastrowid
        c.execute("UPDATE konten SET vorname=?, nachname=? WHERE id=?", (vor, nach, kid))
        if start:
            c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                      (kid, ms(), "Aufladung", start, u["name"], "Startguthaben"))
            kasse_buchen(c, "Aufladung", start, u["name"], zweck=f"Startguthaben {name}", konto_id=kid)
        log(c, u["name"], "Mitglied angelegt", f"{name} ({nr}, {ROLLEN_NAME[rolle]}, Start {eur_txt(start)})")
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    return jsonify(konto=konto_public(k), einmalPin=pin, einmalPw=pw)


MAX_MASSEN = 500


@app.post("/api/konten/massen")
@api(TEAM)
def konten_massen(u):
    """Viele Mitglieder auf einmal anlegen (Rolle Mitglied). Alles oder nichts: Bei einem Fehler wird
    keines angelegt und die Antwort nennt jede fehlerhafte Zeile."""
    liste = body().get("konten")
    if not isinstance(liste, list) or not liste:
        raise Fehler("Keine Mitglieder übergeben.")
    if len(liste) > MAX_MASSEN:
        raise Fehler(f"Höchstens {MAX_MASSEN} Mitglieder auf einmal.")
    fehler, neu, nummern = [], [], set()
    with tx() as c:
        for i, d in enumerate(liste, 1):
            try:
                if not isinstance(d, dict):
                    raise Fehler("Ungültige Zeile.")
                vor, nach, name = _namen(d)
                nr = _nummer(c, d)
                if nr in nummern:
                    raise Fehler("Personalnummer kommt doppelt vor.")
                nummern.add(nr)
                tel, raum = _erreichbarkeit(d)
                start = cents(d.get("start", 0) or 0)
                if not 0 <= start <= MAX_BUCHUNG:
                    raise Fehler(f"Startguthaben muss zwischen 0 und {eur_txt(MAX_BUCHUNG)} liegen.")
                neu.append((vor, nach, name, nr, tel, raum, start))
            except Fehler as f:
                fehler.append({"zeile": i, "fehler": f.text})
        if fehler:
            raise MassenFehler(fehler)
        jetzt, erg, summe = ms(), [], 0
        for vor, nach, name, nr, tel, raum, start in neu:
            pin = einmal_pin()
            kid = c.execute("INSERT INTO konten(nummer, name, vorname, nachname, rolle, pw_hash, muss_aendern, pin_hash, "
                            "pin_muss_aendern, zurueckgesetzt_von, guthaben, angelegt, tel, raum) "
                            "VALUES (?,?,?,?,'kunde','!',0,?,1,?,?,?,?,?)",
                            (nr, name, vor, nach, generate_password_hash(pin), u["name"], start, jetzt, tel, raum)).lastrowid
            if start:
                c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                          (kid, jetzt, "Aufladung", start, u["name"], "Startguthaben"))
                kasse_buchen(c, "Aufladung", start, u["name"], zweck=f"Startguthaben {name}", konto_id=kid)
                summe += start
            erg.append({"konto": konto_public(c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()),
                        "einmalPin": pin})
        log(c, u["name"], "Mitglieder angelegt (Liste)", f"{len(erg)} Mitglieder"
            + (f" · Startguthaben gesamt {eur_txt(summe)}" if summe else ""))
    return jsonify(angelegt=erg)


class MassenFehler(Fehler):
    def __init__(self, zeilen):
        super().__init__(f"{len(zeilen)} Zeile(n) fehlerhaft – es wurde niemand angelegt.", 400)
        self.zeilen = zeilen


@app.errorhandler(MassenFehler)
def _massen_fehler(e):
    return jsonify(fehler=e.text, zeilen=e.zeilen), 400


@app.put("/api/konten/<int:kid>")
@api(TEAM)
def konto_aendern(u, kid):
    """Name, Personalnummer und/oder dienstliche Erreichbarkeit ändern (nur übergebene Felder)."""
    d = body()
    with tx() as c:
        k = _get_konto(c, kid)
        if not _darf_bearbeiten(u, k):
            raise Fehler("Shopverantwortliche und Administratoren bearbeitet nur ein Administrator.", 403)
        vor, nach, name = _namen(d, k)
        nr = _nummer(c, d, kid) if "nummer" in d else k["nummer"]
        tel, raum = _erreichbarkeit(d, k)
        c.execute("UPDATE konten SET name=?, vorname=?, nachname=?, nummer=?, tel=?, raum=? WHERE id=?",
                  (name, vor, nach, nr, tel, raum, kid))
        aend = []
        if name != k["name"]:
            aend.append(f"Name: {k['name']} → {name}")
        if nr != k["nummer"]:
            aend.append(f"Personalnummer: {k['nummer']} → {nr}")
        if (tel, raum) != (k["tel"], k["raum"]):
            aend.append("Erreichbarkeit geändert")
        if aend:
            log(c, u["name"], "Mitglied geändert", f"{name} · " + " · ".join(aend))
    return jsonify(ok=True)


@app.put("/api/konten/<int:kid>/rolle")
@api(INHABER)
def konto_rolle(u, kid):
    """Rolle ändern. Wer neu in die Verwaltung kommt, bekommt ein Einmalpasswort;
    wer herabgestuft wird, verliert das Passwort – PIN, Guthaben und Historie bleiben."""
    neu = body().get("rolle")
    if neu not in ROLLEN:
        raise Fehler("Unbekannte Rolle.")
    pw = None
    with tx() as c:
        k = _get_konto(c, kid)
        alt = k["rolle"]
        if alt == neu:
            return jsonify(ok=True, einmalPw=None)
        if kid == u["id"]:
            raise Fehler("Die eigene Rolle kann nicht geändert werden.")
        if alt == "inhaber" and c.execute(
                "SELECT COUNT(*) FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone()[0] <= 1:
            raise Fehler("Der letzte Administrator kann nicht herabgestuft werden.")
        if neu == "kunde":
            c.execute("UPDATE konten SET rolle='kunde', pw_hash='!', muss_aendern=0 WHERE id=?", (kid,))
            c.execute("DELETE FROM shop_team WHERE konto_id=?", (kid,))
        elif alt == "kunde":
            pw = einmal_passwort()
            c.execute("UPDATE konten SET rolle=?, pw_hash=?, muss_aendern=1, zurueckgesetzt_von=? WHERE id=?",
                      (neu, generate_password_hash(pw), u["name"], kid))
        else:
            c.execute("UPDATE konten SET rolle=? WHERE id=?", (neu, kid))
        sitzungen_beenden(c, kid)
        log(c, u["name"], "Rolle geändert", f"{k['name']} ({k['nummer']}): {ROLLEN_NAME[alt]} → {ROLLEN_NAME[neu]}")
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    return jsonify(ok=True, konto=konto_public(k), einmalPw=pw)


@app.delete("/api/konten/<int:kid>")
@api(TEAM)
def konto_loeschen(u, kid):
    """Konto löschen. Käufe und Buchungen bleiben für die Abrechnung erhalten (mit Namen),
    ein Restguthaben/-schuld wird ausgebucht. Die Personalnummer wird wieder frei."""
    with tx() as c:
        k = _get_konto(c, kid)
        if kid == u["id"]:
            raise Fehler("Du kannst dein eigenes Konto nicht löschen.")
        if not _darf_bearbeiten(u, k):
            raise Fehler("Shopverantwortliche und Administratoren löscht nur ein Administrator.", 403)
        if k["rolle"] == "inhaber" and c.execute(
                "SELECT COUNT(*) FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone()[0] <= 1:
            raise Fehler("Der letzte Administrator kann nicht gelöscht werden.")
        if k["guthaben"]:
            c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                      (kid, ms(), "Ausbuchung", -k["guthaben"], u["name"], "Konto gelöscht"))
        c.execute("DELETE FROM shop_team WHERE konto_id=?", (kid,))
        c.execute("UPDATE konten SET entfernt=1, guthaben=0, pw_hash='!', pin_hash=NULL, muss_aendern=0, "
                  "pin_muss_aendern=0, nummer=? WHERE id=?", (f"geloescht-{kid}-{k['nummer']}", kid))
        sitzungen_beenden(c, kid)
        log(c, u["name"], "Konto gelöscht", f"{k['name']} ({k['nummer']}), Saldo {eur_txt(k['guthaben'])} ausgebucht")
    return jsonify(ok=True)


@app.put("/api/konten/<int:kid>/stilllegen")
@api(TEAM)
def konto_stilllegen(u, kid):
    """Konto stilllegen (z. B. bei Versetzung) oder wieder aktivieren. Anders als Löschen bleibt alles erhalten:
    Personalnummer, PIN, Guthaben und Verlauf. Stillgelegte Konten können sich nicht anmelden.
    Optional wird ein Restguthaben dabei bar aus der Kasse ausgezahlt."""
    d = body()
    an = bool(d.get("an", True))
    with tx() as c:
        k = _get_konto(c, kid)
        if kid == u["id"]:
            raise Fehler("Das eigene Konto kann man nicht stilllegen.")
        if not _darf_bearbeiten(u, k):
            raise Fehler("Shopverantwortliche und Administratoren legt nur ein Administrator still.", 403)
        if an and k["rolle"] == "inhaber" and c.execute(
                "SELECT COUNT(*) FROM konten WHERE rolle='inhaber' AND entfernt=0 AND stillgelegt=0").fetchone()[0] <= 1:
            raise Fehler("Der letzte aktive Administrator kann nicht stillgelegt werden.")
        if bool(k["stillgelegt"]) == an:
            return jsonify(ok=True)
        extra = ""
        if an and d.get("auszahlen") and k["guthaben"] > 0:
            betrag = k["guthaben"]
            if kassenstand(c) < betrag:
                raise Fehler(f"In der Kasse sind nur {eur_txt(kassenstand(c))} – das Restguthaben von {eur_txt(betrag)} "
                             f"kann nicht bar ausgezahlt werden.", 409)
            kasse_buchen(c, "Auszahlung", -betrag, u["name"], zweck=f"{k['name']} · Restguthaben bei Stilllegung", konto_id=kid)
            c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                      (kid, ms(), "Auszahlung", -betrag, u["name"], "Restguthaben bei Stilllegung"))
            c.execute("UPDATE konten SET guthaben=0 WHERE id=?", (kid,))
            extra = f" · Restguthaben {eur_txt(betrag)} bar ausgezahlt"
        c.execute("UPDATE konten SET stillgelegt=? WHERE id=?", (ms() if an else 0, kid))
        if an:
            sitzungen_beenden(c, kid)
        log(c, u["name"], "Konto stillgelegt" if an else "Konto reaktiviert", f"{k['name']} ({k['nummer']})" + extra)
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    return jsonify(ok=True, konto=konto_public(k))


@app.post("/api/konten/<int:kid>/buchen")
@api(TEAM)
def konto_buchen(u, kid):
    d = body()
    auf = d.get("art") == "auf"
    betrag = cents(d.get("betrag"))
    if betrag <= 0:
        raise Fehler("Bitte einen Betrag über 0 eingeben.")
    if betrag > MAX_BUCHUNG:
        raise Fehler(f"Höchstens {eur_txt(MAX_BUCHUNG)} pro Buchung.")
    if kid == u["id"] and u["rolle"] != "inhaber":
        raise Fehler("Das eigene Guthaben bucht bitte jemand anderes (Vier-Augen-Prinzip).", 403)
    notiz = str(d.get("notiz", "")).strip()[:200]
    bar = auf or bool(d.get("bar", True))     # Aufladung = immer Bargeld; Abbuchung = Auszahlung aus der Kasse oder nur Korrektur
    with tx() as c:
        k = _get_konto(c, kid)
        wert = betrag if auf else -betrag
        neu = k["guthaben"] + wert
        if bar:
            if not auf and kassenstand(c) < betrag:
                raise Fehler(f"In der Kasse sind nur {eur_txt(kassenstand(c))} – so viel kann nicht ausgezahlt werden.", 409)
            kasse_buchen(c, "Aufladung" if auf else "Auszahlung", wert, u["name"],
                         zweck=k["name"] + (f" · {notiz}" if notiz else ""), konto_id=kid)
        c.execute("UPDATE konten SET guthaben=? WHERE id=?", (neu, kid))
        c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                  (kid, ms(), "Aufladung" if auf else ("Auszahlung" if bar else "Abbuchung"), wert, u["name"], notiz))
        log(c, u["name"], "Guthaben aufgeladen" if auf else "Guthaben abgebucht",
            f"{k['name']}: {eur_txt(wert)} → {eur_txt(neu)}")
    return jsonify(guthaben=neu / 100)


@app.post("/api/konten/<int:kid>/zuruecksetzen")
@api(TEAM)
def konto_reset(u, kid):
    """art=pin: neue Einmal-PIN (Einkauf). art=passwort: neues Einmalpasswort (Verwaltung, nur Administrator)."""
    art = "passwort" if body().get("art") == "passwort" else "pin"
    with tx() as c:
        k = _get_konto(c, kid)
        if k["rolle"] != "kunde" and u["rolle"] != "inhaber" and k["id"] != u["id"]:
            raise Fehler("Zugänge von Shopverantwortlichen setzt nur ein Administrator zurück.", 403)
        if art == "passwort":
            if k["rolle"] == "kunde":
                raise Fehler("Kunden haben kein Passwort, nur eine PIN.")
            if u["rolle"] != "inhaber":
                raise Fehler("Passwörter setzt nur ein Administrator zurück.", 403)
            geheim = einmal_passwort()
            c.execute("UPDATE konten SET pw_hash=?, muss_aendern=1, zurueckgesetzt_von=?, fehler=0, gesperrt_bis=0 "
                      "WHERE id=?", (generate_password_hash(geheim), u["name"], kid))
        else:
            geheim = einmal_pin()
            c.execute("UPDATE konten SET pin_hash=?, pin_muss_aendern=1, zurueckgesetzt_von=?, fehler=0, gesperrt_bis=0 "
                      "WHERE id=?", (generate_password_hash(geheim), u["name"], kid))
        sitzungen_beenden(c, kid, session.get("sid") if kid == u["id"] and art == "pin" else None)
        log(c, u["name"], "Passwort zurückgesetzt" if art == "passwort" else "PIN zurückgesetzt",
            f"{k['name']} ({k['nummer']})")
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    return jsonify(konto=konto_public(k), **({"einmalPw": geheim} if art == "passwort" else {"einmalPin": geheim}))


@app.get("/api/konten/<int:kid>/verlauf")
@api(TEAM)
def konto_verlauf(_u, kid):
    _get_konto(db(), kid)
    return jsonify(verlauf(kid))


# ------------------------------------------------------------------ Team (Administrator, ältere Schnittstelle)
@app.post("/api/team")
@api(INHABER)
def team_neu(u):
    d = body()
    rolle = d.get("rolle") if d.get("rolle") in TEAM else "personal"
    with tx() as c:
        name = _name(d)
        nr = str(d.get("nummer", "")).strip()
        vorhanden = c.execute("SELECT * FROM konten WHERE nummer=? AND entfernt=0", (nr,)).fetchone()
        pw = einmal_passwort()
        if vorhanden and vorhanden["rolle"] == "kunde":
            # Bestehendes Kundenkonto bekommt Verwaltungsrechte – PIN und Guthaben bleiben
            kid = vorhanden["id"]
            c.execute("UPDATE konten SET name=?, rolle=?, pw_hash=?, muss_aendern=1, zurueckgesetzt_von=? WHERE id=?",
                      (name, rolle, generate_password_hash(pw), u["name"], kid))
            pin = None
            log(c, u["name"], "Verwaltungsrechte vergeben", f"{name} · {ROLLEN_NAME[rolle]} · {nr}")
        else:
            nr = _nummer(c, d)
            pin = einmal_pin()
            kid = c.execute("INSERT INTO konten(nummer, name, rolle, pw_hash, muss_aendern, pin_hash, pin_muss_aendern, "
                            "zurueckgesetzt_von, angelegt) VALUES (?,?,?,?,1,?,1,?,?)",
                            (nr, name, rolle, generate_password_hash(pw), generate_password_hash(pin),
                             u["name"], ms())).lastrowid
            log(c, u["name"], "Zugang angelegt", f"{name} · {ROLLEN_NAME[rolle]} · {nr}")
        c.execute("UPDATE konten SET vorname=?, nachname=? WHERE id=?", (*name_teilen(name), kid))
        k = c.execute("SELECT * FROM konten WHERE id=?", (kid,)).fetchone()
    return jsonify(konto=konto_public(k), einmalPw=pw, einmalPin=pin)


@app.put("/api/team/<int:kid>")
@api(INHABER)
def team_aendern(u, kid):
    d = body()
    rolle = d.get("rolle") if d.get("rolle") in TEAM else "personal"
    hinweis = None
    with tx() as c:
        k = _get_konto(c, kid)
        if k["rolle"] == "kunde":
            raise Fehler("Das ist ein Kundenkonto.")
        name, nr = _name(d), _nummer(c, d, kid)
        if kid == u["id"] and rolle != "inhaber":
            rolle, hinweis = "inhaber", "Die eigene Administrator-Rolle kann nicht abgegeben werden."
        c.execute("UPDATE konten SET name=?, nummer=?, rolle=?, vorname=?, nachname=? WHERE id=?",
                  (name, nr, rolle, *name_teilen(name), kid))
        log(c, u["name"], "Zugang geändert", f"{name} · {ROLLEN_NAME[rolle]} · {nr}")
    return jsonify(ok=True, hinweis=hinweis)


@app.delete("/api/team/<int:kid>")
@api(INHABER)
def team_entfernen(u, kid):
    with tx() as c:
        k = _get_konto(c, kid)
        if k["rolle"] == "kunde":
            raise Fehler("Das ist ein Kundenkonto.")
        if kid == u["id"]:
            raise Fehler("Du kannst dich nicht selbst entfernen.")
        if k["rolle"] == "inhaber" and c.execute(
                "SELECT COUNT(*) FROM konten WHERE rolle='inhaber' AND entfernt=0").fetchone()[0] <= 1:
            raise Fehler("Der letzte Administrator kann nicht entfernt werden.")
        # Konto bleibt als Kundenkonto bestehen (PIN, Guthaben, Historie) – nur die Verwaltung ist weg
        c.execute("UPDATE konten SET rolle='kunde', pw_hash='!', muss_aendern=0 WHERE id=?", (kid,))
        log(c, u["name"], "Verwaltungsrechte entzogen", f"{k['name']} · {k['nummer']} · bleibt als Kundenkonto")
    return jsonify(ok=True)


# ------------------------------------------------------------------ Storno
@app.post("/api/storno")
@api(TEAM)
def storno(u):
    d = body()
    try:
        posten_id = int(d.get("posten"))
    except (TypeError, ValueError):
        raise Fehler("Ungültiger Posten.")
    with tx() as c:
        p = c.execute("SELECT * FROM posten WHERE id=?", (posten_id,)).fetchone()
        if not p or p["storno"]:
            raise Fehler("Dieser Posten ist schon storniert.", 409)
        v = c.execute("SELECT * FROM verkaeufe WHERE id=?", (p["verkauf_id"],)).fetchone()
        darf_shop(u, v["shop_id"])
        betrag = p["preis"] * p["menge"]
        c.execute("UPDATE posten SET storno=1 WHERE id=?", (posten_id,))
        offen = c.execute("SELECT COUNT(*) FROM posten WHERE verkauf_id=? AND storno=0", (v["id"],)).fetchone()[0]
        c.execute("UPDATE verkaeufe SET summe=summe-?, storniert=? WHERE id=?", (betrag, 1 if offen == 0 else 0, v["id"]))
        c.execute("UPDATE konten SET guthaben=guthaben+? WHERE id=?", (betrag, v["konto_id"]))
        if p["pid"]:
            c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) VALUES (?,?,?,1) "
                      "ON CONFLICT(produkt_id, shop_id) DO UPDATE SET menge = menge + excluded.menge",
                      (p["pid"], v["shop_id"], p["menge"]))
        c.execute("INSERT INTO kassenbuch(konto_id, zeit, art, betrag, von, notiz) VALUES (?,?,?,?,?,?)",
                  (v["konto_id"], ms(), "Storno", betrag, u["name"], f"{p['name']} aus {beleg(v['id'])}"))
        log(c, u["name"], "Posten storniert", f"{beleg(v['id'])} · {p['name']} · {eur_txt(betrag)} an {v['name']}")
    return jsonify(betrag=betrag / 100)


# ------------------------------------------------------------------ Sortiment
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
BILD_TYPEN = ("image/jpeg", "image/png", "image/webp")
BILD_MAX_KANTE = 1024          # größere Fotos werden verkleinert
BILD_MAX_PIXEL = 40_000_000    # Schutz vor „Pixel-Bomben“ (winzige Datei, riesiges Bild)


def bild_pruefen(roh):
    """Prüft ein hochgeladenes Bild und erzeugt es NEU (Pillow): Nur echte JPEG/PNG/WebP werden angenommen,
    zu große werden verkleinert, Metadaten (z. B. GPS aus Handyfotos) fallen weg. Was immer in der Datei
    versteckt war – gespeichert und ausgeliefert wird nur das frisch erzeugte Bild.
    Gibt (bytes, mimetype) zurück oder wirft Fehler."""
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = BILD_MAX_PIXEL
    if not roh or len(roh) > MAX_BILD:
        raise Fehler("Das Foto ist zu groß." if roh else "Das Foto ist leer.")
    try:
        with Image.open(io.BytesIO(roh)) as probe:
            if probe.format not in ("JPEG", "PNG", "WEBP"):
                raise Fehler("Bitte ein Foto als JPG, PNG oder WebP verwenden.")
            probe.verify()                                  # erkennt kaputte/manipulierte Dateien
        with Image.open(io.BytesIO(roh)) as im:
            if im.width * im.height > BILD_MAX_PIXEL:
                raise Fehler("Das Foto hat zu viele Pixel.")
            im.load()
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((BILD_MAX_KANTE, BILD_MAX_KANTE))
            out = io.BytesIO()
            if alpha:
                im.save(out, "PNG", optimize=True)
                return out.getvalue(), "image/png"
            im.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Fehler:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Fehler("Das Foto hat zu viele Pixel.")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise Fehler("Das Foto konnte nicht gelesen werden.")
DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$")


def _produkt_felder(d):
    name = str(d.get("name", "")).strip()[:80]
    if not name:
        raise Fehler("Bitte einen Namen eingeben.")
    form = "riegel" if d.get("form") == "riegel" else "dose"
    try:
        ml = int(d.get("ml") or 0)
    except (TypeError, ValueError):
        raise Fehler("Ungültige Zahl.")
    preis = max(0, cents(d.get("preis", 0)))
    ek = None if d.get("ek") in (None, "") else max(0, cents(d.get("ek")))
    farbe = d.get("farbe") if HEX.match(str(d.get("farbe", ""))) else "#3A4250"
    farbe2 = d.get("farbe2") if HEX.match(str(d.get("farbe2", ""))) else "#1A1F28"
    kat = str(d.get("kat", "")).strip()[:40] or "Sonstiges"
    return dict(name=name, kat=kat, preis=preis, ek=ek, form=form, ml=ml, farbe=farbe, farbe2=farbe2)


def _bild(c, pid, d):
    """bild: None = unverändert, "" = entfernen, data:-URL = neues Foto."""
    b = d.get("bild")
    if b is None:
        return
    if b == "":
        c.execute("UPDATE produkte SET bild=NULL, bild_typ=NULL, bild_v=bild_v+1 WHERE id=?", (pid,))
        return
    m = DATA_URL.match(str(b))
    if not m:
        raise Fehler("Das Foto konnte nicht gelesen werden.")
    try:
        roh = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        raise Fehler("Das Foto konnte nicht gelesen werden.")
    bild_neu, typ = bild_pruefen(roh)
    c.execute("UPDATE produkte SET bild=?, bild_typ=?, bild_v=bild_v+1 WHERE id=?", (bild_neu, typ, pid))


def _fuehren_setzen(c, u, pid, wunsch):
    """wunsch: Liste von Shop-IDs, in denen das Produkt geführt werden soll. Nur erlaubte Shops werden angefasst."""
    erlaubt = erlaubte_shops(u)
    wunsch = {int(s) for s in wunsch or [] if str(s).isdigit()}
    namen = {r["id"]: r["name"] for r in alle_shops(c)}
    an, aus = [], []
    for sid in erlaubt:
        alt = c.execute("SELECT gefuehrt FROM bestand WHERE produkt_id=? AND shop_id=?", (pid, sid)).fetchone()
        soll = 1 if sid in wunsch else 0
        if alt is None:
            if soll:
                c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) VALUES (?,?,0,1)", (pid, sid))
                an.append(namen.get(sid, "?"))
        elif alt["gefuehrt"] != soll:
            c.execute("UPDATE bestand SET gefuehrt=? WHERE produkt_id=? AND shop_id=?", (soll, pid, sid))
            (an if soll else aus).append(namen.get(sid, "?"))
    return an, aus


def _get_produkt(c, pid):
    p = c.execute("SELECT * FROM produkte WHERE id=? AND geloescht=0", (pid,)).fetchone()
    if not p:
        raise Fehler("Produkt nicht gefunden – bitte neu laden.", 404)
    return p


def _ort(d, key="ort", standard="lager"):
    o = d.get(key) or standard
    if o not in ORTE:
        raise Fehler("Unbekannter Lagerort.")
    return o


def _eingang_buchen(c, u, p, shop, menge, ek, ort="lager"):
    sp = "lager" if ort == "lager" else "menge"
    c.execute(f"INSERT INTO bestand(produkt_id, shop_id, {sp}, gefuehrt) VALUES (?,?,?,1) "
              f"ON CONFLICT(produkt_id, shop_id) DO UPDATE SET {sp} = {sp} + excluded.{sp}, gefuehrt = 1",
              (p["id"], shop["id"], menge))
    if ek is not None:
        c.execute("UPDATE produkte SET ek=? WHERE id=?", (ek, p["id"]))
    c.execute("INSERT INTO lagerbuch(zeit, shop_id, produkt_id, name, art, menge, ek, von, ort) VALUES (?,?,?,?,?,?,?,?,?)",
              (ms(), shop["id"], p["id"], p["name"], "Eingang", menge, ek if ek is not None else p["ek"], u["name"], ort))


@app.post("/api/produkte")
@api(TEAM)
def produkt_neu(u):
    d = body()
    f = _produkt_felder(d)
    try:
        anfang = max(0, min(9999, int(d.get("anfang") or 0)))
    except (TypeError, ValueError):
        raise Fehler("Ungültiger Anfangsbestand.")
    with tx() as c:
        if c.execute("SELECT 1 FROM produkte WHERE geloescht=0 AND lower(name)=lower(?)", (f["name"],)).fetchone():
            raise Fehler("Ein Produkt mit diesem Namen gibt es schon im Katalog. "
                         "Du kannst es unter Sortiment in deinem Shop führen.", 409)
        sort = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM produkte").fetchone()[0]
        pid = c.execute("INSERT INTO produkte(name, kat, preis, ek, bestand, form, ml, farbe, farbe2, sort) "
                        "VALUES (:name,:kat,:preis,:ek,0,:form,:ml,:farbe,:farbe2,:sort)",
                        {**f, "sort": sort}).lastrowid
        _bild(c, pid, d)
        an, _ = _fuehren_setzen(c, u, pid, d.get("fuehren") or [])
        extra = ""
        if anfang and d.get("shop"):
            shop = _get_shop(c, d.get("shop"))
            darf_shop(u, shop["id"])
            ort = _ort(d, "anfangOrt")
            _eingang_buchen(c, u, _get_produkt(c, pid), shop, anfang, f["ek"], ort)
            extra = f" · Anfangsbestand {anfang} in {shop['name']} ({ORTE[ort]})"
        log(c, u["name"], "Produkt angelegt", f"{f['name']} · {eur_txt(f['preis'])}"
            + (f" · geführt in {', '.join(an)}" if an else "") + extra)
    return jsonify(id=pid)


@app.put("/api/produkte/<int:pid>")
@api(TEAM)
def produkt_aendern(u, pid):
    d = body()
    f = _produkt_felder(d)
    with tx() as c:
        alt = _get_produkt(c, pid)
        if c.execute("SELECT 1 FROM produkte WHERE geloescht=0 AND lower(name)=lower(?) AND id!=?", (f["name"], pid)).fetchone():
            raise Fehler("Ein anderes Produkt heißt schon so.", 409)
        c.execute("UPDATE produkte SET name=:name, kat=:kat, preis=:preis, ek=:ek, form=:form, ml=:ml, "
                  "farbe=:farbe, farbe2=:farbe2 WHERE id=:id", {**f, "id": pid})
        _bild(c, pid, d)
        aend = []
        if alt["name"] != f["name"]:
            aend.append(f"Name: {alt['name']} → {f['name']}")
        if alt["kat"] != f["kat"]:
            aend.append(f"Kategorie: {alt['kat']} → {f['kat']}")
        if alt["preis"] != f["preis"]:
            aend.append(f"Preis: {eur_txt(alt['preis'])} → {eur_txt(f['preis'])}")
        if alt["ek"] != f["ek"]:
            aend.append(f"Einkaufspreis: {eur_txt(alt['ek']) if alt['ek'] is not None else '–'} → "
                        f"{eur_txt(f['ek']) if f['ek'] is not None else '–'}")
        if d.get("bild") is not None:
            aend.append("Foto entfernt" if d.get("bild") == "" else "Foto geändert")
        if "fuehren" in d:
            an, aus = _fuehren_setzen(c, u, pid, d.get("fuehren"))
            if an:
                aend.append(f"jetzt geführt in {', '.join(an)}")
            if aus:
                aend.append(f"nicht mehr geführt in {', '.join(aus)}")
        if aend:
            log(c, u["name"], "Produkt geändert", f"{f['name']} · " + " · ".join(aend))
    return jsonify(ok=True)


@app.put("/api/produkte/<int:pid>/fuehren")
@api(TEAM)
def produkt_fuehren(u, pid):
    d = body()
    with tx() as c:
        p = _get_produkt(c, pid)
        shop = _get_shop(c, d.get("shop"))
        darf_shop(u, shop["id"])
        an = bool(d.get("an"))
        c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) VALUES (?,?,0,?) "
                  "ON CONFLICT(produkt_id, shop_id) DO UPDATE SET gefuehrt = excluded.gefuehrt", (pid, shop["id"], int(an)))
        log(c, u["name"], "Sortiment geändert", f"{shop['name']} · {p['name']} · " + ("wird geführt" if an else "nicht mehr geführt"))
    return jsonify(ok=True)


@app.post("/api/produkte/<int:pid>/eingang")
@api(TEAM)
def produkt_eingang(u, pid):
    d = body()
    try:
        menge = int(d.get("menge"))
    except (TypeError, ValueError):
        raise Fehler("Bitte eine Menge eingeben.")
    if menge < 1 or menge > 9999:
        raise Fehler("Die Menge muss zwischen 1 und 9999 liegen.")
    ek = None if d.get("ek") in (None, "") else max(0, cents(d.get("ek")))
    with tx() as c:
        p = _get_produkt(c, pid)
        shop = _get_shop(c, d.get("shop"))
        darf_shop(u, shop["id"])
        ort = _ort(d)
        _eingang_buchen(c, u, p, shop, menge, ek, ort)
        b = c.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=?", (pid, shop["id"])).fetchone()
        log(c, u["name"], "Wareneingang", f"{shop['name']} · {menge}× {p['name']} ins {ORTE[ort]}"
            + (f" · EK {eur_txt(ek)}/Stück" if ek is not None else "") + f" · jetzt Kühlschrank {b['menge']}, Lager {b['lager']}")
    return jsonify(bestand=b["menge"], lager=b["lager"])


@app.post("/api/produkte/<int:pid>/auffuellen")
@api(TEAM)
def produkt_auffuellen(u, pid):
    """Vom Lager in den Kühlschrank derselben Halle umlagern."""
    d = body()
    try:
        menge = int(d.get("menge"))
    except (TypeError, ValueError):
        raise Fehler("Bitte eine Menge eingeben.")
    if menge < 1 or menge > 9999:
        raise Fehler("Die Menge muss zwischen 1 und 9999 liegen.")
    with tx() as c:
        p = _get_produkt(c, pid)
        shop = _get_shop(c, d.get("shop"))
        darf_shop(u, shop["id"])
        b = c.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=?", (pid, shop["id"])).fetchone()
        if not b or b["lager"] < menge:
            raise Fehler(f"Im Lager sind nur {b['lager'] if b else 0} Stück {p['name']}.", 409)
        c.execute("UPDATE bestand SET lager = lager - ?, menge = menge + ?, gefuehrt = 1 WHERE produkt_id=? AND shop_id=?",
                  (menge, menge, pid, shop["id"]))
        c.execute("INSERT INTO lagerbuch(zeit, shop_id, produkt_id, name, art, menge, ek, von, notiz, ort) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ms(), shop["id"], pid, p["name"], "Auffüllen", menge, p["ek"], u["name"], "Lager → Kühlschrank", "umlagerung"))
        log(c, u["name"], "Kühlschrank aufgefüllt", f"{shop['name']} · {menge}× {p['name']} aus dem Lager · "
            f"jetzt Kühlschrank {b['menge'] + menge}, Lager {b['lager'] - menge}")
    return jsonify(bestand=b["menge"] + menge, lager=b["lager"] - menge)


@app.post("/api/produkte/<int:pid>/zuruecklagern")
@api(TEAM)
def produkt_zuruecklagern(u, pid):
    """Vom Kühlschrank zurück ins Lager derselben Halle – z. B. wenn im Kühlschrank Platz für Neues gebraucht wird."""
    d = body()
    try:
        menge = int(d.get("menge"))
    except (TypeError, ValueError):
        raise Fehler("Bitte eine Menge eingeben.")
    if menge < 1 or menge > 9999:
        raise Fehler("Die Menge muss zwischen 1 und 9999 liegen.")
    with tx() as c:
        p = _get_produkt(c, pid)
        shop = _get_shop(c, d.get("shop"))
        darf_shop(u, shop["id"])
        b = c.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=?", (pid, shop["id"])).fetchone()
        if not b or b["menge"] < menge:
            raise Fehler(f"Im Kühlschrank sind nur {max(b['menge'], 0) if b else 0} Stück {p['name']}.", 409)
        c.execute("UPDATE bestand SET menge = menge - ?, lager = lager + ? WHERE produkt_id=? AND shop_id=?",
                  (menge, menge, pid, shop["id"]))
        c.execute("INSERT INTO lagerbuch(zeit, shop_id, produkt_id, name, art, menge, ek, von, notiz, ort) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ms(), shop["id"], pid, p["name"], "Zurücklagern", menge, p["ek"], u["name"], "Kühlschrank → Lager",
                   "rueckumlagerung"))
        log(c, u["name"], "Ins Lager zurückgelegt", f"{shop['name']} · {menge}× {p['name']} aus dem Kühlschrank · "
            f"jetzt Kühlschrank {b['menge'] - menge}, Lager {b['lager'] + menge}")
    return jsonify(bestand=b["menge"] - menge, lager=b["lager"] + menge)


@app.post("/api/inventur")
@api(TEAM)
def inventur(u):
    d = body()
    zaehlung = d.get("zaehlung")
    if not isinstance(zaehlung, list) or not zaehlung:
        raise Fehler("Es wurde nichts gezählt.")
    min_b = einstellungen()["minBestand"]
    ort = _ort(d, standard="kuehlschrank")
    sp = "lager" if ort == "lager" else "menge"
    knapp, diffs, wert = [], [], 0
    with tx() as c:
        shop = _get_shop(c, d.get("shop"))
        darf_shop(u, shop["id"])
        jetzt = ms()
        for z in zaehlung:
            try:
                pid, ist = int(z["pid"]), int(z["ist"])
            except (KeyError, TypeError, ValueError):
                raise Fehler("Ungültige Zählung.")
            if ist < 0 or ist > 99999:
                raise Fehler("Ungültige Zählung.")
            p = _get_produkt(c, pid)
            b = c.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=?", (pid, shop["id"])).fetchone()
            soll = b[sp] if b else 0
            diff = ist - soll
            if diff == 0:
                continue
            c.execute(f"INSERT INTO bestand(produkt_id, shop_id, {sp}, gefuehrt) VALUES (?,?,?,1) "
                      f"ON CONFLICT(produkt_id, shop_id) DO UPDATE SET {sp} = excluded.{sp}", (pid, shop["id"], ist))
            c.execute("INSERT INTO lagerbuch(zeit, shop_id, produkt_id, name, art, menge, ek, von, notiz, ort) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (jetzt, shop["id"], pid, p["name"], "Inventur", diff, p["ek"], u["name"],
                       f"{ORTE[ort]}: Soll {soll}, gezählt {ist}", ort))
            diffs.append(f"{p['name']} {diff:+d}")
            wert += diff * (p["ek"] or 0)
            if ort == "kuehlschrank" and soll > min_b >= ist:
                knapp.append((p["name"], ist, b["lager"] if b else 0))
        log(c, u["name"], "Inventur", f"{shop['name']} · {ORTE[ort]} · {len(zaehlung)} Produkte gezählt · "
            + (", ".join(diffs) + f" · Wert {eur_txt(wert)}" if diffs else "keine Abweichung"))
    if knapp:
        nachfuellen_melden(shop, knapp)
    return jsonify(abweichungen=len(diffs), wert=wert / 100)


@app.delete("/api/produkte/<int:pid>")
@api(INHABER)
def produkt_loeschen(u, pid):
    with tx() as c:
        p = _get_produkt(c, pid)
        rest = c.execute("SELECT COALESCE(SUM(menge + lager),0) FROM bestand WHERE produkt_id=?", (pid,)).fetchone()[0]
        c.execute("UPDATE produkte SET geloescht=1 WHERE id=?", (pid,))
        c.execute("UPDATE bestand SET gefuehrt=0 WHERE produkt_id=?", (pid,))
        log(c, u["name"], "Produkt gelöscht", f"{p['name']} · zuletzt {eur_txt(p['preis'])} · Restbestand gesamt {rest}")
    return jsonify(ok=True)


# ------------------------------------------------------------------ Shops (Administrator)
def _shop_felder(d):
    name = str(d.get("name", "")).strip()[:40]
    if not name:
        raise Fehler("Bitte einen Namen für den Shop eingeben.")
    return name, str(d.get("ort", "")).strip()[:80]


def _team_setzen(c, sid, ids):
    gueltig = {r[0] for r in c.execute("SELECT id FROM konten WHERE entfernt=0 AND rolle IN ('personal','inhaber')")}
    ids = [int(i) for i in ids or [] if str(i).isdigit() and int(i) in gueltig]
    c.execute("DELETE FROM shop_team WHERE shop_id=?", (sid,))
    c.executemany("INSERT INTO shop_team(shop_id, konto_id) VALUES (?,?)", [(sid, i) for i in ids])
    namen = [r[0] for r in c.execute(f"SELECT name FROM konten WHERE id IN ({','.join(map(str, ids)) or 'NULL'})")]
    return namen


@app.post("/api/shops")
@api(INHABER)
def shop_neu(u):
    d = body()
    name, ort = _shop_felder(d)
    with tx() as c:
        if c.execute("SELECT 1 FROM shops WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise Fehler("Einen Shop mit diesem Namen gibt es schon.")
        sort = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM shops").fetchone()[0]
        sid = c.execute("INSERT INTO shops(name, ort, sort, angelegt) VALUES (?,?,?,?)", (name, ort, sort, ms())).lastrowid
        kopie = ""
        if d.get("kopieVon"):
            quelle = _get_shop(c, d.get("kopieVon"))
            n = c.execute("INSERT INTO bestand(produkt_id, shop_id, menge, gefuehrt) "
                          "SELECT b.produkt_id, ?, 0, 1 FROM bestand b JOIN produkte p ON p.id=b.produkt_id "
                          "WHERE b.shop_id=? AND b.gefuehrt=1 AND p.geloescht=0", (sid, quelle["id"])).rowcount
            kopie = f" · Sortiment von {quelle['name']} übernommen ({n} Produkte, Bestand 0)"
        namen = _team_setzen(c, sid, d.get("team"))
        log(c, u["name"], "Shop angelegt", f"{name}{kopie}" + (f" · Shopverantwortliche: {', '.join(namen)}" if namen else ""))
    return jsonify(id=sid)


@app.put("/api/shops/<int:sid>")
@api(INHABER)
def shop_aendern(u, sid):
    d = body()
    with tx() as c:
        alt = _get_shop(c, sid)
        name, ort = _shop_felder({"name": d.get("name", alt["name"]), "ort": d.get("ort", alt["ort"])})
        if c.execute("SELECT 1 FROM shops WHERE lower(name)=lower(?) AND id!=?", (name, sid)).fetchone():
            raise Fehler("Einen Shop mit diesem Namen gibt es schon.")
        aktiv = alt["aktiv"] if "aktiv" not in d else (1 if d.get("aktiv") else 0)
        if not aktiv and alt["aktiv"] and c.execute("SELECT COUNT(*) FROM shops WHERE aktiv=1").fetchone()[0] <= 1:
            raise Fehler("Mindestens ein Shop muss geöffnet bleiben.")
        c.execute("UPDATE shops SET name=?, ort=?, aktiv=? WHERE id=?", (name, ort, aktiv, sid))
        aend = []
        if name != alt["name"]:
            aend.append(f"Name: {alt['name']} → {name}")
        if ort != alt["ort"]:
            aend.append(f"Ort: {ort or '–'}")
        if aktiv != alt["aktiv"]:
            aend.append("geöffnet" if aktiv else "geschlossen (nicht mehr auswählbar)")
        if aend:
            log(c, u["name"], "Shop geändert", f"{name} · " + " · ".join(aend))
    return jsonify(ok=True)


@app.put("/api/shops/<int:sid>/reihenfolge")
@api(TEAM)
def shop_reihenfolge(u, sid):
    """Eigene Reihenfolge der Produkte in diesem Shop (so, wie sie im Kühlschrank stehen)."""
    pids = body().get("produkte")
    if not isinstance(pids, list) or len(pids) > 2000:
        raise Fehler("Ungültige Reihenfolge.")
    with tx() as c:
        shop = _get_shop(c, sid)
        darf_shop(u, shop["id"])
        for i, pid in enumerate(pids):
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                raise Fehler("Ungültige Reihenfolge.")
            c.execute("UPDATE bestand SET sort=? WHERE produkt_id=? AND shop_id=?", (i, pid, shop["id"]))
        log(c, u["name"], "Reihenfolge geändert", f"{shop['name']} · {len(pids)} Produkte")
    return jsonify(ok=True)


@app.put("/api/shops/<int:sid>/team")
@api(INHABER)
def shop_team(u, sid):
    d = body()
    with tx() as c:
        shop = _get_shop(c, sid)
        namen = _team_setzen(c, sid, d.get("konten"))
        log(c, u["name"], "Shopverantwortliche zugeordnet", f"{shop['name']}: {', '.join(namen) or 'niemand'}")
    return jsonify(ok=True)


@app.put("/api/konten/<int:kid>/shops")
@api(INHABER)
def konto_shops(u, kid):
    ids = body().get("shops") or []
    with tx() as c:
        k = _get_konto(c, kid)
        if k["rolle"] == "kunde":
            raise Fehler("Nur Shopverantwortlichen und Administratoren kann man Shops zuordnen.")
        gueltig = {r[0]: r[1] for r in c.execute("SELECT id, name FROM shops")}
        ids = [int(i) for i in ids if str(i).isdigit() and int(i) in gueltig]
        c.execute("DELETE FROM shop_team WHERE konto_id=?", (kid,))
        c.executemany("INSERT INTO shop_team(shop_id, konto_id) VALUES (?,?)", [(i, kid) for i in ids])
        log(c, u["name"], "Zuständige Shops geändert", f"{k['name']}: {', '.join(gueltig[i] for i in ids) or 'keine'}")
    return jsonify(ok=True)


@app.delete("/api/shops/<int:sid>")
@api(INHABER)
def shop_loeschen(u, sid):
    with tx() as c:
        shop = _get_shop(c, sid)
        if c.execute("SELECT COUNT(*) FROM shops").fetchone()[0] <= 1:
            raise Fehler("Der letzte Shop kann nicht gelöscht werden.")
        if c.execute("SELECT 1 FROM verkaeufe WHERE shop_id=? LIMIT 1", (sid,)).fetchone():
            raise Fehler("In diesem Shop wurde schon verkauft – die Buchungen müssen erhalten bleiben. "
                         "Du kannst ihn stattdessen schließen.", 409)
        c.execute("DELETE FROM bestand WHERE shop_id=?", (sid,))
        c.execute("DELETE FROM lagerbuch WHERE shop_id=?", (sid,))
        c.execute("DELETE FROM shop_team WHERE shop_id=?", (sid,))
        c.execute("DELETE FROM shops WHERE id=?", (sid,))
        log(c, u["name"], "Shop gelöscht", shop["name"])
    return jsonify(ok=True)


# ------------------------------------------------------------------ Einstellungen (Administrator)
@app.put("/api/einstellungen")
@api(INHABER)
def einstellungen_setzen(u):
    d = body()
    alt = einstellungen()
    neu = dict(alt)
    try:
        neu["shopName"] = str(d.get("shopName", alt["shopName"])).strip()[:40] or "Kaffeekasse"
        neu["adresse"] = str(d.get("adresse", alt["adresse"])).strip()[:120]
        neu["limit"] = max(0.0, round(float(str(d.get("limit", alt["limit"])).replace(",", ".")), 2))
        neu["autoLogout"] = max(30, min(86400, int(d.get("autoLogout", alt["autoLogout"]))))
        neu["autoLogoutTeam"] = max(60, min(86400, int(d.get("autoLogoutTeam", alt["autoLogoutTeam"]))))
        neu["minBestand"] = max(0, min(999, int(d.get("minBestand", alt["minBestand"]))))
        neu["warnGuthaben"] = max(0.0, min(1000.0, round(float(str(d.get("warnGuthaben", alt.get("warnGuthaben", 5))).replace(",", ".")), 2)))
        neu["datenschutzKontakt"] = str(d.get("datenschutzKontakt", alt.get("datenschutzKontakt", ""))).strip()[:500]
        neu["stil"] = d.get("stil", alt.get("stil", "standard"))
        if neu["stil"] not in STILE:
            raise Fehler("Unbekanntes Design.")
        neu["meldeUrl"] = str(d.get("meldeUrl", alt.get("meldeUrl", ""))).strip()[:300]
        if neu["meldeUrl"] and not re.match(r"^https?://", neu["meldeUrl"]):
            raise Fehler("Die Webhook-Adresse muss mit http:// oder https:// beginnen.")
        fmt = d.get("meldeFormat", alt.get("meldeFormat"))
        neu["meldeFormat"] = fmt if fmt in MELDE_FORMATE else "ntfy"
        if neu["meldeFormat"] == "ntfy" and neu["meldeUrl"] and ist_gotify_url(neu["meldeUrl"]):
            neu["meldeFormat"] = "gotify"

    except (TypeError, ValueError):
        raise Fehler("Ungültige Eingabe.")
    with tx() as c:
        for key, v in neu.items():
            c.execute("INSERT OR REPLACE INTO einstellungen(key, value) VALUES (?,?)", (key, json.dumps(v)))
        diff = [f"{key}: {alt.get(key)} → {v}" for key, v in neu.items() if alt.get(key) != v]
        if diff:
            log(c, u["name"], "Einstellungen geändert", " · ".join(diff))
    return jsonify(einstellungen=neu)


# ------------------------------------------------------------------ Export
ZAHL_RE = re.compile(r"^-?[\d.]+(,\d+)?$")


def _zelle(v):
    """Text, der mit = + - @ beginnt, würde Excel als Formel ausführen → mit ' entschärfen (Zahlen bleiben)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") and not ZAHL_RE.match(v):
        return "'" + v
    return v


def csv_antwort(name, rows):
    buf = io.StringIO()
    buf.write("\ufeff")
    csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL).writerows([[_zelle(v) for v in r] for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def de(c):
    return "" if c is None else f"{c / 100:.2f}".replace(".", ",")


def _dateiname(basis):
    von, bis = _zeitraum()
    if von:
        a = datetime.fromtimestamp(von / 1000, TZ).strftime("%Y-%m-%d")
        b = datetime.fromtimestamp((bis - 1) / 1000, TZ).strftime("%Y-%m-%d")
        return f"{basis}_{a}_bis_{b}.csv" if a != b else f"{basis}_{a}.csv"
    return f"{basis}_{datetime.now(TZ):%Y-%m-%d}.csv"


@app.get("/api/export/verkaeufe.csv")
@api(TEAM)
def export_verkaeufe(u):
    """Verkäufe der erlaubten Shops im Zeitraum; ?shop=ID schränkt ein."""
    von, bis = _zeitraum()
    ids = _shops_aus_anfrage(u)
    rows = [["Beleg", "Zeit", "Shop", "Personalnummer", "Mitglied", "Artikel", "Kategorie", "Menge", "Einzelpreis",
             "Gesamt", "Einkaufspreis", "Rohertrag", "Storno"]]
    for r in db().execute("SELECT p.*, v.zeit, v.name AS konto, k.nummer, s.name AS shop FROM posten p "
                          "JOIN verkaeufe v ON v.id=p.verkauf_id JOIN konten k ON k.id=v.konto_id "
                          "LEFT JOIN shops s ON s.id=v.shop_id "
                          f"WHERE v.shop_id IN {_in(ids)} AND v.zeit >= ? AND v.zeit < ? ORDER BY v.id, p.id", (von, bis)):
        roh = None if r["ek"] is None or r["storno"] else (r["preis"] - r["ek"]) * r["menge"]
        rows.append([beleg(r["verkauf_id"]), zeit_txt(r["zeit"]), r["shop"] or "", r["nummer"], r["konto"], r["name"], r["kat"],
                     r["menge"], de(r["preis"]), de(r["preis"] * r["menge"]), de(r["ek"]), de(roh),
                     "ja" if r["storno"] else "nein"])
    return csv_antwort(_dateiname("verkaeufe"), rows)


@app.get("/api/export/kassenbuch.csv")
@api(TEAM)
def export_kassenbuch(_u):
    von, bis = _zeitraum()
    rows = [["Zeit", "Personalnummer", "Mitglied", "Art", "Betrag", "Von", "Notiz"]]
    for r in db().execute("SELECT b.*, k.nummer, k.name FROM kassenbuch b JOIN konten k ON k.id=b.konto_id "
                          "WHERE b.zeit >= ? AND b.zeit < ? ORDER BY b.id", (von, bis)):
        rows.append([zeit_txt(r["zeit"]), r["nummer"], r["name"], r["art"], de(r["betrag"]), r["von"], r["notiz"]])
    return csv_antwort(_dateiname("kassenbuch"), rows)


@app.get("/api/export/kontostaende.csv")
@api(TEAM)
def export_konten(_u):
    rows = [["Personalnummer", "Nachname", "Vorname", "Rolle", "Guthaben", "Interne Rufnummer", "Dienststelle/Raum", "Status"]]
    for r in db().execute("SELECT * FROM konten WHERE entfernt=0 ORDER BY nachname COLLATE NOCASE, vorname COLLATE NOCASE"):
        rows.append([r["nummer"], r["nachname"], r["vorname"], ROLLEN_NAME[r["rolle"]], de(r["guthaben"]),
                     r["tel"], r["raum"], f"stillgelegt seit {zeit_txt(r['stillgelegt'])[:10]}" if r["stillgelegt"] else "aktiv"])
    return csv_antwort(f"kontostaende_{datetime.now(TZ):%Y-%m-%d}.csv", rows)


@app.get("/api/export/protokoll.csv")
@api(TEAM)
def export_protokoll(_u):
    von, bis = _zeitraum()
    rows = [["Zeit", "Wer", "Was", "Einzelheit"]]
    for r in db().execute("SELECT * FROM journal WHERE zeit >= ? AND zeit < ? ORDER BY id", (von, bis)):
        rows.append([zeit_txt(r["zeit"]), r["wer"], r["was"], r["detail"]])
    return csv_antwort(_dateiname("protokoll"), rows)


@app.get("/api/export/lagerbuch.csv")
@api(TEAM)
def export_lagerbuch(u):
    von, bis = _zeitraum()
    ids = _shops_aus_anfrage(u)
    rows = [["Zeit", "Shop", "Produkt", "Art", "Ort", "Menge", "Einkaufspreis", "Wert", "Von", "Notiz"]]
    for r in db().execute("SELECT l.*, s.name AS shop FROM lagerbuch l LEFT JOIN shops s ON s.id=l.shop_id "
                          f"WHERE l.shop_id IN {_in(ids)} AND l.zeit >= ? AND l.zeit < ? ORDER BY l.id", (von, bis)):
        rows.append([zeit_txt(r["zeit"]), r["shop"] or "", r["name"], r["art"],
                     {"kuehlschrank": "Kühlschrank", "lager": "Lager", "umlagerung": "Lager → Kühlschrank",
                      "rueckumlagerung": "Kühlschrank → Lager"}.get(r["ort"], r["ort"]),
                     r["menge"], de(r["ek"]),
                     de(None if r["ek"] is None else r["ek"] * r["menge"]), r["von"], r["notiz"]])
    return csv_antwort(_dateiname("lagerbuch"), rows)


@app.get("/api/export/bestand.csv")
@api(TEAM)
def export_bestand(u):
    ids = _shops_aus_anfrage(u)
    rows = [["Shop", "Produkt", "Kategorie", "Kühlschrank", "Lager", "Gesamt", "Verkaufspreis", "Einkaufspreis", "Warenwert (EK)"]]
    for r in db().execute("SELECT b.menge, b.lager, s.name AS shop, p.name, p.kat, p.preis, p.ek FROM bestand b "
                          "JOIN produkte p ON p.id=b.produkt_id JOIN shops s ON s.id=b.shop_id "
                          f"WHERE b.gefuehrt=1 AND p.geloescht=0 AND b.shop_id IN {_in(ids)} "
                          "ORDER BY s.sort, s.name, p.kat, p.name"):
        gesamt = max(0, r["menge"]) + max(0, r["lager"])
        rows.append([r["shop"], r["name"], r["kat"], r["menge"], r["lager"], gesamt, de(r["preis"]), de(r["ek"]),
                     de(None if r["ek"] is None else r["ek"] * gesamt)])
    return csv_antwort(f"bestand_{datetime.now(TZ):%Y-%m-%d}.csv", rows)


# ------------------------------------------------------------------ Benachrichtigungen (Nachfüllen)
LETZTE_MELDUNG = {}


MELDE_FORMATE = ("ntfy", "gotify", "json")


def ist_gotify_url(url):
    """Gotify-Adressen enden auf /message (Token als ?token=… oder im Pfad davor)."""
    from urllib.parse import urlparse
    return urlparse(url).path.rstrip("/").endswith("/message")


def _webhook(url, fmt, titel, text, daten):
    import urllib.error
    import urllib.request
    if fmt == "ntfy" and ist_gotify_url(url):
        fmt = "gotify"                     # ältere Einstellung: Gotify-Adresse mit Format "Text"
    if fmt == "gotify":
        # Gotify: POST /message?token=<App-Token>, JSON mit title/message/priority
        data = json.dumps({"title": titel, "message": text, "priority": 5,
                           "extras": {"client::display": {"contentType": "text/plain"}, "kaffeekasse": daten}},
                          ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    elif fmt == "json":
        data = json.dumps({"titel": titel, "text": text, **daten}, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    else:   # ntfy & Co.: Text im Body, Titel im Header (RFC 2047 wegen Umlauten)
        data = text.encode()
        headers = {"Content-Type": "text/plain; charset=utf-8", "Tags": "coffee",
                   "Title": "=?UTF-8?B?" + base64.b64encode(titel.encode()).decode() + "?="}
    headers["User-Agent"] = "Kaffeekasse"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 300:
                raise OSError(f"HTTP {r.status}")
    except urllib.error.HTTPError as ex:   # Antwort des Servers mitliefern, damit man den Fehler versteht
        try:
            antwort = ex.read(300).decode("utf-8", "replace").strip()
        except Exception:                  # noqa: BLE001
            antwort = ""
        hinweis = ""
        if fmt == "gotify" and ex.code in (401, 403):
            hinweis = " – Token prüfen: in Gotify unter „Apps“ eine App anlegen und deren Token verwenden (nicht den Client-Token)."
        raise OSError(f"HTTP {ex.code} {ex.reason}" + (f": {antwort}" if antwort else "") + hinweis) from None
    except urllib.error.URLError as ex:
        grund = str(ex.reason)
        if "CERTIFICATE_VERIFY_FAILED" in grund:
            grund += " – das HTTPS-Zertifikat wird nicht anerkannt (selbst signiert?). Besser die http-Adresse im internen Netz verwenden."
        elif "Name or service not known" in grund or "nodename nor servname" in grund or "Temporary failure in name resolution" in grund:
            grund += " – der Name ist aus dem Container nicht auflösbar. IP-Adresse verwenden oder beide Container ins selbe Docker-Netz."
        elif "Connection refused" in grund:
            grund += " – unter dieser Adresse/Port antwortet nichts. Achtung: „localhost“ ist im Container der Container selbst."
        raise OSError(grund) from None


def benachrichtigen(titel, text, daten, warten=False):
    """Schickt die Meldung an den Webhook (ntfy, Home Assistant …). Läuft im Hintergrund."""
    e = einstellungen()
    url, fmt = (e.get("meldeUrl") or "").strip(), e.get("meldeFormat") or "ntfy"

    def senden():
        try:
            _webhook(url, fmt, titel, text, daten)
            ergebnis = ["Webhook: gesendet"]
        except Exception as ex:          # noqa: BLE001 – Meldung darf den Verkauf nie stören
            ergebnis = [f"Webhook: Fehler – {ex}"]
        LETZTE_MELDUNG.update(zeit=ms(), titel=titel, ergebnis=ergebnis)
        print(f"Kaffeekasse Benachrichtigung – {titel}: {ergebnis[0]}", flush=True)
        return ergebnis

    if not url:
        return ["Keine Webhook-Adresse eingetragen."]
    if warten:
        return senden()
    import threading
    threading.Thread(target=senden, daemon=True).start()
    return []


def nachfuellen_melden(shop, knapp):
    titel = f"Nachfüllen: {shop['name']}"
    text = "\n".join(f"{name}: noch {n} im Kühlschrank" + (f" (im Lager: {lg})" if lg else " – Lager leer, bitte nachbestellen")
                     for name, n, lg in knapp)
    benachrichtigen(titel, text, {"shop": shop["name"],
                                  "produkte": [{"name": n, "bestand": b, "lager": lg} for n, b, lg in knapp]})


@app.post("/api/benachrichtigung/test")
@api(INHABER)
def benachrichtigung_test(u):
    d = body()
    sid = d.get("shop") or alle_shops()[0]["id"]
    shop = _get_shop(db(), sid)
    erg = benachrichtigen(f"Test: {shop['name']}", f"Testmeldung der Kaffeekasse, ausgelöst von {u['name']}.",
                          {"shop": shop["name"], "test": True, "produkte": []}, warten=True)
    return jsonify(ergebnis=erg)


# ------------------------------------------------------------------ Kasse (Bargeld)
# Eine gemeinsame Kasse für alle Shops. Jede Aufladung ist Bargeld und landet in der Kasse,
# Auszahlungen, Ausgaben (Versand, Events, Wareneinkauf …) und Einlagen werden hier gebucht.
KASSE_KATEGORIEN = ["Wareneinkauf", "Versand", "Event", "Pfand", "Spende", "Anfangsbestand", "Sonstiges"]
STUECKELUNG = [50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10, 5, 2, 1]


def kassenstand(c=None):
    return (c or db()).execute("SELECT COALESCE(SUM(betrag),0) FROM kasse").fetchone()[0]


def kasse_buchen(c, art, betrag, von, kategorie="", zweck="", beleg="", konto_id=None, pruefung_id=None, storno_von=None,
                 zeit=None):
    return c.execute("INSERT INTO kasse(zeit, art, betrag, von, kategorie, zweck, beleg, konto_id, pruefung_id, storno_von) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (zeit or ms(), art, betrag, von, kategorie, zweck, beleg, konto_id, pruefung_id, storno_von)).lastrowid


def _kasse_zeile(r, saldo=None):
    d = {"id": r["id"], "zeit": r["zeit"], "art": r["art"], "betrag": r["betrag"] / 100, "von": r["von"],
         "kategorie": r["kategorie"], "zweck": r["zweck"], "beleg": r["beleg"], "kontoId": r["konto_id"],
         "pruefungId": r["pruefung_id"], "stornoVon": r["storno_von"]}
    if saldo is not None:
        d["saldo"] = saldo / 100
    return d


def _pruefung_public(r):
    return {"id": r["id"], "zeit": r["zeit"], "soll": r["soll"] / 100, "ist": r["ist"] / 100,
            "differenz": r["differenz"] / 100, "pruefer": r["pruefer"], "pruefer2": r["pruefer2"],
            "zaehlung": json.loads(r["zaehlung"] or "{}"), "bemerkung": r["bemerkung"], "gebucht": bool(r["gebucht"])}


@app.get("/api/kasse")
@api(TEAM)
def kasse_ansicht(_u):
    von, bis = _zeitraum()
    c = db()
    vorher = c.execute("SELECT COALESCE(SUM(betrag),0) FROM kasse WHERE zeit < ?", (von,)).fetchone()[0]
    zeilen, saldo = [], vorher
    for r in c.execute("SELECT * FROM kasse WHERE zeit >= ? AND zeit < ? ORDER BY zeit, id", (von, bis)):
        saldo += r["betrag"]
        zeilen.append(_kasse_zeile(r, saldo))
    zeilen.reverse()
    storniert = {r[0] for r in c.execute("SELECT storno_von FROM kasse WHERE storno_von IS NOT NULL")}
    for z in zeilen:
        z["storniert"] = z["id"] in storniert
    ein = sum(z["betrag"] for z in zeilen if z["betrag"] > 0)
    aus = sum(z["betrag"] for z in zeilen if z["betrag"] < 0)
    letzte = c.execute("SELECT * FROM kassenpruefungen ORDER BY id DESC LIMIT 1").fetchone()
    seit = c.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN betrag>0 THEN betrag END),0), "
                     "COALESCE(SUM(CASE WHEN betrag<0 THEN betrag END),0) FROM kasse WHERE zeit > ?",
                     (letzte["zeit"] if letzte else 0,)).fetchone()
    pruefungen = [_pruefung_public(r) for r in c.execute(
        "SELECT * FROM kassenpruefungen WHERE zeit >= ? AND zeit < ? ORDER BY id DESC", (von, bis))]
    return jsonify(stand=kassenstand(c) / 100, anfang=vorher / 100, eintraege=zeilen, einnahmen=round(ein, 2),
                   ausgaben=round(aus, 2), letztePruefung=_pruefung_public(letzte) if letzte else None,
                   seitPruefung={"buchungen": seit[0], "ein": seit[1] / 100, "aus": seit[2] / 100},
                   pruefungen=pruefungen, kategorien=KASSE_KATEGORIEN, stueckelung=STUECKELUNG)


@app.post("/api/kasse")
@api(TEAM)
def kasse_buchung(u):
    """Ausgabe (Versand, Event, Wareneinkauf …) oder Einlage von Hand buchen."""
    d = body()
    art = "Ausgabe" if d.get("art") == "ausgabe" else "Einlage" if d.get("art") == "einlage" else None
    if not art:
        raise Fehler("Bitte Ausgabe oder Einlage wählen.")
    betrag = cents(d.get("betrag"))
    if betrag <= 0:
        raise Fehler("Bitte einen Betrag über 0 eingeben.")
    kategorie = " ".join(str(d.get("kategorie", "")).split())[:40] or "Sonstiges"
    zweck = " ".join(str(d.get("zweck", "")).split())[:200]
    beleg = " ".join(str(d.get("beleg", "")).split())[:60]
    if not zweck:
        raise Fehler("Bitte angeben, wofür das Geld ist (Zweck).")
    zweiter = None
    if art == "Ausgabe" and u["rolle"] != "inhaber":
        if betrag > kassenstand():
            raise Fehler(f"In der Kasse sind nur {eur_txt(kassenstand())}.", 409)
        zweiter = freigabe_pruefen(u, d, "Geld aus der Kasse")
    von = u["name"] + (f" · freigegeben von {zweiter['name']}" if zweiter else "")
    with tx() as c:
        stand = kassenstand(c)
        if art == "Ausgabe" and betrag > stand:
            raise Fehler(f"In der Kasse sind nur {eur_txt(stand)}.", 409)
        wert = -betrag if art == "Ausgabe" else betrag
        kid = kasse_buchen(c, art, wert, von, kategorie, zweck, beleg)
        log(c, u["name"], f"Kasse: {art}", f"{kategorie} · {zweck} · {eur_txt(wert)}" + (f" · Beleg {beleg}" if beleg else "")
            + f" · Kassenstand {eur_txt(stand + wert)}" + (f" · freigegeben von {zweiter['name']}" if zweiter else ""))
    return jsonify(id=kid, stand=(stand + wert) / 100)


@app.post("/api/kasse/<int:eid>/storno")
@api(INHABER)
def kasse_storno(u, eid):
    """Fehlbuchung (Ausgabe/Einlage) durch eine Gegenbuchung aufheben – nichts wird gelöscht."""
    with tx() as c:
        r = c.execute("SELECT * FROM kasse WHERE id=?", (eid,)).fetchone()
        if not r or r["art"] not in ("Ausgabe", "Einlage"):
            raise Fehler("Nur Ausgaben und Einlagen lassen sich hier stornieren. Aufladungen korrigiert man beim Mitglied.")
        if c.execute("SELECT 1 FROM kasse WHERE storno_von=?", (eid,)).fetchone():
            raise Fehler("Diese Buchung ist schon storniert.", 409)
        kasse_buchen(c, "Storno", -r["betrag"], u["name"], r["kategorie"], f"Storno: {r['zweck']}", r["beleg"], storno_von=eid)
        log(c, u["name"], "Kasse: Storno", f"{r['art']} {eur_txt(r['betrag'])} · {r['zweck']}")
    return jsonify(ok=True)


@app.post("/api/kasse/pruefung")
@api(TEAM)
def kasse_pruefung(u):
    """Kassenprüfung: gezähltes Bargeld mit dem Soll vergleichen, Protokoll speichern, Differenz auf Wunsch buchen."""
    d = body()
    zaehlung = {}
    if isinstance(d.get("zaehlung"), dict):
        for k_, v in d["zaehlung"].items():
            try:
                w, n = int(k_), int(v or 0)
            except (TypeError, ValueError):
                raise Fehler("Ungültige Zählung.")
            if w not in STUECKELUNG or n < 0 or n > 100000:
                raise Fehler("Ungültige Zählung.")
            if n:
                zaehlung[str(w)] = n
    ist = sum(int(w) * n for w, n in zaehlung.items()) if zaehlung else cents(d.get("ist"))
    if ist < 0:
        raise Fehler("Ungültiger Betrag.")
    pruefer2 = " ".join(str(d.get("pruefer2", "")).split())[:80]
    bemerkung = str(d.get("bemerkung", "")).strip()[:500]
    buchen = bool(d.get("buchen", True))
    freigegeben = False
    if buchen and ist < kassenstand() and u["rolle"] != "inhaber":
        # Einen Fehlbetrag auszubuchen nimmt Geld aus der Kasse → nur mit Freigabe; die zweite Person ist dann der 2. Prüfer
        pruefer2 = freigabe_pruefen(u, d, "Einen Fehlbetrag auszubuchen")["name"]
        freigegeben = True
    with tx() as c:
        soll = kassenstand(c)
        diff = ist - soll
        if buchen and diff < 0 and u["rolle"] != "inhaber" and not freigegeben:
            raise Fehler("Der Kassenstand hat sich gerade geändert – bitte die Prüfung noch einmal speichern.", 409)
        jetzt = ms()   # Prüfung und Differenzbuchung bekommen DENSELBEN Zeitpunkt („seit letzter Prüfung“ stimmt dann)
        pid = c.execute("INSERT INTO kassenpruefungen(zeit, soll, ist, differenz, pruefer, pruefer2, zaehlung, bemerkung, gebucht) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (jetzt, soll, ist, diff, u["name"], pruefer2, json.dumps(zaehlung), bemerkung,
                         int(buchen and diff != 0))).lastrowid
        if buchen and diff:
            kasse_buchen(c, "Kassendifferenz", diff, u["name"], "Kassenprüfung",
                         f"Kassenprüfung #{pid}: Soll {eur_txt(soll)}, gezählt {eur_txt(ist)}", pruefung_id=pid, zeit=jetzt)
        log(c, u["name"], "Kassenprüfung", f"Soll {eur_txt(soll)} · gezählt {eur_txt(ist)} · Differenz {eur_txt(diff)}"
            + (f" · zweiter Prüfer: {pruefer2}" if pruefer2 else "") + (" · Differenz gebucht" if buchen and diff else ""))
        r = c.execute("SELECT * FROM kassenpruefungen WHERE id=?", (pid,)).fetchone()
    return jsonify(_pruefung_public(r))


@app.get("/api/kasse/pruefung/<int:pid>")
@api(TEAM)
def kasse_pruefung_lesen(_u, pid):
    c = db()
    r = c.execute("SELECT * FROM kassenpruefungen WHERE id=?", (pid,)).fetchone()
    if not r:
        raise Fehler("Diese Kassenprüfung gibt es nicht.", 404)
    vorige = c.execute("SELECT * FROM kassenpruefungen WHERE id < ? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    ab = vorige["zeit"] if vorige else 0
    buchungen = c.execute("SELECT art, COUNT(*) n, COALESCE(SUM(betrag),0) s FROM kasse WHERE zeit > ? AND zeit <= ? "
                          "AND (pruefung_id IS NULL OR pruefung_id != ?) GROUP BY art ORDER BY art", (ab, r["zeit"], pid)).fetchall()
    return jsonify(pruefung=_pruefung_public(r), vorige=_pruefung_public(vorige) if vorige else None,
                   zeitraum=[ab, r["zeit"]], buchungen=[[b["art"], b["n"], b["s"] / 100] for b in buchungen])


@app.get("/api/export/kasse.csv")
@api(TEAM)
def export_kasse(_u):
    von, bis = _zeitraum()
    c = db()
    saldo = c.execute("SELECT COALESCE(SUM(betrag),0) FROM kasse WHERE zeit < ?", (von,)).fetchone()[0]
    rows = [["Nr", "Zeit", "Art", "Kategorie", "Zweck", "Beleg", "Einnahme", "Ausgabe", "Kassenstand", "Gebucht von"],
            ["", "", "Übertrag", "", "", "", "", "", de(saldo), ""]]
    for r in c.execute("SELECT * FROM kasse WHERE zeit >= ? AND zeit < ? ORDER BY zeit, id", (von, bis)):
        saldo += r["betrag"]
        rows.append([r["id"], zeit_txt(r["zeit"]), r["art"], r["kategorie"], r["zweck"], r["beleg"],
                     de(r["betrag"]) if r["betrag"] > 0 else "", de(-r["betrag"]) if r["betrag"] < 0 else "",
                     de(saldo), r["von"]])
    return csv_antwort(_dateiname("kassenbuch-bargeld"), rows)


# ------------------------------------------------------------------ Sicherung
BACKUP_DIR = os.path.join(DATA_DIR, "sicherungen")
BACKUP_TAGE = int(_env_float("BACKUP_TAGE", "14"))
BACKUP_UHR = os.environ.get("BACKUP_UHR", "03:00")
BACKUP_RE = re.compile(r"^kaffeekasse-(auto|manuell|vor-einlesen)-\d{4}-\d{2}-\d{2}_\d{4,6}\.json$")


def sicherung_daten(c):
    """Alle Tabellen als EINE Momentaufnahme lesen.

    Alles läuft in einer Lesetransaktion: Dank WAL sieht sie genau den Stand von ihrem Beginn –
    Einkäufe, die währenddessen gebucht werden, sind entweder ganz drin oder gar nicht
    (sonst könnte z. B. ein Verkauf ohne seine Posten oder ein Guthaben ohne seine Buchung gesichert werden).
    Verkaufen bleibt währenddessen möglich, die Lesetransaktion blockiert niemanden."""
    daten = {"format": "kaffeekasse-sicherung", "version": 2, "erstellt": ms(), "tabellen": {}}
    eigene = not c.in_transaction
    if eigene:
        c.execute("BEGIN")
    try:
        for t in TABELLEN:
            rows = []
            for r in c.execute(f"SELECT * FROM {t}"):
                d = dict(r)
                if t == "produkte" and d.get("bild") is not None:
                    d["bild"] = base64.b64encode(d["bild"]).decode()
                rows.append(d)
            daten["tabellen"][t] = rows
    finally:
        if eigene:
            c.execute("COMMIT")
    return daten


def sicherung_schreiben(art):
    """Schreibt eine Sicherung nach /data/sicherungen (eigene Verbindung, auch aus dem Hintergrund-Thread)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)   # Transaktion steuert sicherung_daten selbst
    c.row_factory = sqlite3.Row
    try:
        daten = sicherung_daten(c)
    finally:
        c.close()
    name = f"kaffeekasse-{art}-{datetime.now(TZ):%Y-%m-%d_%H%M%S}.json"
    pfad = os.path.join(BACKUP_DIR, name)
    with open(pfad + ".tmp", "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False)
    os.replace(pfad + ".tmp", pfad)
    # aufräumen: je Art die neuesten BACKUP_TAGE Dateien behalten
    for a in ("auto", "manuell", "vor-einlesen"):
        alte = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith(f"kaffeekasse-{a}-") and n.endswith(".json"))
        for n in alte[:-max(1, BACKUP_TAGE)]:
            try:
                os.remove(os.path.join(BACKUP_DIR, n))
            except OSError:
                pass
    return name


def _backup_schleife():
    from datetime import timedelta
    try:
        h, m = (int(x) for x in BACKUP_UHR.split(":")[:2])
    except ValueError:
        h, m = 3, 0
    while True:
        jetzt = datetime.now(TZ)
        ziel = jetzt.replace(hour=h, minute=m, second=0, microsecond=0)
        if ziel <= jetzt:
            ziel += timedelta(days=1)
        time.sleep(max(1, (ziel - jetzt).total_seconds()))
        try:
            import fcntl
            os.makedirs(BACKUP_DIR, exist_ok=True)
            with open(os.path.join(BACKUP_DIR, ".lock"), "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue            # anderer Prozess sichert gerade
                heute = f"kaffeekasse-auto-{datetime.now(TZ):%Y-%m-%d}_"
                if not any(n.startswith(heute) for n in os.listdir(BACKUP_DIR)):
                    print("Kaffeekasse: automatische Sicherung", sicherung_schreiben("auto"), flush=True)
        except Exception as ex:          # noqa: BLE001
            print(f"Kaffeekasse: automatische Sicherung fehlgeschlagen – {ex}", flush=True)
        time.sleep(61)


if BACKUP_TAGE > 0 and os.environ.get("KAFFEEKASSE_TEST") != "1":
    import threading
    threading.Thread(target=_backup_schleife, daemon=True, name="auto-sicherung").start()


@app.get("/api/sicherung")
@api(INHABER)
def sicherung(u):
    with tx() as c:
        log(c, u["name"], "Sicherung heruntergeladen", "")
    name = f"kaffeekasse-sicherung-{datetime.now(TZ):%Y-%m-%d}.json"
    return Response(json.dumps(sicherung_daten(db()), ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/sicherungen")
@api(INHABER)
def sicherungen_liste(_u):
    out = []
    if os.path.isdir(BACKUP_DIR):
        for n in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if BACKUP_RE.match(n):
                st = os.stat(os.path.join(BACKUP_DIR, n))
                out.append({"name": n, "groesse": st.st_size, "zeit": int(st.st_mtime * 1000), "art": n.split("-")[1]})
    return jsonify(dateien=out, behalten=BACKUP_TAGE, uhr=BACKUP_UHR, ordner=BACKUP_DIR)


@app.post("/api/sicherungen")
@api(INHABER)
def sicherung_jetzt(u):
    name = sicherung_schreiben("manuell")
    with tx() as c:
        log(c, u["name"], "Sicherung auf dem Server erstellt", name)
    return jsonify(name=name)


def _backup_pfad(name):
    if not BACKUP_RE.match(name or ""):
        raise Fehler("Diese Sicherung gibt es nicht.", 404)
    pfad = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(pfad):
        raise Fehler("Diese Sicherung gibt es nicht.", 404)
    return pfad


@app.get("/api/sicherungen/<name>")
@api(INHABER)
def sicherung_datei(u, name):
    pfad = _backup_pfad(name)
    with tx() as c:
        log(c, u["name"], "Sicherung heruntergeladen", name)
    return send_from_directory(BACKUP_DIR, os.path.basename(pfad), as_attachment=True, mimetype="application/json")


def _bild_aus_sicherung(b64):
    """Bilder aus einer Sicherung genauso prüfen wie beim Hochladen. Was kein echtes Bild ist, fällt weg."""
    if not b64:
        return None, None
    try:
        return bild_pruefen(base64.b64decode(b64, validate=True))
    except (Fehler, ValueError, TypeError):
        return None, None


def bilder_pruefen_einmalig(c):
    """Einmalig nach dem Update: vorhandene Produktbilder durch die neue Prüfung schicken."""
    if c.execute("SELECT 1 FROM einstellungen WHERE key='bilderGeprueft'").fetchone():
        return
    for r in c.execute("SELECT id, bild FROM produkte WHERE bild IS NOT NULL").fetchall():
        try:
            neu, typ = bild_pruefen(r["bild"])
            c.execute("UPDATE produkte SET bild=?, bild_typ=?, bild_v=bild_v+1 WHERE id=?", (neu, typ, r["id"]))
        except Fehler:
            c.execute("UPDATE produkte SET bild=NULL, bild_typ=NULL, bild_v=bild_v+1 WHERE id=?", (r["id"],))
            print(f"Kaffeekasse: Produktbild #{r['id']} war kein gültiges Bild und wurde entfernt.", flush=True)
    c.execute("INSERT OR REPLACE INTO einstellungen(key, value) VALUES ('bilderGeprueft', 'true')")


def _einlesen(u, d):
    tabs = d.get("tabellen")
    if d.get("format") not in ("kaffeekasse-sicherung", "kiosk-sicherung") or not isinstance(tabs, dict) or "konten" not in tabs:
        raise Fehler("Die Datei passt nicht zu dieser App.")
    if not any(k.get("rolle") == "inhaber" and not k.get("entfernt") for k in tabs["konten"]):
        raise Fehler("In der Sicherung gibt es keinen Administrator-Zugang.")
    try:
        vorher = sicherung_schreiben("vor-einlesen")     # damit man zurück kann
    except OSError:
        vorher = ""
    with tx() as c:
        for t in reversed(TABELLEN):
            if t != "journal":
                c.execute(f"DELETE FROM {t}")
        for t in TABELLEN:
            if t == "journal":
                continue  # Protokoll bleibt erhalten, wird nie überschrieben
            spalten = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            for row in tabs.get(t, []):
                row = {k: v for k, v in row.items() if k in spalten}
                if t == "produkte" and "bild" in row:
                    row["bild"], row["bild_typ"] = _bild_aus_sicherung(row.get("bild"))
                if not row:
                    continue
                c.execute(f"INSERT INTO {t}({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        if not any("pin_hash" in k for k in tabs["konten"]):
            c.execute("UPDATE konten SET pin_hash=pw_hash, pin_muss_aendern=muss_aendern, pw_hash='!', muss_aendern=0 "
                      "WHERE rolle='kunde'")
        shops_sicherstellen(c)   # ältere Sicherungen ohne Shops → alles in den ersten Shop
        katalog_migrieren(c)     # ältere Sicherungen mit Sortiment je Shop → gemeinsamer Katalog
        namen_aufteilen(c)       # ältere Sicherungen mit nur einem Namensfeld
        c.execute("UPDATE konten SET mail=''")                     # E-Mail wird nicht mehr verwendet
        c.execute("DELETE FROM einstellungen WHERE key='meldeMail'")
        for k, v in DEFAULT_EINSTELLUNGEN.items():
            c.execute("INSERT OR IGNORE INTO einstellungen(key, value) VALUES (?,?)", (k, json.dumps(v)))
        c.execute("DELETE FROM sitzungen")
        log(c, u["name"], "Sicherung eingelesen", f"vom {zeit_txt(int(d.get('erstellt') or 0))}"
            + (f" · vorheriger Stand gesichert als {vorher}" if vorher else ""))
    _sitzung_beenden()


@app.post("/api/sicherung")
@api(INHABER)
def sicherung_einlesen(u):
    _einlesen(u, body())
    return jsonify(ok=True)


@app.post("/api/sicherungen/<name>/einlesen")
@api(INHABER)
def sicherung_datei_einlesen(u, name):
    with open(_backup_pfad(name), encoding="utf-8") as f:
        d = json.load(f)
    _einlesen(u, d)
    return jsonify(ok=True)


@app.post("/api/zuruecksetzen")
@api(INHABER)
def alles_zuruecksetzen(u):
    if body().get("bestaetigung") != "LÖSCHEN":
        raise Fehler("Bitte zur Bestätigung LÖSCHEN eingeben.")
    try:
        sicherung_schreiben("vor-einlesen")
    except OSError:
        pass
    with tx() as c:
        for t in ("posten", "verkaeufe", "kassenbuch", "lagerbuch", "kasse", "kassenpruefungen", "bestand", "produkte",
                  "shop_team", "shops"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM konten WHERE id != ?", (u["id"],))
        c.execute("UPDATE konten SET guthaben=0, stillgelegt=0 WHERE id=?", (u["id"],))   # Buchungen sind weg → Saldo auch
        c.execute("DELETE FROM sitzungen WHERE konto_id != ?", (u["id"],))
        c.execute("DELETE FROM einstellungen")
        for k, v in DEFAULT_EINSTELLUNGEN.items():
            c.execute("INSERT INTO einstellungen(key, value) VALUES (?,?)", (k, json.dumps(v)))
        _seed_produkte(c, [shops_sicherstellen(c)])
        log(c, u["name"], "Alles zurückgesetzt", "Anfangszustand, nur der eigene Administrator-Zugang bleibt")
    return jsonify(ok=True)


# ------------------------------------------------------------------ App zum Installieren (PWA)
@app.get("/manifest.webmanifest")
def manifest():
    name = einstellungen().get("shopName") or "Kaffeekasse"
    shop = request.args.get("shop", "")
    start = f"/?shop={int(shop)}" if shop.isdigit() else "/"     # App startet direkt im gewählten Shop
    m = {"name": name, "short_name": name[:12], "start_url": start, "scope": "/", "display": "standalone",
         "background_color": "#0E1116", "theme_color": "#0E1116", "lang": "de",
         "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                   {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                   {"src": "/static/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(m, ensure_ascii=False), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/qr.svg")
@api(TEAM)
def qr_code(_u):
    """QR-Code als SVG, z. B. für die Aushänge je Shop. Nur http(s)-Adressen."""
    from qr import qr_svg
    text = request.args.get("text", "")
    if not re.match(r"^https?://", text) or len(text) > 200:
        raise Fehler("Bitte eine Adresse mit http:// oder https:// (höchstens 200 Zeichen).")
    return Response(qr_svg(text), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(STATIC, "sw.js", mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/apple-touch-icon.png")
def apple_icon():
    return send_from_directory(STATIC, "icon-180.png", mimetype="image/png")


# Datenbank erst anlegen/prüfen, wenn alle Funktionen definiert sind
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

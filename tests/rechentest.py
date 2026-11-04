"""Rechentest der Kaffeekasse: spielt Einkäufe, Stornos, Kasse, Prüfung und Inventur über die echte
Schnittstelle durch und rechnet danach alle Summen direkt in der Datenbank nach.

Aufruf (im Projektordner, braucht nur Flask):   python3 tests/rechentest.py .
Läuft mit einer eigenen, leeren Datenbank in einem Temp-Ordner – die echten Daten werden nicht angefasst.
Ausgabe: OK / FEHLER / OFFEN (bekannte Kleinigkeit) / HINWEIS (so gewollt). Exit-Code 1 bei Fehlern."""
import os, sys, tempfile, threading, json, sqlite3

APP_DIR = sys.argv[1]
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kk-")
os.environ["KAFFEEKASSE_TEST"] = "1"
os.environ["LOGIN_SPERRE_IP"] = "0"
sys.path.insert(0, APP_DIR)
import app as A  # noqa: E402

ERG = []
os.environ.setdefault("TZ", "Europe/Berlin")


def ok(bed, text):
    ERG.append(("OK " if bed else "FEHLER ") + text)


def info(text):
    ERG.append("HINWEIS " + text)


def offen(bed, text):
    """Bekannte Kleinigkeit, noch nicht behoben."""
    ERG.append(("OK " if bed else "OFFEN ") + text)


class Client:
    def __init__(self):
        self.c = A.app.test_client()
        self.csrf = self.c.get("/api/me").get_json()["csrf"]
        self.sitz = ""

    def req(self, m, url, data=None):
        h = {"X-CSRF": self.csrf}
        if self.sitz:
            h["X-Sitzung"] = self.sitz
        r = self.c.open(url, method=m, json=data, headers=h)
        if r.headers.get("X-Sitzung"):
            self.sitz = r.headers["X-Sitzung"]
        j = r.get_json(silent=True)
        if isinstance(j, dict) and j.get("csrf"):
            self.csrf = j["csrf"]
        return r.status_code, j

    def login(self, nr, geheim):
        s, j = self.req("POST", "/api/login", {"nummer": nr, "passwort": geheim})
        assert s == 200, (s, j)
        return j

def liste(j):
    return j if isinstance(j, list) else j["verkaeufe"]

# ---------- Einrichtung
adm = Client()
s, j = adm.req("POST", "/api/einrichtung", {"vorname": "Max", "nachname": "Mustermann", "nummer": "10000001",
                                              "passwort": "AdminPasswort1", "pin": "4826", "shopName": "Strichliste",
                                              "ersterShop": "Halle 1"})
ok(s == 200, f"Einrichtung ({s})")
st = adm.req("GET", "/api/state")[1]
h1 = st["shops"][0]["id"]
cola = next(p for p in st["produkte"] if p["name"].startswith("Coca-Cola 0"))["id"]
mars = next(p for p in st["produkte"] if p["name"] == "Mars")["id"]

# zweite Halle mit gleichem Sortiment
s, j = adm.req("POST", "/api/shops", {"name": "Halle 2", "kopieVon": h1})
h2 = j["id"]

# Ware: Eingang ins Lager mit EK, auffüllen
adm.req("POST", f"/api/produkte/{cola}/eingang", {"shop": h1, "menge": 24, "ek": "0,55", "ort": "lager"})
adm.req("POST", f"/api/produkte/{cola}/auffuellen", {"shop": h1, "menge": 10})
adm.req("POST", f"/api/produkte/{mars}/eingang", {"shop": h1, "menge": 6, "ek": "0,62", "ort": "kuehlschrank"})
adm.req("POST", f"/api/produkte/{cola}/eingang", {"shop": h2, "menge": 1, "ek": "0,60", "ort": "kuehlschrank"})

# Mitglieder mit Startguthaben
s, j = adm.req("POST", "/api/konten", {"vorname": "Kai", "nachname": "Wagner", "nummer": "20000001", "start": "20"})
kai, kai_pin = j["konto"]["id"], j["einmalPin"]
s, j = adm.req("POST", "/api/konten", {"vorname": "Sara", "nachname": "Lehmann", "nummer": "20000002", "start": "0"})
sara, sara_pin = j["konto"]["id"], j["einmalPin"]
adm.req("POST", f"/api/konten/{sara}/buchen", {"art": "auf", "betrag": "1,50"})


def mitglied(nr, einmal, neu):
    c = Client()
    j = c.login(nr, einmal)
    s, j = c.req("POST", "/api/passwort", {"neu": neu})
    assert s == 200, j
    return c

k1 = mitglied("20000001", kai_pin, "7391")
k2 = mitglied("20000002", sara_pin, "5182")

# Einkäufe
s, j = k1.req("POST", "/api/kaufen", {"shop": h1, "posten": [{"pid": cola, "menge": 3}, {"pid": mars, "menge": 2}]})
ok(s == 200 and abs(j["summe"] - (3 * 1.2 + 2 * 1.1)) < 1e-9, f"Kauf Kai: Summe {j and j.get('summe')} = 5,80")
beleg_kai = j["beleg"]
s, j = k2.req("POST", "/api/kaufen", {"shop": h1, "posten": [{"pid": cola, "menge": 1}]})
ok(s == 200, "Kauf Sara 1 Cola (Guthaben 1,50 → 0,30)")

# Schuldengrenze: 30 €
s, j = k2.req("POST", "/api/kaufen", {"shop": h1, "posten": [{"pid": cola, "menge": 6}]})
ok(s == 200 and abs(j["guthaben"] - (0.3 - 7.2)) < 1e-9, f"Kauf ins Minus erlaubt bis Grenze → {j and j.get('guthaben')}")

# Teilstorno: 1 Posten von Kai (die Mars)
verk = liste(adm.req("GET", "/api/verkaeufe?von=0")[1])
v_kai = next(v for v in verk if v["beleg"] == beleg_kai)
p_mars = next(p for p in v_kai["posten"] if p["pid"] == mars)
s, j = adm.req("POST", "/api/storno", {"posten": p_mars["id"]})
ok(s == 200 and abs(j["betrag"] - 2.2) < 1e-9, "Teilstorno 2× Mars = 2,20 zurück")
s, j = adm.req("POST", "/api/storno", {"posten": p_mars["id"]})
ok(s == 409, "Doppeltes Storno wird abgelehnt")

# Kasse: Ausgabe, Einlage, Storno der Einlage, Auszahlung, Prüfung mit Differenz
adm.req("POST", "/api/kasse", {"art": "ausgabe", "betrag": "12,40", "zweck": "Wareneinkauf Getränke", "kategorie": "Wareneinkauf"})
s, j = adm.req("POST", "/api/kasse", {"art": "einlage", "betrag": "5", "zweck": "Spende"})
einlage_id = j["id"]
adm.req("POST", f"/api/kasse/{einlage_id}/storno", {})
adm.req("POST", f"/api/konten/{kai}/buchen", {"art": "ab", "betrag": "2", "bar": True, "notiz": "Auszahlung"})
adm.req("POST", f"/api/konten/{kai}/buchen", {"art": "ab", "betrag": "0,50", "bar": False, "notiz": "Korrektur"})
kv = adm.req("GET", "/api/kasse?von=0")[1]
soll_kasse = 2000 + 150 - 1240 - 200
ok(round(kv["stand"] * 100) == soll_kasse, f"Kassenstand {kv['stand']:.2f} = {soll_kasse/100:.2f}")
s, j = adm.req("POST", "/api/kasse/pruefung", {"ist": "6,60", "pruefer2": "Erika Beispiel"})
ok(s == 200 and round(j["differenz"] * 100) == 660 - soll_kasse, f"Prüfung Differenz {j['differenz']:.2f}")
kv = adm.req("GET", "/api/kasse?von=0")[1]
ok(round(kv["stand"] * 100) == 660, f"Kassenstand nach Differenzbuchung {kv['stand']:.2f} = 6,60")
saldo_ok = all(round(kv["eintraege"][i]["saldo"] * 100) == round((kv["eintraege"][i + 1]["saldo"] + kv["eintraege"][i]["betrag"]) * 100)
               for i in range(len(kv["eintraege"]) - 1))
ok(saldo_ok and round(kv["eintraege"][0]["saldo"] * 100) == 660, "Laufender Kassenstand im Kassenbuch stimmt Zeile für Zeile")
ok(kv["seitPruefung"]["buchungen"] == 0,
   f"„Seit letzter Prüfung“ zählt die eigene Differenzbuchung nicht mit (gezählt: {kv['seitPruefung']['buchungen']})")

# Inventur: Kühlschrank Cola gezählt 1 weniger
db = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "kaffeekasse.db")); db.row_factory = sqlite3.Row
lg = db.execute("SELECT lager FROM bestand WHERE produkt_id=? AND shop_id=?", (cola, h1)).fetchone()[0]
s, j = adm.req("POST", "/api/inventur", {"shop": h1, "ort": "lager", "zaehlung": [{"pid": cola, "ist": lg - 1}]})
ok(s == 200 and abs(j["wert"] + 0.60) < 1e-9, f"Inventur: Schwund 1 Cola, bewertet mit zuletzt gezahltem EK 0,60 → {j and j.get('wert')}")

# Gleichzeitige Käufe des letzten Stücks in Halle 2
res = []
def kaufe(cl):
    res.append(cl.req("POST", "/api/kaufen", {"shop": h2, "posten": [{"pid": cola, "menge": 1}]})[0])
t = [threading.Thread(target=kaufe, args=(c,)) for c in (k1, k2)]
[x.start() for x in t]; [x.join() for x in t]
b2 = db.execute("SELECT menge FROM bestand WHERE produkt_id=? AND shop_id=?", (cola, h2)).fetchone()[0]
ok(sorted(res) == [200, 409] and b2 == 0, f"Letzte Dose gleichzeitig an 2 Geräten: {sorted(res)}, Bestand danach {b2}")

# Konto mit Restguthaben löschen
s, j = adm.req("POST", "/api/konten", {"vorname": "Tom", "nachname": "Becker", "nummer": "20000003", "start": "10"})
tom = j["konto"]["id"]
adm.req("DELETE", f"/api/konten/{tom}")

# ---------- Nachrechnen direkt in der Datenbank
fehler_konten = []
for k in db.execute("SELECT * FROM konten"):
    kb = db.execute("SELECT COALESCE(SUM(betrag),0) FROM kassenbuch WHERE konto_id=?", (k["id"],)).fetchone()[0]
    ps = db.execute("SELECT COALESCE(SUM(p.preis*p.menge),0) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                    "WHERE v.konto_id=?", (k["id"],)).fetchone()[0]
    if kb - ps != k["guthaben"]:
        fehler_konten.append((k["name"], k["guthaben"], kb - ps))
ok(not fehler_konten, f"Guthaben = Kontobuchungen − Einkäufe für jedes Konto {fehler_konten or ''}")

bad = db.execute("SELECT v.id, v.summe, (SELECT COALESCE(SUM(preis*menge),0) FROM posten WHERE verkauf_id=v.id AND storno=0) s "
                 "FROM verkaeufe v WHERE v.summe != s").fetchall()
ok(not bad, "Belegsumme = Summe der nicht stornierten Posten")

kasse_sum = db.execute("SELECT SUM(betrag) FROM kasse").fetchone()[0]
ok(kasse_sum == 660 + 1000, f"Kasse nach Startguthaben Tom: {kasse_sum/100:.2f} (Restguthaben bleibt als Bargeld in der Kasse)")

# Bestand Kühlschrank Halle 1 Cola: 10 aufgefüllt − 3 − 1 − 6 − 1 (Inventur) = −1? → prüfen
kc = db.execute("SELECT menge, lager FROM bestand WHERE produkt_id=? AND shop_id=?", (cola, h1)).fetchone()
ok(tuple(kc) == (10 - 3 - 1 - 6, 13), f"Bestand Cola Halle 1: Kühlschrank/Lager {tuple(kc)}")
km = db.execute("SELECT menge FROM bestand WHERE produkt_id=? AND shop_id=?", (mars, h1)).fetchone()[0]
ok(km == 6, f"Bestand Mars nach Kauf 2 und Storno 2: {km}")

# Lagerbuch erklärt den Kühlschrank-Bestand?
lb = db.execute("SELECT COALESCE(SUM(menge),0) FROM lagerbuch WHERE produkt_id=? AND shop_id=? "
                "AND ort IN ('kuehlschrank','umlagerung')", (cola, h1)).fetchone()[0]
info(f"Lagerbuch enthält keine Verkäufe/Stornos (gewollt): Lagerbuch {lb}, Kühlschrank {kc[0]}")

# Kennzahlen
kz = adm.req("GET", f"/api/kennzahlen?von=0&shop={h1}")[1]
ums = db.execute("SELECT SUM(preis*menge) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id WHERE storno=0 AND shop_id=?", (h1,)).fetchone()[0]
ok(round(kz["umsatz"] * 100) == ums, f"Umsatz Halle 1 {kz['umsatz']:.2f}")
roh = db.execute("SELECT SUM((preis-ek)*menge) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id WHERE storno=0 AND ek IS NOT NULL AND shop_id=?", (h1,)).fetchone()[0]
ok(round(kz["rohertrag"] * 100) == roh, f"Rohertrag Halle 1 {kz['rohertrag']:.2f}")
ok(round(kz["schwund"]["wert"] * 100) == 60, f"Schwund-Wert {kz['schwund']['wert']:.2f}")

# Vier-Augen: Shopverantwortliche storniert eigenen Einkauf?
s, j = adm.req("POST", "/api/konten", {"vorname": "Sabine", "nachname": "Krüger", "nummer": "30000001", "rolle": "personal"})
sab = j["konto"]["id"]; sab_pw, sab_pin = j["einmalPw"], j["einmalPin"]
adm.req("PUT", f"/api/konten/{sab}/shops", {"shops": [h1]})
adm.req("POST", f"/api/konten/{sab}/buchen", {"art": "auf", "betrag": "5"})
sp = mitglied("30000001", sab_pin, "8264")
s, j = sp.req("POST", "/api/kaufen", {"shop": h1, "posten": [{"pid": mars, "menge": 1}]})
sv = Client(); sv.login("30000001", sab_pw); sv.req("POST", "/api/passwort", {"neu": "SabinePasswort9"})
vs = liste(sv.req("GET", "/api/verkaeufe?von=0")[1])
eig = next(v for v in vs if v["kontoId"] == sab)
s, j = sv.req("POST", "/api/storno", {"posten": eig["posten"][0]["id"]})
info(f"Shopverantwortliche darf eigenen Einkauf stornieren (bewusst erlaubt, Antwort {s})")
s, j = sv.req("POST", f"/api/konten/{sab}/buchen", {"art": "auf", "betrag": "5"})
ok(s == 403, "Shopverantwortliche kann eigenes Guthaben nicht aufladen")

# Rundung von Beträgen
for eingabe, cent in [("0,29", 29), ("2,67", 267), ("0,1", 10), ("1e2", 10000), (1.2, 120), ("12", 1200)]:
    ok(A.cents(eingabe) == cent, f"Betrag „{eingabe}“ → {A.cents(eingabe)} Cent (erwartet {cent})")
for eingabe in ("1,005", "2.675", "abc", "nan", "inf"):
    try:
        A.cents(eingabe); ok(False, f"Betrag „{eingabe}“ wird abgelehnt")
    except A.Fehler as f:
        ok(True, f"Betrag „{eingabe}“ wird abgelehnt: {f.text}")
s, j = adm.req("POST", "/api/kasse", {"art": "einlage", "betrag": "1,005", "zweck": "Test"})
ok(s == 400, f"Kasse lehnt 1,005 € ab ({s})")

# ---------- „Wie letztes Mal“
st_kai = k1.req("GET", "/api/state")[1]
le = st_kai["letzterEinkauf"].get(str(h1)) or []
ok(len(le) >= 1 and all(x["menge"] >= 1 for x in le), f"Letzter Einkauf von Kai in Halle 1 wird geliefert: {le}")
ok(all(x["pid"] != mars for x in le), "Stornierte Posten (die Mars) sind nicht im letzten Einkauf enthalten")

# ---------- Bestellvorschlag
s, bv = adm.req("GET", f"/api/bestellvorschlag?tage=14&basis=28&shop={h1}")
cola_bv = next((z for z in bv["produkte"] if z["pid"] == cola), None)
verkauft_cola = db.execute("SELECT SUM(menge) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                           "WHERE p.pid=? AND p.storno=0 AND v.shop_id=?", (cola, h1)).fetchone()[0]
vorrat = sum(db.execute("SELECT MAX(menge,0)+MAX(lager,0) FROM bestand WHERE produkt_id=? AND shop_id=?", (cola, h1)).fetchone())
import math
erwartet = max(0, math.ceil(verkauft_cola / 28 * 14) + bv["puffer"] - vorrat)
ok((cola_bv["bedarf"] if cola_bv else 0) == erwartet,
   f"Bestellvorschlag Cola Halle 1: {verkauft_cola} verkauft, Vorrat {vorrat} → bestellen {cola_bv and cola_bv['bedarf']} (erwartet {erwartet})")

# ---------- Abschluss
s, ab = adm.req("GET", f"/api/abschluss?von=0")
ok(s == 200, "Abschluss abrufbar")
kasse_db = db.execute("SELECT SUM(betrag) FROM kasse").fetchone()[0]
ok(round(ab["kasse"]["ende"] * 100) == kasse_db and ab["kasse"]["anfang"] == 0, f"Abschluss: Kasse 0,00 → {ab['kasse']['ende']:.2f}")
ok(round(sum(a["summe"] for a in ab["kasse"]["arten"]) * 100) == kasse_db, "Abschluss: Kassenbewegungen ergeben den Endbestand")
g_db = db.execute("SELECT SUM(CASE WHEN guthaben>0 THEN guthaben END), SUM(CASE WHEN guthaben<0 THEN guthaben END) FROM konten").fetchone()
ok(round(ab["konten"]["guthaben"] * 100) == (g_db[0] or 0) and round(ab["konten"]["schulden"] * 100) == (g_db[1] or 0),
   f"Abschluss: Guthaben/Schulden zum Stichtag = Kontostände ({ab['konten']['guthaben']:.2f} / {ab['konten']['schulden']:.2f})")
ums_db = db.execute("SELECT SUM(preis*menge) FROM posten WHERE storno=0").fetchone()[0]
ok(round(sum(x["umsatz"] for x in ab["verkauf"]["jeShop"]) * 100) == ums_db, "Abschluss: Umsatz aller Shops stimmt")
# Stichtag in der Vergangenheit: vor allen Buchungen → alles 0
s, ab0 = adm.req("GET", "/api/abschluss?von=0&bis=1")
ok(ab0["konten"]["guthaben"] == 0 and ab0["kasse"]["ende"] == 0, "Abschluss zu einem Stichtag vor allen Buchungen ist leer")
# Stichtag mitten im Betrieb: vor dem Teilstorno und der Auszahlung → Kai muss den damaligen Stand haben
t_storno = db.execute("SELECT MIN(zeit) FROM kassenbuch WHERE art='Storno'").fetchone()[0]
s, abm = adm.req("GET", f"/api/abschluss?von=0&bis={t_storno}")
kai_damals = (db.execute("SELECT COALESCE(SUM(betrag),0) FROM kassenbuch WHERE konto_id=? AND zeit < ?", (kai, t_storno)).fetchone()[0]
              - db.execute("SELECT COALESCE(SUM(p.preis*p.menge),0) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                           "WHERE v.konto_id=? AND v.zeit < ?", (kai, t_storno)).fetchone()[0])
alle_damals = {}
for k_ in db.execute("SELECT id FROM konten"):
    i = k_[0]
    alle_damals[i] = (db.execute("SELECT COALESCE(SUM(betrag),0) FROM kassenbuch WHERE konto_id=? AND zeit < ?", (i, t_storno)).fetchone()[0]
                      - db.execute("SELECT COALESCE(SUM(p.preis*p.menge),0) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                                   "WHERE v.konto_id=? AND v.zeit < ?", (i, t_storno)).fetchone()[0])
plus_damals = sum(v for v in alle_damals.values() if v > 0)
ok(round(abm["konten"]["guthaben"] * 100) == plus_damals,
   f"Abschluss zum Stichtag vor dem Storno: Guthaben {abm['konten']['guthaben']:.2f} = damals {plus_damals/100:.2f} (Kai damals {kai_damals/100:.2f})")

# ---------- Konto stilllegen
kai_vorher = db.execute("SELECT guthaben FROM konten WHERE id=?", (kai,)).fetchone()[0]
kasse_vorher = db.execute("SELECT SUM(betrag) FROM kasse").fetchone()[0]
s, j = adm.req("PUT", f"/api/konten/{kai}/stilllegen", {"an": True, "auszahlen": True})
ok(s == 200, "Kai stilllegen mit Auszahlung")
kai_nach = db.execute("SELECT guthaben, stillgelegt FROM konten WHERE id=?", (kai,)).fetchone()
kasse_nach = db.execute("SELECT SUM(betrag) FROM kasse").fetchone()[0]
ok(kai_nach[0] == (0 if kai_vorher > 0 else kai_vorher) and kai_nach[1] > 0,
   f"Stillgelegt, Guthaben {kai_vorher/100:.2f} → {kai_nach[0]/100:.2f}")
ok(kasse_vorher - kasse_nach == max(0, kai_vorher), f"Kasse um das ausgezahlte Restguthaben vermindert ({(kasse_vorher-kasse_nach)/100:.2f})")
s, j = k1.req("GET", "/api/state")
ok(s == 401, "Laufende Anmeldung des stillgelegten Kontos ist beendet")
c_neu = Client()
s, j = c_neu.req("POST", "/api/login", {"nummer": "20000001", "passwort": "0000"})
ok(s == 401 and "stillgelegt" not in (j or {}).get("fehler", ""), "Falsche PIN verrät nicht, dass das Konto stillgelegt ist")
s, j = c_neu.req("POST", "/api/login", {"nummer": "20000001", "passwort": "7391"})
ok(s == 403 and "stillgelegt" in j["fehler"], f"Richtige PIN: Anmeldung abgelehnt mit Hinweis ({s})")
adm.req("PUT", f"/api/konten/{kai}/stilllegen", {"an": False})
s, j = c_neu.req("POST", "/api/login", {"nummer": "20000001", "passwort": "7391"})
ok(s == 200, "Nach dem Reaktivieren klappt die Anmeldung wieder")
fehler_konten = []
for k in db.execute("SELECT * FROM konten"):
    kb = db.execute("SELECT COALESCE(SUM(betrag),0) FROM kassenbuch WHERE konto_id=?", (k["id"],)).fetchone()[0]
    ps = db.execute("SELECT COALESCE(SUM(p.preis*p.menge),0) FROM posten p JOIN verkaeufe v ON v.id=p.verkauf_id "
                    "WHERE v.konto_id=?", (k["id"],)).fetchone()[0]
    if kb - ps != k["guthaben"]:
        fehler_konten.append(k["name"])
ok(not fehler_konten, f"Nach Stilllegen/Auszahlung: Guthaben = Buchungen − Einkäufe für jedes Konto {fehler_konten or ''}")

# ---------- Einstellungen
s, j = adm.req("PUT", "/api/einstellungen", {"warnGuthaben": "7,5", "datenschutzKontakt": "Max Mustermann, App. 12345", "stil": "hangar"})
ok(s == 200 and j["einstellungen"]["warnGuthaben"] == 7.5 and j["einstellungen"]["stil"] == "hangar", "Einstellungen Warngrenze/Kontakt/Design speichern")
s, j = adm.req("PUT", "/api/einstellungen", {"stil": "gibtsnicht"})
ok(s == 400, "Unbekanntes Design wird abgelehnt")
ok(Client().req("GET", "/api/me")[1]["datenschutzKontakt"] == "Max Mustermann, App. 12345", "Datenschutz-Kontakt auch ohne Anmeldung lesbar")

# Zurücksetzen: eigenes Guthaben?
adm.req("POST", f"/api/konten/{sab}/buchen", {"art": "auf", "betrag": "1"})
me_id = db.execute("SELECT id FROM konten WHERE nummer='10000001'").fetchone()[0]
s, _ = adm.req("POST", f"/api/kaufen", {"shop": h1, "posten": [{"pid": mars, "menge": 1}]})
adm2 = Client(); adm2.login("10000001", "4826")
adm2.req("POST", "/api/kaufen", {"shop": h1, "posten": [{"pid": mars, "menge": 1}]})
adm.req("POST", "/api/zuruecksetzen", {"bestaetigung": "LÖSCHEN"})
g_nach = db.execute("SELECT guthaben FROM konten WHERE id=?", (me_id,)).fetchone()[0]
ok(g_nach == 0, f"Nach „Alles zurücksetzen“ ist das eigene Guthaben 0 (ist {g_nach/100:.2f} €)")

print("\n".join(ERG))
fehler = sum(1 for e in ERG if e.startswith("FEHLER"))
print(f"\n{len(ERG)} Prüfungen, {fehler} Fehler")
sys.exit(1 if fehler else 0)

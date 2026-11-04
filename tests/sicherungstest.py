"""Sicherungstest: schreibt eine Sicherung, während ununterbrochen eingekauft wird, und prüft, ob die Sicherung in sich stimmig ist.
Aufruf im Projektordner: python3 tests/sicherungstest.py .   (eigene Temp-Datenbank, dauert ca. 1 Minute)"""
import os, sys, tempfile, time, random, sqlite3, threading, json

APP_DIR = sys.argv[1]
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kks-")
os.environ["KAFFEEKASSE_TEST"] = "1"
sys.path.insert(0, APP_DIR)
import app as A  # noqa: E402

DBP = os.path.join(os.environ["DATA_DIR"], "kaffeekasse.db")
db = sqlite3.connect(DBP, isolation_level=None)
jetzt = int(time.time() * 1000)
db.execute("BEGIN")
konten = []
for i in range(50):
    konten.append(db.execute("INSERT INTO konten(nummer,name,rolle,pw_hash,pin_hash,angelegt,nachname,guthaben) "
                             "VALUES (?,?,?,?,?,?,?,0)", (f"2{i:07d}", f"M{i}", "kunde", "!", "x", jetzt, f"M{i}")).lastrowid)
for _ in range(400_000):   # Altbestand, damit die Sicherung ein paar Sekunden braucht
    k = random.choice(konten)
    vid = db.execute("INSERT INTO verkaeufe(konto_id,name,zeit,summe,shop_id) VALUES (?,?,?,120,1)", (k, "x", jetzt)).lastrowid
    db.execute("INSERT INTO posten(verkauf_id,pid,name,kat,preis,menge) VALUES (?,1,'Cola','S',120,1)", (vid,))
    db.execute("UPDATE konten SET guthaben=guthaben-120 WHERE id=?", (k,))
db.execute("COMMIT")

stop = False
gebucht = [0]
def einkaufen():
    c = sqlite3.connect(DBP, isolation_level=None, timeout=30)
    while not stop:
        k = random.choice(konten)
        c.execute("BEGIN IMMEDIATE")
        vid = c.execute("INSERT INTO verkaeufe(konto_id,name,zeit,summe,shop_id) VALUES (?,?,?,120,1)", (k, "x", jetzt)).lastrowid
        c.execute("INSERT INTO posten(verkauf_id,pid,name,kat,preis,menge) VALUES (?,1,'Cola','S',120,1)", (vid,))
        c.execute("UPDATE konten SET guthaben=guthaben-120 WHERE id=?", (k,))
        c.execute("COMMIT")
        gebucht[0] += 1
        pass

t = threading.Thread(target=einkaufen); t.start()
time.sleep(0.3)
start = time.perf_counter()
name = A.sicherung_schreiben("manuell")
dauer = time.perf_counter() - start
stop = True; t.join()

d = json.load(open(os.path.join(A.BACKUP_DIR, name)))["tabellen"]
posten_je_verkauf = {}
for p in d["posten"]:
    posten_je_verkauf[p["verkauf_id"]] = posten_je_verkauf.get(p["verkauf_id"], 0) + p["preis"] * p["menge"]
kauf_je_konto = {}
for v in d["verkaeufe"]:
    kauf_je_konto[v["konto_id"]] = kauf_je_konto.get(v["konto_id"], 0) + posten_je_verkauf.get(v["id"], 0)
ohne_posten = sum(1 for v in d["verkaeufe"] if v["id"] not in posten_je_verkauf)
falsch = sum(1 for k in d["konten"] if k["guthaben"] != -kauf_je_konto.get(k["id"], 0))
print(f"Sicherung {dauer:.1f} s, währenddessen {gebucht[0]} Einkäufe gebucht")
print(f"  Verkäufe ohne Posten in der Sicherung: {ohne_posten}")
print(f"  Konten, deren Guthaben nicht zu den gesicherten Einkäufen passt: {falsch} von {len(d['konten'])}")
print("  → Sicherung STIMMIG" if not ohne_posten and not falsch else "  → Sicherung WIDERSPRÜCHLICH")

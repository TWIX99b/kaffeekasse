"""Sicherheitstest Produktbilder: Es darf nur ein echtes, neu erzeugtes Bild gespeichert und ausgeliefert werden.

Aufruf:  python tests/bildtest.py <Pfad zum Projekt>
"""
import base64
import io
import os
import sqlite3
import sys
import tempfile

APP = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
DATA = tempfile.mkdtemp(prefix="kk-bild-")
os.environ.update(DATA_DIR=DATA, KAFFEEKASSE_TEST="1", LOGIN_SPERRE_IP="0")
sys.path.insert(0, APP)
import app as A  # noqa: E402
from PIL import Image  # noqa: E402

fehler = []
ok = 0


def pruefe(bedingung, text):
    global ok
    if bedingung:
        ok += 1
        print("OK   ", text)
    else:
        fehler.append(text)
        print("FEHLER", text)


def b64(x):
    return base64.b64encode(x).decode()


def bild(fmt="JPEG", groesse=(60, 40), modus="RGB", **kw):
    buf = io.BytesIO()
    Image.new(modus, groesse, (200, 100, 50) if modus == "RGB" else (0, 0, 0, 0)).save(buf, fmt, **kw)
    return buf.getvalue()


c = A.app.test_client()
csrf = c.get("/api/me").get_json()["csrf"]
r = c.post("/api/einrichtung", json={"nachname": "Admin", "nummer": "10000001", "passwort": "AdminPasswort1",
                                     "pin": "4826"}, headers={"X-CSRF": csrf})
H = {"X-CSRF": r.get_json()["csrf"], "X-Sitzung": r.headers["X-Sitzung"]}
PR = {p["id"]: p for p in c.get("/api/state", headers=H).get_json()["produkte"]}
pids = sorted(PR)


def hochladen(pid, roh, typ="image/jpeg"):
    felder = {k: PR[pid][k] for k in ("name", "kat", "preis", "form", "ml")}
    return c.put(f"/api/produkte/{pid}", json={**felder, "bild": f"data:{typ};base64," + b64(roh)}, headers=H)


# Echte Fotos funktionieren weiter
exif = Image.new("RGB", (1, 1)).getexif()
exif[0x010F] = "Handyhersteller"
r = hochladen(pids[0], bild(groesse=(3000, 2000), exif=exif))
g = c.get(f"/bild/{pids[0]}")
im = Image.open(io.BytesIO(g.data))
pruefe(r.status_code == 200 and g.status_code == 200, "Echtes JPEG wird angenommen")
pruefe(max(im.size) <= A.BILD_MAX_KANTE, f"Großes Foto wird verkleinert ({im.size[0]}×{im.size[1]})")
pruefe(not im.getexif(), "Metadaten (EXIF) werden entfernt")
pruefe(g.headers.get("X-Content-Type-Options") == "nosniff", "Bild mit nosniff ausgeliefert")
pruefe("sandbox" in (g.headers.get("Content-Security-Policy") or ""), "Bild in CSP-Sandbox ausgeliefert")

r = hochladen(pids[1], bild("PNG", modus="RGBA"), "image/png")
pruefe(r.status_code == 200 and c.get(f"/bild/{pids[1]}").mimetype == "image/png", "PNG mit Transparenz bleibt PNG")
r = hochladen(pids[2], bild("WEBP"), "image/webp")
pruefe(r.status_code == 200 and c.get(f"/bild/{pids[2]}").mimetype in A.BILD_TYPEN, "WebP wird angenommen")

# Angriffe über das normale Hochladen
r = hochladen(pids[3], b"<html><script>alert(1)</script></html>", "image/png")
pruefe(r.status_code == 400, "HTML als PNG getarnt wird abgelehnt")
r = hochladen(pids[3], bild() + b"<script>alert(1)</script>")
pruefe(r.status_code == 200 and b"<script>" not in c.get(f"/bild/{pids[3]}").data,
       "Angehängtes Skript an echtem JPEG wird entfernt")
r = hochladen(pids[4], bild("GIF"), "image/png")
pruefe(r.status_code == 400, "Anderes Format (GIF) wird abgelehnt")
buf = io.BytesIO()
Image.new("L", (20000, 20000)).save(buf, "PNG", optimize=True)
r = hochladen(pids[4], buf.getvalue(), "image/png")
pruefe(r.status_code == 400, f"Pixel-Bombe ({len(buf.getvalue()) // 1024} KB, 20000×20000) wird abgelehnt")
r = hochladen(pids[4], bild()[:200])
pruefe(r.status_code == 400, "Abgeschnittenes JPEG wird abgelehnt")

# Angriff über eine präparierte Sicherung
sich = c.get("/api/sicherung", headers=H).get_json()
for p in sich["tabellen"]["produkte"]:
    if p["id"] == pids[5]:
        p["bild"], p["bild_typ"] = b64(b"<html><script>alert(1)</script></html>"), "text/html"
    if p["id"] == pids[6]:
        p["bild"], p["bild_typ"] = b64(b"alert(1)"), "text/javascript"
    if p["id"] == pids[7]:
        p["bild"], p["bild_typ"] = b64(bild()), "text/html"   # echtes Bild, falscher Typ
r = c.post("/api/sicherung", json=sich, headers=H)
pruefe(r.status_code == 200, "Präparierte Sicherung lässt sich einlesen (ohne die schadhaften Bilder)")
pruefe(c.get(f"/bild/{pids[5]}").status_code == 404, "HTML aus der Sicherung wird nicht ausgeliefert")
pruefe(c.get(f"/bild/{pids[6]}").status_code == 404, "JavaScript aus der Sicherung wird nicht ausgeliefert")
pruefe(c.get(f"/bild/{pids[7]}").mimetype == "image/jpeg", "Echtes Bild mit falschem Typ wird korrigiert")
pruefe(c.get(f"/bild/{pids[0]}").status_code == 200, "Gute Bilder bleiben beim Einlesen erhalten")

# Bestehende Datenbank (vor dem Update) mit schadhaftem Bild wird beim Start bereinigt
db = sqlite3.connect(A.DB_PATH)
db.execute("UPDATE produkte SET bild=?, bild_typ='text/html' WHERE id=?", (b"<script>alert(1)</script>", pids[8]))
db.execute("UPDATE produkte SET bild=?, bild_typ='image/png' WHERE id=?", (b"kein Bild", pids[9]))
db.execute("DELETE FROM einstellungen WHERE key='bilderGeprueft'")
db.commit()
db.close()
A.init_db()
db = sqlite3.connect(A.DB_PATH)
reste = db.execute("SELECT COUNT(*) FROM produkte WHERE id IN (?,?) AND bild IS NOT NULL", (pids[8], pids[9])).fetchone()[0]
gut = db.execute("SELECT bild_typ FROM produkte WHERE id=?", (pids[0],)).fetchone()[0]
db.close()
pruefe(reste == 0, "Alte schadhafte Bilder werden beim Start entfernt")
pruefe(gut == "image/jpeg", "Alte gute Bilder bleiben beim Start erhalten")

print(f"\n{ok} Prüfungen OK, {len(fehler)} Fehler")
sys.exit(1 if fehler else 0)

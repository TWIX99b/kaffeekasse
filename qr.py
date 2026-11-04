"""Kleiner QR-Code-Erzeuger ohne Fremdbibliothek (Byte-Modus, Fehlerkorrektur M, Version 1–10).

Reicht für Adressen bis ca. 210 Zeichen. Aufbau nach ISO/IEC 18004 (angelehnt an Project Nayuki).
qr_svg(text) liefert ein SVG, qr_matrix(text) die Module als Liste von Listen (True = schwarz).
"""

# Version: (EC-Codewörter je Block, [(Anzahl Blöcke, Datencodewörter je Block), …])  – Stufe M
_BLOECKE = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_AUSRICHTUNG = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
                7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_EC_M = 0  # Formatbits der Stufe M


def _gf_mal(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(grad):
    erg = [0] * (grad - 1) + [1]
    wurzel = 1
    for _ in range(grad):
        for j in range(len(erg)):
            erg[j] = _gf_mal(erg[j], wurzel)
            if j + 1 < len(erg):
                erg[j] ^= erg[j + 1]
        wurzel = _gf_mal(wurzel, 0x02)
    return erg


def _rs_rest(daten, divisor):
    erg = [0] * len(divisor)
    for b in daten:
        faktor = b ^ erg.pop(0)
        erg.append(0)
        for i, koeff in enumerate(divisor):
            erg[i] ^= _gf_mal(koeff, faktor)
    return erg


def _codewoerter(daten, version):
    ec, gruppen = _BLOECKE[version]
    kapazitaet = sum(n * k for n, k in gruppen)
    bits = []

    def schreibe(wert, laenge):
        bits.extend((wert >> i) & 1 for i in range(laenge - 1, -1, -1))

    schreibe(0b0100, 4)                                   # Byte-Modus
    schreibe(len(daten), 8 if version < 10 else 16)
    for b in daten:
        schreibe(b, 8)
    schreibe(0, min(4, kapazitaet * 8 - len(bits)))       # Abschluss
    schreibe(0, (-len(bits)) % 8)
    worte = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    fuell = [0xEC, 0x11]
    while len(worte) < kapazitaet:
        worte.append(fuell[(len(worte) - len(bits) // 8) % 2])
    # in Blöcke teilen, Fehlerkorrektur anhängen, verschränken
    divisor = _rs_divisor(ec)
    bloecke, pos = [], 0
    for n, k in gruppen:
        for _ in range(n):
            d = worte[pos:pos + k]
            pos += k
            bloecke.append((d, _rs_rest(d, divisor)))
    erg = []
    for i in range(max(len(d) for d, _ in bloecke)):
        erg.extend(d[i] for d, _ in bloecke if i < len(d))
    for i in range(ec):
        erg.extend(e[i] for _, e in bloecke)
    return erg


def _masken(m, x, y):
    return [
        (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
        (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
        (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
    ][m]


def _strafe(mod):
    n = len(mod)
    s = 0
    zeilen = [mod[y] for y in range(n)] + [[mod[y][x] for y in range(n)] for x in range(n)]
    for z in zeilen:                                      # Regel 1: lange Läufe
        lauf, farbe = 0, None
        for v in z:
            if v == farbe:
                lauf += 1
            else:
                if lauf >= 5:
                    s += 3 + lauf - 5
                lauf, farbe = 1, v
        if lauf >= 5:
            s += 3 + lauf - 5
        txt = "".join("1" if v else "0" for v in z)       # Regel 3: Suchmuster-ähnlich
        s += 40 * (txt.count("00001011101") + txt.count("10111010000"))
    for y in range(n - 1):                                # Regel 2: 2×2-Blöcke
        for x in range(n - 1):
            if mod[y][x] == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                s += 3
    dunkel = sum(sum(z) for z in mod)                     # Regel 4: Verhältnis hell/dunkel
    s += 10 * (abs(dunkel * 20 - n * n * 10) // (n * n))
    return s


def qr_matrix(text):
    daten = text.encode("utf-8")
    version = next((v for v in range(1, 11)
                    if len(daten) + (2 if v < 10 else 3) <= sum(n * k for n, k in _BLOECKE[v][1])), None)
    if version is None:
        raise ValueError("Text zu lang für den QR-Code")
    n = version * 4 + 17
    mod = [[False] * n for _ in range(n)]
    fest = [[False] * n for _ in range(n)]

    def setze(x, y, dunkel):
        mod[y][x] = dunkel
        fest[y][x] = True

    for i in range(n):                                    # Taktmuster
        setze(6, i, i % 2 == 0)
        setze(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):       # Suchmuster + Trennlinie
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    setze(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    pos = _AUSRICHTUNG[version]                           # Ausrichtungsmuster
    for i, ax in enumerate(pos):
        for j, ay in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setze(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def formatbits(maske):
        d = _EC_M << 3 | maske
        rest = d
        for _ in range(10):
            rest = (rest << 1) ^ ((rest >> 9) * 0x537)
        bits = (d << 10 | rest) ^ 0x5412
        b = lambda i: (bits >> i) & 1 == 1              # noqa: E731
        for i in range(6):
            setze(8, i, b(i))
        setze(8, 7, b(6))
        setze(8, 8, b(7))
        setze(7, 8, b(8))
        for i in range(9, 15):
            setze(14 - i, 8, b(i))
        for i in range(8):
            setze(n - 1 - i, 8, b(i))
        for i in range(8, 15):
            setze(8, n - 15 + i, b(i))
        setze(8, n - 8, True)                             # immer dunkles Modul

    formatbits(0)                                         # Platz reservieren
    if version >= 7:                                      # Versionsinformation
        rest = version
        for _ in range(12):
            rest = (rest << 1) ^ ((rest >> 11) * 0x1F25)
        bits = version << 12 | rest
        for i in range(18):
            bit = (bits >> i) & 1 == 1
            a, b = n - 11 + i % 3, i // 3
            setze(a, b, bit)
            setze(b, a, bit)

    worte = _codewoerter(daten, version)                  # Daten im Zickzack eintragen
    i, rechts = 0, n - 1
    while rechts >= 1:
        if rechts == 6:
            rechts = 5
        for vert in range(n):
            for j in range(2):
                x = rechts - j
                hoch = ((rechts + 1) & 2) == 0
                y = n - 1 - vert if hoch else vert
                if not fest[y][x] and i < len(worte) * 8:
                    mod[y][x] = (worte[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        rechts -= 2

    beste = None
    for m in range(8):                                    # beste Maske wählen
        versuch = [zeile[:] for zeile in mod]
        for y in range(n):
            for x in range(n):
                if not fest[y][x] and _masken(m, x, y):
                    versuch[y][x] = not versuch[y][x]
        mod_alt, mod[:] = [z[:] for z in mod], versuch
        formatbits(m)
        strafe = _strafe(mod)
        if beste is None or strafe < beste[0]:
            beste = (strafe, [z[:] for z in mod])
        mod[:] = mod_alt
    return beste[1]


def qr_svg(text, rand=4):
    mod = qr_matrix(text)
    n = len(mod) + 2 * rand
    pfad = "".join(f"M{x + rand},{y + rand}h1v1h-1z" for y, z in enumerate(mod) for x, v in enumerate(z) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{pfad}" fill="#000"/></svg>')

"use strict";
/* Kaffeekasse – Oberfläche. Design und Abläufe aus der Kiosk-Simulation,
   Daten und Prüfungen liegen auf dem Server (/api/...). */

/* =========================================================
   1. Produktgrafiken – selbst gezeichnet, keine Logos.
   ========================================================= */
function canSVG(c1, c2, gross){
  const t = gross ? 8 : 48, b = 144;
  const x = 21, w = 58;
  const h = b - t;
  return `<svg viewBox="0 0 100 152" xmlns="http://www.w3.org/2000/svg" role="img" aria-hidden="true">
    <rect x="${x+3}" y="${t-8}" width="${w-6}" height="10" rx="3" fill="#B9C2CB"/>
    <rect x="${x}" y="${t}" width="${w}" height="${h}" rx="8" fill="${c1}"/>
    <rect x="${x}" y="${t + h*0.6}" width="${w}" height="${h*0.4}" fill="${c2}"/>
    <rect x="${x}" y="${b-10}" width="${w}" height="10" rx="5" fill="#9BA5AE"/>
    <rect x="${x+4}" y="${t+6}" width="6" height="${h-18}" rx="3" fill="#fff" opacity=".22"/>
    <rect x="${x+w-9}" y="${t+6}" width="3" height="${h-18}" rx="1.5" fill="#000" opacity=".18"/>
    <rect x="${x}" y="${t}" width="${w}" height="${h}" rx="8" fill="none" stroke="#000" stroke-opacity=".28"/>
  </svg>`;
}
function barSVG(c1, c2){
  return `<svg viewBox="0 0 140 90" xmlns="http://www.w3.org/2000/svg" role="img" aria-hidden="true">
    <path d="M14 26 l8 6 -8 6 8 6 -8 6 8 6 -8 6 h12 V26 z" fill="${c2}"/>
    <path d="M126 26 l-8 6 8 6 -8 6 8 6 -8 6 8 6 h-12 V26 z" fill="${c2}"/>
    <rect x="24" y="26" width="92" height="38" rx="5" fill="${c1}"/>
    <rect x="24" y="48" width="92" height="16" rx="3" fill="${c2}" opacity=".75"/>
    <rect x="30" y="30" width="80" height="5" rx="2.5" fill="#fff" opacity=".2"/>
    <rect x="24" y="26" width="92" height="38" rx="5" fill="none" stroke="#000" stroke-opacity=".28"/>
  </svg>`;
}
const HEX = /^#[0-9a-fA-F]{6}$/;
function art(p){
  if (!p) return "";
  if (p.bild) return `<img src="${esc(p.bild)}" alt="">`;
  const f1 = HEX.test(p.farbe || "") ? p.farbe : "#3A4250";
  const f2 = HEX.test(p.farbe2 || "") ? p.farbe2 : "#1A1F28";
  if (p.form === "riegel") return barSVG(f1, f2);
  return canSVG(f1, f2, (p.ml || 330) >= 500);
}

/* =========================================================
   2. Daten & Server
   ========================================================= */
let D = {einstellungen:{shopName:"Kaffeekasse", limit:30, autoLogout:300, autoLogoutTeam:900, minBestand:5, adresse:""},
         produkte:[], konten:[], shops:[], daten:null};
let CSRF = "";
/* Sitzung zusätzlich zum Cookie im Header – falls der Browser das Cookie nicht behält */
let SITZUNG = "";
try{ SITZUNG = sessionStorage.getItem("kaffeekasse.sitzung") || localStorage.getItem("kaffeekasse.sitzung") || ""; }catch(e){}
function sitzungMerken(t){
  SITZUNG = t || "";
  try{
    sessionStorage.removeItem("kaffeekasse.sitzung"); localStorage.removeItem("kaffeekasse.sitzung");
    if (SITZUNG) (bleibtAngemeldet ? localStorage : sessionStorage).setItem("kaffeekasse.sitzung", SITZUNG);
  }catch(e){}
}

/* Eigene Reihenfolge je Shop (fehlende Angaben ans Ende, sonst Katalog-Reihenfolge) */
const nachReihe = (liste, sid) => liste.map((p, i) => [p, (p.reihe || {})[sid], i])
  .sort((a, b) => (a[1] ?? 1e9) - (b[1] ?? 1e9) || a[2] - b[2]).map(x => x[0]);
const EUR = n => (+n).toLocaleString("de-DE",{minimumFractionDigits:2,maximumFractionDigits:2}) + " €";
const dt = ts => new Date(ts).toLocaleString("de-DE",{day:"2-digit",month:"2-digit",year:"2-digit",hour:"2-digit",minute:"2-digit"});
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

class ApiFehler extends Error { constructor(msg, status){ super(msg); this.status = status; } }

async function api(method, url, data, zweiterVersuch){
  const opt = {method, headers:{"X-CSRF":CSRF}, credentials:"same-origin"};
  if (SITZUNG) opt.headers["X-Sitzung"] = SITZUNG;
  if (data !== undefined){ opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
  let r;
  try { r = await fetch(url, opt); }
  catch(e){ throw new ApiFehler("Keine Verbindung zum Server.", 0); }
  if (r.headers.has("X-Sitzung")) sitzungMerken(r.headers.get("X-Sitzung"));
  let j = {};
  try { j = await r.json(); } catch(e){}
  if (j && j.csrf) CSRF = j.csrf;
  if (!r.ok){
    // Sitzung/Token veraltet (z. B. Server neu gestartet): Token frisch holen und einmal wiederholen
    if (r.status === 400 && /Sitzung abgelaufen|Seite neu laden/.test(j.fehler || "") && !zweiterVersuch){
      try{ const m = await api("GET", "/api/me", undefined, true); if (m.csrf) CSRF = m.csrf; }catch(e){}
      return api(method, url, data, true);
    }
    if (r.status === 401 && user){ sitzungVerloren(); }
    if (r.status === 401) sitzungMerken("");
    const fe = new ApiFehler(j.fehler || "Fehler " + r.status, r.status);
    if (j.zeilen) fe.zeilen = j.zeilen;
    throw fe;
  }
  return j;
}
/* Aufruf mit Fehlermeldung als Toast; gibt null bei Fehler zurück */
async function tu(method, url, data){
  try { return await api(method, url, data); }
  catch(e){ toast(e.message, "err"); return null; }
}

/* =========================================================
   Shops: gewählter Shop fürs Einkaufen (pro Gerät gemerkt, per ?shop=… vorwählbar)
   ========================================================= */
let SHOPS = [];            // aktive Shops (auch vor dem Login bekannt)
let aktShop = null;        // Einkauf
let vShop = "alle";        // Verwaltung: "alle" oder Shop-ID
function shopName(id){ const s = (D.shops || SHOPS).find(x => x.id === id) || SHOPS.find(x => x.id === id); return s ? s.name : "–"; }
function shopMerken(id){
  aktShop = id;
  try{ localStorage.setItem("kaffeekasse.shop", String(id)); }catch(e){}
  const m = document.querySelector('link[rel="manifest"]');     // "Zum Home-Bildschirm" startet in diesem Shop
  if (m && id != null) m.setAttribute("href", "/manifest.webmanifest?shop=" + id);
}
function shopWaehlen(liste){
  SHOPS = liste || [];
  const url = new URLSearchParams(location.search).get("shop") || new URLSearchParams(location.search).get("halle");
  let wahl = null;
  if (url){
    const u = url.toLowerCase();
    const t = SHOPS.find(x => String(x.id) === url || x.name.toLowerCase() === u || x.name.toLowerCase().replace(/\s+/g, "") === u.replace(/\s+/g, ""));
    if (t) wahl = t.id;
  }
  if (wahl == null){ try{ wahl = +localStorage.getItem("kaffeekasse.shop") || null; }catch(e){} }
  if (!SHOPS.some(x => x.id === wahl)) wahl = SHOPS.length ? SHOPS[0].id : null;
  if (wahl != null) shopMerken(wahl);
  shopAuswahlZeichnen();
}
function shopOptionen(){ return SHOPS.map(x => `<option value="${x.id}" ${x.id === aktShop ? "selected" : ""}>${esc(x.name)}${x.ort ? " · " + esc(x.ort) : ""}</option>`).join(""); }
function shopAuswahlZeichnen(){
  const mehrere = SHOPS.length > 1;
  $("#lShopBox").hidden = !mehrere;
  $("#lShop").innerHTML = shopOptionen();
  $("#shopSelBox").hidden = !mehrere;
  $("#shopSel").innerHTML = shopOptionen();
  $("#shopNameFest").hidden = true;
}
$("#lShop").onchange = () => { shopMerken(+$("#lShop").value); shopAuswahlZeichnen(); };
$("#shopSel").onchange = () => {
  const neu = +$("#shopSel").value;
  const wechseln = () => { cart = []; kat = "Alle"; shopMerken(neu); shopAuswahlZeichnen(); renderShop(); };
  if (!cart.length) return wechseln();
  $("#shopSel").value = aktShop;
  confirmBox("Shop wechseln?", "Der Warenkorb gehört zu " + shopName(aktShop) + " und wird dabei geleert.", "Wechseln", wechseln);
};

async function ladeDaten(){
  const s = await api("GET", "/api/state");
  D = Object.assign(D, s);
  user = s.user;
  stilSetzen(D.einstellungen.stil);
  SHOPS = (D.shops || []).filter(x => x.aktiv).map(({id, name, ort}) => ({id, name, ort}));
  if (!SHOPS.some(x => x.id === aktShop) && SHOPS.length) shopMerken(SHOPS[0].id);
  shopAuswahlZeichnen();
  $("#shopName").textContent = $("#shopNameLogin").textContent = D.einstellungen.shopName;
  document.title = D.einstellungen.shopName;
  return s;
}

/* =========================================================
   3. Hilfen für die Oberfläche
   ========================================================= */
/* Nur Fehler und Warnungen werden eingeblendet – reine Erfolgsmeldungen ("hinzugefügt",
   "gespeichert" …) nicht, die Oberfläche zeigt das Ergebnis ja direkt an. */
function toast(msg, kind){
  if (kind !== "err" && kind !== "warn") return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function openModal(html){
  $("#modal").innerHTML = html;
  $("#modalScrim").classList.add("open");
  const f = $("#modal").querySelector("input,button");
  if (f) setTimeout(() => f.focus(), 40);
}
function closeModal(){ $("#modalScrim").classList.remove("open"); $("#modalScrim").dataset.fest = ""; }
$("#modalScrim").addEventListener("click", e => {
  if (e.target.id === "modalScrim" && !$("#modalScrim").dataset.fest) closeModal();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#modalScrim").dataset.fest){ closeModal(); closeCart(); }
});

function confirmBox(titel, text, okLabel, onOk, gefahr){
  openModal(`<h2>${esc(titel)}</h2><p class="muted" style="margin:6px 0 0">${esc(text)}</p>
    <div class="modal-actions"><button class="btn" data-x="no">Abbrechen</button>
    <button class="btn ${gefahr ? "btn-danger" : "btn-primary"}" data-x="yes">${esc(okLabel)}</button></div>`);
  $("#modal").querySelector('[data-x="no"]').onclick = closeModal;
  $("#modal").querySelector('[data-x="yes"]').onclick = () => { closeModal(); onOk(); };
}

function busy(btn, an){
  if (!btn) return;
  btn.disabled = an;
}

function wer(){
  const team = user.echteRolle && user.echteRolle !== "kunde";
  return rolleName(user.echteRolle || user.rolle) + (team ? (user.modus === "verwaltung" ? " · Verwaltung" : " · Einkauf") : "") + " · " + user.nummer;
}
function rolleName(r){ return r === "inhaber" ? "Administrator" : r === "personal" ? "Shopverantwortliche(r)" : "Mitglied"; }
function mindestLaenge(rolle){ return rolle === "kunde" ? 4 : 10; }
const istKunde = k => k && k.rolle === "kunde";
const istTeam = k => k && k.rolle !== "kunde";
function pinZuEinfach(p){
  if (new Set(p).size === 1) return true;
  const d = [...p].slice(1).map((c,i) => +c - +p[i]);
  return d.every(x => x === 1) || d.every(x => x === -1);
}
const telFeld = el => el.addEventListener("input", () => { el.value = el.value.replace(/\D/g, "").slice(0, 5); });
const nurZiffern = el => el.addEventListener("input", () => { el.value = el.value.replace(/\D/g, "").slice(0, 8); });

/* =========================================================
   4. Sitzung: Anmelden, Abmelden, Zeitschaltuhr
   ========================================================= */
let user = null;
let cart = [];
let kat = "Alle";
let restSek = 0, timerId = null, warnGezeigt = false;
let bleibtAngemeldet = false;
let letzterPing = 0;

function showView(id){
  $("#boot").hidden = true;
  $("#fusszeile").hidden = false;
  $$(".view").forEach(v => v.classList.remove("active"));
  $("#view-" + id).classList.add("active");
  document.documentElement.dataset.ansicht = id;   // z. B. für ruhigeren Hintergrund in der Verwaltung
  window.scrollTo(0,0);
}

async function login(){
  const nr = ($("#luser").value || "").trim();
  const pw = $("#lpass").value || "";
  if (!nr || !pw){ toast("Bitte Personalnummer und " + (loginMitPasswort ? "Passwort" : "PIN") + " eingeben.","err"); return; }
  // 4–8 Ziffern = PIN (Einkauf), sonst Passwort (mit Verwaltung). Der Server erkennt das genauso.
  if (!/^\d{8}$/.test(nr)){ toast("Die Personalnummer hat genau 8 Ziffern.","err"); return; }
  const bleiben = $("#lstay").checked;
  busy($("#loginBtn"), true);
  try{
    const r = await api("POST", "/api/login", {nummer:nr, passwort:pw, bleiben});
    if (r.pending) return geheimDialog(r.konto, r.art, true, bleiben);
    await starteSitzung(bleiben);
  }catch(e){
    toast(e.message, "err");
    $("#lpass").value = "";
  }finally{ busy($("#loginBtn"), false); }
}

/* PIN oder Passwort festlegen/ändern.
   art = "pin" (Einkauf) oder "passwort" (Verwaltung).
   In der Verwaltung wird auch die eigene PIN mit dem Passwort bestätigt. */
function geheimDialog(k, art, erzwungen, bleiben){
  const pin = art === "pin";
  const wort = pin ? "PIN" : "Passwort";
  const altMitPasswort = !erzwungen && user && user.modus === "verwaltung";
  const altWort = altMitPasswort ? "Passwort" : wort;
  const feld = (id, ac, ziffern) => ziffern
    ? `<input id="${id}" class="field mono" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="${ac}">`
    : `<input id="${id}" class="field" type="password" autocomplete="${ac}">`;
  const titel = erzwungen ? (pin ? "Eigene PIN festlegen" : "Neues Passwort festlegen")
    : (pin && !k.hatPin ? "PIN festlegen" : wort + " ändern");
  openModal(`<h2>${titel}</h2>
    <p class="muted" style="margin:6px 0 16px">${esc(k.name)} · Personalnummer <b class="num">${esc(k.nummer)}</b></p>
    ${erzwungen && k.zurueckgesetztVon ? `<div class="card warn-card" style="margin-bottom:14px">
      <p class="small" style="margin:0">Dein Zugang wurde von <b>${esc(k.zurueckgesetztVon)}</b> angelegt bzw. zurückgesetzt.
      Warst du das nicht, sag Bescheid, bevor du weitermachst.</p></div>` : ""}
    ${pin && istTeam({rolle:k.echteRolle || k.rolle}) ? `<p class="muted small" style="margin:0 0 12px">Mit der PIN meldest du dich zum <b>Einkaufen</b> an, mit dem Passwort zur <b>Verwaltung</b>.</p>` : ""}
    ${erzwungen ? "" : `<label class="label" for="pwAlt">${altMitPasswort ? "Dein Passwort (zur Bestätigung)" : "Bisherige" + (pin ? "" : "s") + " " + altWort}</label>
      ${feld("pwAlt","current-password", !altMitPasswort && pin)}`}
    <label class="label" for="pwNeu" style="margin-top:12px">Neue${pin ? "" : "s"} ${wort}</label>${feld("pwNeu","new-password", pin)}
    <label class="label" for="pwNeu2" style="margin-top:12px">Wiederholen</label>${feld("pwNeu2","new-password", pin)}
    <p class="muted small" style="margin:10px 0 0">${pin
      ? "4 bis 8 Ziffern. Nicht erlaubt sind leicht zu erratende PINs wie 1111 oder 1234."
      : "Mindestens 10 Zeichen. Am besten im Passwortmanager speichern."}</p>
    <div class="modal-actions">
      <button class="btn" id="pwAb">Abbrechen</button>
      <button class="btn btn-primary" id="pwOk">Speichern</button></div>`);
  if (pin){ ["#pwNeu","#pwNeu2"].forEach(id => nurZiffern($(id))); if ($("#pwAlt") && !altMitPasswort) nurZiffern($("#pwAlt")); }

  $("#modalScrim").dataset.fest = erzwungen ? "1" : "";
  $("#pwAb").onclick = () => { closeModal(); if (erzwungen){ $("#lpass").value = ""; } };
  $("#pwOk").onclick = async () => {
    const neu = $("#pwNeu").value, neu2 = $("#pwNeu2").value;
    if (pin){
      if (!/^\d{4,8}$/.test(neu)){ toast("Die PIN besteht aus 4 bis 8 Ziffern.","err"); return; }
      if (pinZuEinfach(neu)){ toast("Diese PIN ist zu leicht zu erraten.","err"); return; }
    } else if (neu.length < 10){ toast("Mindestens 10 Zeichen.","err"); return; }
    if (neu !== neu2){ toast("Die beiden Eingaben stimmen nicht überein.","err"); return; }
    const daten = {neu, art};
    if (!erzwungen) daten.alt = $("#pwAlt").value;
    busy($("#pwOk"), true);
    const r = await tu("POST", "/api/passwort", daten);
    busy($("#pwOk"), false);
    if (!r) return;
    closeModal();
    toast(wort + " gespeichert.");
    if (erzwungen) await starteSitzung(bleiben);
    else aktualisieren();
  };
}

async function starteSitzung(bleiben){
  bleibtAngemeldet = !!bleiben;
  try{ await ladeDaten(); }
  catch(e){
    toast("Anmeldung fehlgeschlagen: " + e.message + " Bitte Seite neu laden und erneut anmelden.", "err");
    showView("login");
    return;
  }
  cart = [];
  bleibtAngemeldet = !!bleiben;
  $("#luser").value = ""; $("#lpass").value = "";
  $("#topbar").hidden = false;
  $("#whoName").textContent = user.name;
  $("#whoSub").textContent = wer();
  // Immer zuerst der Einkauf – die Verwaltung liegt hinter dem Knopf "Verwaltung"
  $("#adminBtn").hidden = user.modus !== "verwaltung";
  $("#adminBtn").classList.remove("active");
  showView("shop"); renderShop();
  // Eigenes Telefon: kein Zwangs-Abmelden. Verwaltung läuft immer mit Zeitlimit.
  if (bleibtAngemeldet && user.rolle === "kunde"){ stopTimer(); $("#timerBox").style.display = "none"; }
  else { $("#timerBox").style.display = ""; startTimer(); }
  toast("Angemeldet: " + user.name);

}

async function logout(auto){
  stopTimer();
  try{ await api("POST", "/api/logout", {auto:!!auto}); }catch(e){}
  lokalAbmelden();
}
function lokalAbmelden(){
  user = null; cart = []; closeCart(); closeModal();
  stopTimer();
  $("#topbar").hidden = true;
  showView("login");
  setTimeout(() => $("#luser").focus(), 60);
}
function sitzungVerloren(){
  lokalAbmelden();
  toast("Sitzung abgelaufen – bitte neu anmelden.", "warn");
}

function limitSek(){
  return user && user.rolle === "kunde" ? D.einstellungen.autoLogout : D.einstellungen.autoLogoutTeam;
}
function startTimer(){
  stopTimer();
  restSek = limitSek();
  warnGezeigt = false;
  tick();
  timerId = setInterval(tick, 1000);
}
function stopTimer(){ if (timerId) clearInterval(timerId); timerId = null; }
function resetTimer(){
  if (!user) return;
  restSek = limitSek();
  warnGezeigt = false;
  $("#timerBox").classList.remove("warn");
  // Server wissen lassen, dass hier noch jemand arbeitet
  if (Date.now() - letzterPing > 60000){ letzterPing = Date.now(); api("POST", "/api/ping", {}).catch(() => {}); }
}
function tick(){
  restSek--;
  const p = Math.max(0, restSek / limitSek() * 100);
  $("#timerFill").style.width = p + "%";
  if (restSek <= 45) $("#timerBox").classList.add("warn");
  if (restSek === 45 && !warnGezeigt){
    warnGezeigt = true;
    openModal(`<h2>Bist du noch da?</h2><p class="muted" style="margin:6px 0 0">Du wirst gleich automatisch abgemeldet. Ein offener Warenkorb wird dabei geleert.</p>
      <div class="modal-actions"><button class="btn btn-primary" id="stayBtn">Ich bin noch da</button></div>`);
    $("#stayBtn").onclick = () => { closeModal(); resetTimer(); };
  }
  if (restSek <= 0){ logout(true); toast("Automatisch abgemeldet.","warn"); }
}
["click","keydown","touchstart"].forEach(ev =>
  document.addEventListener(ev, () => { if (user && timerId && restSek > 45) resetTimer(); }, {passive:true}));

/* =========================================================
   5. Shop
   ========================================================= */
function schuldenGesperrt(k){ return k.guthaben <= -D.einstellungen.limit; }

function renderShop(){
  const gesperrt = schuldenGesperrt(user);
  $("#balancePill").textContent = "Guthaben " + EUR(user.guthaben);
  $("#balancePill").className = "pill num " + (user.guthaben < 0 ? (gesperrt ? "pill-red" : "pill-orange") : "pill-green");

  const bn = $("#debtBanner");
  if (gesperrt){
    bn.hidden = false;
    bn.innerHTML = `<div class="card danger-card">
      <h2 style="color:var(--red-text)">Konto gesperrt</h2>
      <p class="muted" style="margin:6px 0 0">Dein Konto steht bei ${EUR(user.guthaben)}. Ab ${EUR(-D.einstellungen.limit)} ist kein Einkauf mehr möglich.
      Bitte begleiche deine Schulden bei den Shopverantwortlichen, dann wird das Konto sofort wieder freigegeben.</p></div>`;
  } else if (user.guthaben < 0){
    bn.hidden = false;
    bn.innerHTML = `<div class="card warn-card">
      <p style="margin:0">Dein Konto ist im Minus: <b class="num">${EUR(user.guthaben)}</b>.
      <span class="muted">Du kannst noch bis ${EUR(-D.einstellungen.limit)} einkaufen.</span></p></div>`;
  } else if (D.einstellungen.warnGuthaben > 0 && user.guthaben < D.einstellungen.warnGuthaben){
    bn.hidden = false;
    bn.innerHTML = `<div class="card warn-card">
      <p style="margin:0">Dein Guthaben ist fast aufgebraucht: <b class="num">${EUR(user.guthaben)}</b>.
      <span class="muted">Bitte bei den Shopverantwortlichen aufladen.</span></p></div>`;
  } else bn.hidden = true;
  wieImmerZeichnen(gesperrt);

  const imShop = nachReihe(D.produkte.filter(p => p.bestand[aktShop] !== undefined), aktShop);
  const kats = ["Alle", ...new Set(imShop.map(p => p.kat))];
  if (!kats.includes(kat)) kat = "Alle";
  $("#cats").innerHTML = kats.map(c =>
    `<button class="cat ${c === kat ? "active" : ""}" data-kat="${esc(c)}">${esc(c)}</button>`).join("");
  $$("#cats .cat").forEach(b => b.onclick = () => { kat = b.dataset.kat; renderShop(); });

  const liste = imShop.filter(p => kat === "Alle" || p.kat === kat);
  $("#prodGrid").innerHTML = liste.map(p => {
    const imKorb = cart.filter(c => c.pid === p.id).reduce((a,c) => a + c.menge, 0);
    const frei = (p.bestand[aktShop] || 0) - imKorb;
    const aus = gesperrt || frei <= 0;
    return `<button class="prod" data-id="${p.id}" ${aus ? "disabled" : ""}>
      ${imKorb ? `<span class="badge-count num">${imKorb}</span>` : ""}
      ${frei <= 0 ? `<span class="stock-tag pill pill-red">leer</span>`
        : frei <= D.einstellungen.minBestand ? `<span class="stock-tag pill pill-orange num">nur ${frei}</span>` : ""}
      <div class="prod-img${p.bild ? " foto" : ""}">${art(p)}</div>
      <div class="prod-body">
        <div class="prod-name">${esc(p.name)}</div>
        <div class="small muted">${esc(p.kat)}</div>
        <div class="prod-meta"><span class="price">${EUR(p.preis)}</span></div>
      </div></button>`;
  }).join("") || `<div class="empty">Keine Produkte in dieser Kategorie.</div>`;

  $$("#prodGrid .prod").forEach(b => b.onclick = () => addToCart(+b.dataset.id));
  renderCart();
}

function addToCart(pid){
  const p = D.produkte.find(x => x.id === pid);
  if (!p) return;
  const pos = cart.find(c => c.pid === pid);
  const imKorb = pos ? pos.menge : 0;
  if (imKorb >= (p.bestand[aktShop] || 0)){ toast("Nicht mehr auf Lager.","warn"); return; }
  if (pos) pos.menge++;
  else cart.push({pid, name:p.name, kat:p.kat, preis:p.preis, menge:1});
  renderShop();
  if (!$("#cartDrawer").classList.contains("open")) toast(p.name + " hinzugefügt");
}
function setMenge(pid, d){
  const pos = cart.find(c => c.pid === pid);
  if (!pos) return;
  const p = D.produkte.find(x => x.id === pid);
  pos.menge += d;
  const lager = p ? (p.bestand[aktShop] || 0) : 0;
  if (p && pos.menge > lager){ pos.menge = lager; toast("Nicht mehr auf Lager.","warn"); }
  if (pos.menge <= 0) cart = cart.filter(c => c.pid !== pid);
  renderShop();
}
const cartSum = () => Math.round(cart.reduce((a,c) => a + c.preis * c.menge, 0) * 100) / 100;
const cartCount = () => cart.reduce((a,c) => a + c.menge, 0);

function renderCart(){
  const n = cartCount(), s = cartSum();
  $("#cartFab").hidden = n === 0;
  $("#cartFabCount").textContent = n + " Artikel";
  $("#cartFabSum").textContent = EUR(s);

  $("#cartBody").innerHTML = cart.length ? cart.map(c => {
    const p = D.produkte.find(x => x.id === c.pid);
    return `<div class="line">
      <div class="line-thumb">${art(p)}</div>
      <div style="flex:1;min-width:0">
        <div style="font-weight:500">${esc(c.name)}</div>
        <div class="small muted num">${EUR(c.preis)} pro Stück</div>
      </div>
      <div class="qty">
        <button data-m="-1" data-id="${c.pid}" aria-label="weniger">−</button>
        <span class="num">${c.menge}</span>
        <button data-m="1" data-id="${c.pid}" aria-label="mehr">+</button>
      </div>
      <div class="num" style="width:62px;text-align:right;font-weight:500">${EUR(c.preis * c.menge)}</div>
    </div>`;
  }).join("") : `<div class="empty">Noch nichts drin.<br><span class="small">Tippe im Shop auf ein Produkt.</span></div>`;

  $$("#cartBody .qty button").forEach(b => b.onclick = () => setMenge(+b.dataset.id, +b.dataset.m));

  const danach = user ? user.guthaben - s : 0;
  $("#cartSub").textContent = EUR(s);
  $("#cartTotal").textContent = EUR(s);
  $("#cartAfter").textContent = EUR(danach);
  $("#cartAfter").style.color = danach < 0 ? (danach < -D.einstellungen.limit ? "var(--red-text)" : "var(--orange-text)") : "";
  $("#payBtn").disabled = cart.length === 0;
}

function openCart(){ $("#cartDrawer").classList.add("open"); $("#cartScrim").classList.add("open"); }
function closeCart(){ $("#cartDrawer").classList.remove("open"); $("#cartScrim").classList.remove("open"); }
$("#cartFab").onclick = openCart;
$("#cartClose").onclick = closeCart;
$("#cartScrim").onclick = closeCart;

$("#payBtn").onclick = async () => {
  const s = cartSum(), danach = user.guthaben - s;
  if (danach < -D.einstellungen.limit - 0.001){
    openModal(`<h2>Das reicht nicht</h2>
      <p class="muted" style="margin:6px 0 14px">Dein Konto würde damit auf <b class="num">${EUR(danach)}</b> fallen.
      Erlaubt sind höchstens ${EUR(-D.einstellungen.limit)}.</p>
      <p class="muted" style="margin:0">Nimm etwas aus dem Warenkorb oder lade bei den Shopverantwortlichen Guthaben auf.</p>
      <div class="modal-actions"><button class="btn btn-primary" id="okBtn">Verstanden</button></div>`);
    $("#okBtn").onclick = closeModal;
    return;
  }
  busy($("#payBtn"), true);
  let r;
  try{
    r = await api("POST", "/api/kaufen", {shop:aktShop, posten:cart.map(c => ({pid:c.pid, menge:c.menge}))});
  }catch(e){
    busy($("#payBtn"), false);
    toast(e.message, "err");
    if (e.status === 409){ await ladeDaten().catch(() => {}); renderShop(); }
    return;
  }
  busy($("#payBtn"), false);
  cart = [];
  closeCart();
  await ladeDaten().catch(() => {});
  openModal(`<h2>Danke, ${esc(user.name.split(" ")[0])}</h2>
    <p class="muted" style="margin:6px 0 16px">Gebucht: <b class="num">${EUR(r.summe)}</b> · ${esc(r.shop)} · Beleg ${esc(r.beleg)}</p>
    <div class="card" style="background:var(--surface-2)">
      <div class="sum-row"><span class="muted">Neues Guthaben</span><span class="num" style="font-weight:600">${EUR(r.guthaben)}</span></div>
    </div>
    <div class="modal-actions">
      <button class="btn" id="weiterBtn">Weiter einkaufen</button>
      <button class="btn btn-primary" id="fertigBtn">Schließen</button>
    </div>`);
  $("#weiterBtn").onclick = () => { closeModal(); renderShop(); resetTimer(); };
  $("#fertigBtn").onclick = () => { closeModal(); seiteSchliessen(); };
  renderShop();
};

/* "Schließen" nach dem Kauf: Fenster/App schließen, die Anmeldung bleibt bestehen.
   Browser erlauben das nur bei installierten Apps bzw. selbst geöffneten Fenstern –
   sonst zurück zum Anfang des Shops (die Abmeldung erfolgt dann automatisch nach Zeit). */
function seiteSchliessen(){
  kat = "Alle";
  renderShop();
  window.scrollTo(0, 0);
  try{ window.close(); }catch(e){}
}

/* ---------- Konto-Knopf (für alle) ---------- */
$("#kontoBtn").onclick = () => kontoDialog();

async function kontoDialog(){
  if (!user) return;
  let verlauf = "";
  const imShop = user.modus !== "verwaltung";
  const v = await tu("GET", "/api/mein-verlauf");
  if (!v) return;
  verlauf = `<h3 style="margin:18px 0 8px">Buchungen</h3>${verlaufTabelle(v)}`;
  const team = istTeam({rolle:user.echteRolle});
  openModal(`<h2>Mein Konto</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(user.name)} · <span class="num">${esc(user.nummer)}</span> · ${rolleName(user.echteRolle)}
      · Guthaben <b class="num">${EUR(user.guthaben)}</b></p>
    ${team ? `<div class="card" style="background:var(--surface-2);margin-bottom:14px;padding:12px 14px">
      <p class="small" style="margin:0">${imShop
        ? "Mit PIN angemeldet: nur Einkauf. Für die Verwaltung abmelden und mit deinem <b>Passwort</b> anmelden."
        : "Mit Passwort angemeldet: Einkauf und – über den Knopf <b>Verwaltung</b> oben – die Verwaltung."}
      ${!imShop && !user.hatPin ? "<br><b>Du hast noch keine PIN.</b> Leg sie hier fest, dann kannst du dich am Kiosk auch schnell nur zum Einkaufen anmelden." : ""}</p></div>` : ""}
    <label class="label">Design</label>
    ${designWahlHTML()}
    <p class="muted small" style="margin:8px 0 0">„Automatisch“ folgt der Einstellung deines Handys bzw. Computers.</p>
    ${verlauf}
    <div class="modal-actions">
      ${imShop ? `<button class="btn" id="kPin">PIN ändern</button>`
        : `<button class="btn" id="kPin">${user.hatPin ? "PIN ändern" : "PIN festlegen"}</button><button class="btn" id="kPw">Passwort ändern</button>`}
      <button class="btn btn-primary" id="kClose">Schließen</button></div>
    <p class="small" style="margin:10px 0 0;text-align:right"><button type="button" class="linkbtn-muted" id="kDs">Datenschutzhinweis</button></p>`);
  designWahlVerdrahten();
  $("#kDs").onclick = datenschutzDialog;
  $("#kClose").onclick = closeModal;
  $("#kPin").onclick = () => geheimDialog(user, "pin", false, bleibtAngemeldet);
  if ($("#kPw")) $("#kPw").onclick = () => geheimDialog(user, "passwort", false, false);
}

/* ---------- Design: automatisch / hell / dunkel ---------- */
const DESIGNS = [["auto","Automatisch"],["hell","Hell"],["dunkel","Dunkel"]];
function designLesen(){ try{ return localStorage.getItem("kaffeekasse.design") || "auto"; }catch(e){ return "auto"; } }
function designSetzen(t){
  try{ localStorage.setItem("kaffeekasse.design", t); }catch(e){}
  const r = document.documentElement;
  if (t === "hell") r.setAttribute("data-theme","light");
  else if (t === "dunkel") r.setAttribute("data-theme","dark");
  else r.removeAttribute("data-theme");
  themeColor();
}
/* ---------- Stil der ganzen Kaffeekasse (stellt der Administrator ein) ---------- */
let STILE = {standard:"Standard"};
const STIL_HINWEIS = {
  standard: "Das bisherige Design. Hell oder dunkel wählt jede Person selbst unter Konto → Design.",
  hangar: "Hangarboden mit Glasflächen, Warnstreifen und Remove-Before-Flight-Fahnen im Warenkorb. Immer dunkel.",
  dashboard: "Milchglas-Kacheln wie im Home-Assistant-Dashboard. Hell oder dunkel wählt jede Person selbst."
};
function stilSetzen(s){
  document.documentElement.setAttribute("data-stil", STILE[s] ? s : "standard");
  themeColor();
}
function themeColor(){
  const m = document.querySelector('meta[name="theme-color"]');
  if (m) m.setAttribute("content", getComputedStyle(document.body).backgroundColor);
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", themeColor);
function designWahlHTML(){
  const akt = designLesen();
  return `<div class="seg" id="designWahl">${DESIGNS.map(([k,l]) =>
    `<button type="button" data-d="${k}" class="${k === akt ? "active" : ""}">${l}</button>`).join("")}</div>`;
}
function designWahlVerdrahten(){
  $$("#designWahl [data-d]").forEach(b => b.onclick = () => {
    designSetzen(b.dataset.d);
    $$("#designWahl [data-d]").forEach(x => x.classList.toggle("active", x === b));
  });
}

function verlaufTabelle(zeilen){
  return zeilen.length ? `<table>${zeilen.map(z => `<tr><td class="small muted num" style="white-space:nowrap">${dt(z.zeit)}</td>
      <td class="small">${esc(z.txt)}</td><td class="t-right num" style="white-space:nowrap;color:${z.betrag >= 0 ? "var(--green-text)" : "var(--orange-text)"}">${EUR(z.betrag)}</td></tr>`).join("")}</table>`
    : `<div class="empty">Noch keine Buchungen.</div>`;
}

/* =========================================================
   6. Verwaltung
   ========================================================= */
function istInhaber(){ return user && user.rolle === "inhaber"; }

/* Shops, die jemand in der Verwaltung sieht (Admin: alle, Shopverantwortliche: zugeordnete) */
function meineShops(){ return (D.shops || []).filter(x => (D.meineShops || []).includes(x.id)); }
function vShopIds(){ return vShop === "alle" ? (D.meineShops || []) : [vShop]; }
const shopParam = () => vShop === "alle" ? "" : "&shop=" + vShop;
function adminShopZeichnen(){
  const liste = meineShops();
  if (vShop !== "alle" && !liste.some(x => x.id === vShop)) vShop = "alle";
  if (vShop === "alle" && liste.length === 1) vShop = liste[0].id;
  const opts = (liste.length > 1 ? [`<option value="alle">${istInhaber() ? "Alle Shops" : "Alle meine Shops"}</option>`] : [])
    .concat(liste.map(x => `<option value="${x.id}" ${x.id === vShop ? "selected" : ""}>${esc(x.name)}${x.aktiv ? "" : " (geschlossen)"}</option>`));
  $("#adminShop").innerHTML = opts.join("") || `<option>Kein Shop zugeordnet</option>`;
  $("#adminShop").value = String(vShop);
  $("#adminShop").disabled = liste.length <= 1;
}
$("#adminShop").onchange = () => { const v = $("#adminShop").value; vShop = v === "alle" ? "alle" : +v; renderStaff(); ladeTab(); };

/* ---------- Zeitraum-Leiste (Übersicht, Verkäufe, Export, Protokoll) ---------- */
const ZR_ARTEN = [["heute","Heute"],["7","7 Tage"],["monat","Dieser Monat"],["vormonat","Letzter Monat"],["frei","Eigener Zeitraum"]];
let ZR = {art:"monat", von:null, bis:null};
const tagAnfang = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const isoTag = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
function zeitraum(){
  const jetzt = new Date(), heute = tagAnfang(jetzt);
  const morgen = new Date(heute); morgen.setDate(morgen.getDate() + 1);
  switch (ZR.art){
    case "heute": return [heute, morgen];
    case "7": { const v = new Date(heute); v.setDate(v.getDate() - 6); return [v, morgen]; }
    case "vormonat": return [new Date(heute.getFullYear(), heute.getMonth() - 1, 1), new Date(heute.getFullYear(), heute.getMonth(), 1)];
    case "frei": {
      const v = ZR.von ? new Date(ZR.von + "T00:00") : new Date(heute.getFullYear(), heute.getMonth(), 1);
      const b = ZR.bis ? new Date(ZR.bis + "T00:00") : new Date(heute);
      b.setDate(b.getDate() + 1);
      return [v, b];
    }
    default: return [new Date(heute.getFullYear(), heute.getMonth(), 1), morgen];
  }
}
const zrQuery = () => { const [v,b] = zeitraum(); return `von=${v.getTime()}&bis=${b.getTime()}`; };
function zrText(){
  const [v,b] = zeitraum(); const e = new Date(b); e.setDate(e.getDate() - 1);
  const f = d => d.toLocaleDateString("de-DE",{day:"2-digit",month:"2-digit",year:"numeric"});
  return +v === +e ? f(v) : f(v) + " – " + f(e);
}
function zeitraumZeichnen(){
  $$("[data-zr]").forEach(box => {
    const [v,b] = zeitraum(); const e = new Date(b); e.setDate(e.getDate() - 1);
    box.innerHTML = `<div class="cats">${ZR_ARTEN.map(([k,l]) => `<button class="cat ${k === ZR.art ? "active" : ""}" data-zra="${k}">${l}</button>`).join("")}</div>
      ${ZR.art === "frei" ? `<div class="zr-frei"><label class="small muted">von <input type="date" class="field" data-zrv value="${isoTag(v)}"></label>
        <label class="small muted">bis <input type="date" class="field" data-zrb value="${isoTag(e)}"></label></div>`
        : `<span class="small muted num zr-text">${zrText()}</span>`}`;
    box.querySelectorAll("[data-zra]").forEach(btn => btn.onclick = () => { ZR.art = btn.dataset.zra; zeitraumZeichnen(); ladeTab(); });
    const vi = box.querySelector("[data-zrv]"), bi = box.querySelector("[data-zrb]");
    if (vi) vi.onchange = () => { ZR.von = vi.value; zeitraumZeichnen(); ladeTab(); };
    if (bi) bi.onchange = () => { ZR.bis = bi.value; zeitraumZeichnen(); ladeTab(); };
  });
  renderExport();
}

/* ---------- Tabs ---------- */
let aktTab = "t-ueber";
function renderAdmin(){
  const chef = istInhaber();
  if (vShop === "alle" && (D.meineShops || []).includes(aktShop) && !chef) vShop = aktShop;
  $("#adminSub").textContent = chef
    ? "Voller Zugriff: alle Shops, Mitglieder, Rollen und Einstellungen."
    : "Mitglieder und Guthaben für alle – Sortiment und Verkäufe deiner Shops.";
  $$("#view-admin [data-owner]").forEach(el => el.hidden = !chef);
  if (!chef && ["allg","melde"].includes(einstTeil)) einstTeil = "export";
  zeitraumZeichnen();
  renderStaff();
  zeigeTab("t-ueber");   // Immer mit der Übersicht starten
}

function zeigeTab(id){
  aktTab = id;
  $('[data-tabs="admin"]').querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.panel === id));
  $("#view-admin").querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === id));
  ladeTab();
}

/* Tabs mit Zeitraum laden ihre Daten nach – nur den gerade sichtbaren */
let ladeNr = 0;
async function ladeTab(){
  if (!user || user.modus !== "verwaltung") return;
  const nr = ++ladeNr;
  if (aktTab === "t-ueber"){
    const k = await tu("GET", "/api/kennzahlen?" + zrQuery() + shopParam());
    if (k && nr === ladeNr){ KZ = k; renderKpis(); }
  } else if (aktTab === "t-verk"){
    const v = await tu("GET", "/api/verkaeufe?" + zrQuery() + shopParam());
    if (v && nr === ladeNr){
      // neuere Server: {verkaeufe, anzahl, summe, …}; ältere: nur die Liste
      VK = Array.isArray(v) ? v : (v.verkaeufe || []);
      VKG = Array.isArray(v) ? null : v;
      renderSales();
    }
  } else if (aktTab === "t-kasse"){
    const k = await tu("GET", "/api/kasse?" + zrQuery());
    if (k && nr === ladeNr){ KA = k; renderKasse(); }
  } else if (aktTab === "t-shops"){
    const [v,b] = zeitraumMonat();
    const k = await tu("GET", `/api/kennzahlen?von=${v}&bis=${b}`);
    if (k && nr === ladeNr){ SHKZ = k; renderShops(); }
  } else if (aktTab === "t-einst"){
    if (einstTeil === "log"){
      const j = await tu("GET", "/api/journal?" + zrQuery());
      if (j && nr === ladeNr){ JR = j; renderLog(); }
    } else if (einstTeil === "sich" && istInhaber()){
      const s = await tu("GET", "/api/sicherungen");
      if (s && nr === ladeNr){ SB = s; renderSicherungen(); }
    }
  }
}
function zeitraumMonat(){ const h = new Date(); return [new Date(h.getFullYear(), h.getMonth(), 1).getTime(), Date.now() + 60000]; }
let KZ = null, VK = [], VKG = null, JR = [], SB = null, SHKZ = null, KA = null;

function renderStaff(){
  adminShopZeichnen();
  renderCustomers(); renderProdTable(); renderFolder(); renderSettings(); renderEinstTeil();
  if (KZ) renderKpis();
  renderSales(); renderLog(); if (KA) renderKasse();
  if (istInhaber()) renderShops();
}

/* Nach jeder Änderung: frische Daten vom Server holen und neu zeichnen */
async function aktualisieren(){
  try{ await ladeDaten(); }catch(e){ toast(e.message, "err"); return; }
  $("#whoName").textContent = user.name;
  $("#whoSub").textContent = wer();
  renderShop();
  if (user.modus === "verwaltung"){ renderStaff(); ladeTab(); }
}

/* ---------- Einheitliche Liste: eine sichtbare Aktion + ☰-Menü ---------- */
function listeZeile({avatar, titel, zeilen = [], wert = "", aktion, menu, id, klasse = ""}){
  return `<div class="list-item ${klasse}">
    ${avatar !== undefined ? `<div class="avatar">${avatar}</div>` : ""}
    <div class="li-main">
      <div class="li-titel">${titel}</div>
      ${zeilen.filter(Boolean).map(z => `<div class="small muted li-zeile">${z}</div>`).join("")}
    </div>
    ${wert ? `<div class="li-wert">${wert}</div>` : ""}
    <div class="li-actions">
      ${aktion ? `<button class="btn btn-sm" data-akt="${id}">${aktion}</button>` : ""}
      ${menu ? `<button class="btn btn-sm burger" data-menu="${id}" aria-label="Weitere Aktionen" title="Weitere Aktionen">☰</button>` : ""}
    </div></div>`;
}
/* Menü an einem Knopf öffnen: eintraege = [[icon, text, fn, gefahr?], …] */
function menuOeffnen(anker, kopf, eintraege, schluessel){
  const m = $("#menu");
  if (!m.hidden && m.dataset.id === schluessel){ menuSchliessen(); return; }
  m.innerHTML = `<div class="menu-head">${esc(kopf)}</div>` + (eintraege.length
    ? eintraege.map(([i,l,,g], n) => `<button role="menuitem" data-n="${n}" class="${g ? "danger" : ""}"><span class="mi">${i}</span>${esc(l)}</button>`).join("")
    : `<div class="muted small" style="padding:8px 12px">Keine Aktionen verfügbar.</div>`);
  m.dataset.id = schluessel;
  m.hidden = false;
  const r = anker.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
  let top = r.bottom + 6, left = Math.min(r.right - w, window.innerWidth - w - 8);
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
  m.style.top = top + "px"; m.style.left = Math.max(8, left) + "px";
  m.querySelectorAll("[data-n]").forEach(b => b.onclick = () => { menuSchliessen(); eintraege[+b.dataset.n][2](); });
}
function menuSchliessen(){ $("#menu").hidden = true; }
document.addEventListener("click", e => { if (!e.target.closest("#menu") && !e.target.closest("[data-menu]")) menuSchliessen(); });
window.addEventListener("resize", menuSchliessen);
window.addEventListener("scroll", menuSchliessen, {passive:true});
function chips(ziel, liste, aktiv, onWahl){
  $(ziel).innerHTML = liste.map(([k,l,n]) => `<button class="cat ${k === aktiv ? "active" : ""}" data-c="${esc(k)}">${esc(l)}${n !== undefined ? ` <span class="num">${n}</span>` : ""}</button>`).join("");
  $(ziel).querySelectorAll("[data-c]").forEach(b => b.onclick = () => onWahl(b.dataset.c));
}
function listeVerdrahten(ziel, aktionen, menues){
  $(ziel).querySelectorAll("[data-akt]").forEach(b => b.onclick = () => aktionen(b.dataset.akt));
  $(ziel).querySelectorAll("[data-menu]").forEach(b => b.onclick = e => { e.stopPropagation(); menues(b.dataset.menu, b); });
}
const shopTag = sid => (D.meineShops || []).length > 1 && vShop === "alle" ? `<span class="shop-tag">${esc(shopName(sid))}</span>` : "";

/* =========================================================
   Übersicht
   ========================================================= */
function balken(liste, wertText, alt){
  const max = Math.max(...liste.map(x => x[1]), 0) || 1;
  return liste.map(([n,v,extra]) => `<div class="bar-row">
    <div><div class="small">${esc(n)}</div><div class="bar-track"><div class="bar-fill${alt ? " alt" : ""}" style="width:${v/max*100}%"></div></div></div>
    <div class="t-right num small">${wertText(v, extra)}</div></div>`).join("") || `<p class="muted small">Keine Verkäufe in diesem Zeitraum.</p>`;
}
function renderKpis(){
  const k = KZ; if (!k) return;
  const marge = k.umsatz - k.ohneEk > 0 ? Math.round(k.rohertrag / (k.umsatz - k.ohneEk) * 100) : null;
  $("#kpis").innerHTML = [
    ["Umsatz", EUR(k.umsatz), `${k.verkaeufe} Einkäufe · ${k.stueck} Artikel`],
    ["Rohertrag", EUR(k.rohertrag), marge !== null ? `Marge ${marge} %` + (k.ohneEk ? ` · ${EUR(k.ohneEk)} Umsatz ohne Einkaufspreis` : "") : "Einkaufspreise fehlen noch"],
    ["Schwund (Inventur)", EUR(k.schwund.wert), `${k.schwund.stueck} Stück fehlten` + (k.mehrbestand.stueck ? ` · ${k.mehrbestand.stueck} mehr gezählt` : "")],
    ["Offene Beträge", EUR(k.schulden), `alle Shops · ${k.konten} Konten`]
  ].map(([l,v,s]) => `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-val">${v}</div><div class="kpi-sub small muted">${esc(s)}</div></div>`).join("");
  $("#kpis2").innerHTML = [
    ["Wareneinsatz", EUR(k.wareneinsatz)],
    ["Wareneingang", EUR(k.eingang.wert) + ` · ${k.eingang.stueck} Stk`],
    ["Warenwert Kühlschrank + Lager (EK)", EUR(k.lager.wert)],
    ["Guthaben der Mitglieder", EUR(k.guthaben)],
    ["Kassenstand (Bargeld)", EUR(k.kasse)]
  ].map(([l,v]) => `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-val">${v}</div></div>`).join("");

  // Nachfüllen: aktueller Bestand (unabhängig vom Zeitraum)
  const ids = vShopIds(), min = D.einstellungen.minBestand;
  const niedrig = [];
  D.produkte.forEach(p => ids.forEach(sid => { const b = p.bestand[sid]; if (b !== undefined && b <= min) niedrig.push([p, sid, b, lagerIn(p, sid)]); }));
  niedrig.sort((a,b) => a[2] - b[2]);
  const nachbestellen = niedrig.filter(x => !x[3]).length;
  $("#lowStockBox").innerHTML = niedrig.length ? `<div class="card warn-card nachfuell">
    <div class="row" style="justify-content:space-between;margin-bottom:8px"><h3 style="color:var(--orange-text)">Kühlschrank nachfüllen (${niedrig.length})</h3>
      <span class="small muted">Kühlschrank ≤ ${min}${nachbestellen ? ` · <span style="color:var(--red-text)">${nachbestellen}× Lager leer – nachbestellen</span>` : ""}</span></div>
    <div class="chips">${niedrig.map(([p,sid,b,lg], i) => `<button class="pill ${lg ? "pill-orange" : "pill-red"} nf" data-i="${i}" title="${lg ? "Aus dem Lager auffüllen" : "Lager leer – Wareneingang buchen"}">${esc(p.name)}${ids.length > 1 ? " · " + esc(shopName(sid)) : ""} · ❄ <b class="num">${b}</b> · ▦ <b class="num">${lg}</b></button>`).join("")}</div></div>` : "";
  $$("#lowStockBox .nf").forEach(b => b.onclick = () => { const [p,sid,,lg] = niedrig[+b.dataset.i]; lg ? auffuellenDialog(p, sid) : eingangDialog(p, sid); });

  // Umsatz pro Tag: jeder Tag im Zeitraum, auch ohne Umsatz
  const [v,b] = zeitraum(); const tage = {}; k.tage.forEach(([t,s]) => tage[t] = s);
  const alle = []; for (let d = new Date(v); d < b && alle.length < 400; d.setDate(d.getDate() + 1)) alle.push([isoTag(d), tage[isoTag(d)] || 0]);
  const tmax = Math.max(...alle.map(x => x[1]), 0) || 1;
  $("#tagChart").innerHTML = alle.length > 1 ? `<div class="tage">${alle.map(([t,s]) => `<div class="tag" title="${t.split("-").reverse().join(".")}: ${EUR(s)}"><div style="height:${s/tmax*100}%"></div></div>`).join("")}</div>
    <div class="row small muted" style="justify-content:space-between;margin-top:6px"><span>${alle[0][0].split("-").reverse().join(".")}</span><span>max. ${EUR(tmax === 1 && !alle.some(x => x[1]) ? 0 : tmax)}</span><span>${alle[alle.length-1][0].split("-").reverse().join(".")}</span></div>`
    : `<div class="kpi-val">${EUR(alle.length ? alle[0][1] : 0)}</div>`;

  const shopListe = ids.map(sid => [shopName(sid), (k.proShop[String(sid)] || {umsatz:0}).umsatz]).sort((a,b) => b[1] - a[1]);
  $("#shopVergleichBox").hidden = ids.length < 2;
  $("#shopVergleich").innerHTML = balken(shopListe, (s, n) => EUR(s));
  $("#bestsellers").innerHTML = balken(k.top.map(([n,c,s]) => [n,c,s]), c => c + " Stk");
  $("#catRevenue").innerHTML = balken(k.kat, s => EUR(s), true);
}

/* =========================================================
   Verkäufe (inkl. Storno)
   ========================================================= */
let vFilter = "alle";
function renderSales(){
  const q = ($("#vSuche").value || "").toLowerCase().trim();
  const basis = VK || [];
  const teil = basis.filter(v => v.posten.some(p => p.storno));
  // Gekürzt = der Server hat nur die neuesten Einkäufe geschickt; Anzahl und Summe kommen dann vom Server
  const gekuerzt = !!VKG && VKG.anzahl > basis.length;
  chips("#vFilter", [["alle","Alle",VKG ? VKG.anzahl : basis.length],["storno","Mit Storno",VKG ? VKG.mitStorno : teil.length]],
        vFilter, c => { vFilter = c; renderSales(); });
  const cmp = {"zeit-": (a,b) => b.zeit - a.zeit, "zeit": (a,b) => a.zeit - b.zeit,
               "summe-": (a,b) => b.summe - a.summe || b.zeit - a.zeit, "name": (a,b) => a.name.localeCompare(b.name, "de") || b.zeit - a.zeit}[$("#vSort").value];
  const liste = (vFilter === "storno" ? teil : basis)
    .filter(v => !q || v.name.toLowerCase().includes(q) || v.nummer.includes(q) || v.beleg.toLowerCase().includes(q) || v.posten.some(p => p.name.toLowerCase().includes(q)))
    .sort(cmp);
  const summe = liste.reduce((a,v) => a + v.summe, 0);
  if (VKG && !q){
    // ohne Suche: Werte des Servers – stimmen auch, wenn die Liste gekürzt ist
    const n = vFilter === "storno" ? VKG.mitStorno : VKG.anzahl;
    const s = vFilter === "storno" ? VKG.summeMitStorno : VKG.summe;
    $("#vAnzahl").textContent = `${n} Einkäufe · ${EUR(s)}`;
  } else {
    $("#vAnzahl").textContent = `${liste.length} Einkäufe · ${EUR(summe)}` + (gekuerzt ? ` (Suche nur in den neuesten ${basis.length})` : "");
  }
  const hinweisGekuerzt = gekuerzt
    ? `<p class="muted small" style="text-align:center">Angezeigt und durchsucht werden die neuesten ${basis.length} von ${VKG.anzahl} Einkäufen. Für ältere bitte den Zeitraum eingrenzen oder als CSV exportieren.</p>`
    : "";
  $("#vListe").innerHTML = (liste.slice(0, 400).map(v => listeZeile({
    id: v.id, klasse: v.storniert ? "storniert" : "",
    titel: `${esc(v.name)} <span class="muted small num">${esc(v.nummer)}</span>${shopTag(v.shop)}`,
    zeilen: [`<span class="num">${dt(v.zeit)} · ${esc(v.beleg)}</span>`,
             esc(v.posten.map(p => p.menge + "× " + p.name + (p.storno ? " (storniert)" : "")).join(", "))],
    wert: `<span class="pill num ${v.storniert ? "pill-red" : ""}">${EUR(v.summe)}</span>`,
    aktion: "Details", menu: true
  })).join("") + (liste.length > 400 ? `<p class="muted small" style="text-align:center">… ${liste.length - 400} weitere – bitte Zeitraum eingrenzen oder als CSV exportieren.</p>` : "")
    || `<div class="empty">Keine Verkäufe in diesem Zeitraum.</div>`) + hinweisGekuerzt;
  const finde = id => basis.find(v => v.id === +id);
  listeVerdrahten("#vListe", id => verkaufDialog(finde(id)), (id, b) => {
    const v = finde(id), offen = v.posten.filter(p => !p.storno);
    menuOeffnen(b, v.beleg + " · " + v.name, [
      ["☰", "Details & einzelne Posten stornieren", () => verkaufDialog(v)],
      ["↺", "Ganzen Einkauf stornieren", () => ganzStornieren(v), true],
      ["≡", "Kontoverlauf von " + v.name, () => { const k = (D.konten || []).find(x => x.id === v.kontoId); if (k) verlaufDialog(k); else toast("Dieses Konto gibt es nicht mehr.","warn"); }]
    ].filter((x, i) => i !== 1 || offen.length), "v" + id);
  });
}
$("#vSuche").addEventListener("input", renderSales);
$("#vSort").onchange = renderSales;

function verkaufDialog(v){
  openModal(`<h2>Einkauf ${esc(v.beleg)}</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(v.name)} · <span class="num">${esc(v.nummer)}</span> · ${dt(v.zeit)} · ${esc(shopName(v.shop))}</p>
    <table>${v.posten.map(p => `<tr>
      <td style="${p.storno ? "text-decoration:line-through;color:var(--muted)" : ""}">${esc(p.name)}</td>
      <td class="num" style="width:48px">${p.menge}×</td>
      <td class="t-right num" style="width:78px">${EUR(p.preis * p.menge)}</td>
      <td class="t-right" style="width:104px">${p.storno ? `<span class="pill pill-red">storniert</span>`
        : `<button class="btn btn-sm btn-danger" data-p="${p.id}">Stornieren</button>`}</td></tr>`).join("")}
      <tr><td><b>Summe</b></td><td></td><td class="t-right num"><b>${EUR(v.summe)}</b></td><td></td></tr></table>
    <p class="muted small" style="margin:12px 0 0">Beim Stornieren geht der Betrag zurück aufs Konto und die Ware zurück in den Bestand von ${esc(shopName(v.shop))}.</p>
    <div class="modal-actions"><button class="btn btn-primary" id="vdZu">Schließen</button></div>`);
  $("#vdZu").onclick = closeModal;
  $$("#modal [data-p]").forEach(b => b.onclick = () => {
    const p = v.posten.find(x => x.id === +b.dataset.p);
    confirmBox("Posten stornieren?", `${p.name} (${p.menge}×, ${EUR(p.preis * p.menge)}) wird ${v.name} gutgeschrieben und der Bestand zurückgebucht.`,
      "Stornieren", async () => { if (await tu("POST", "/api/storno", {posten:p.id})) aktualisieren(); }, true);
  });
}
function ganzStornieren(v){
  const offen = v.posten.filter(p => !p.storno);
  const betrag = offen.reduce((a,p) => a + p.preis * p.menge, 0);
  confirmBox("Ganzen Einkauf stornieren?", `${offen.length} Posten (${EUR(betrag)}) werden ${v.name} gutgeschrieben und der Bestand zurückgebucht.`,
    "Alles stornieren", async () => {
      for (const p of offen){ if (!await tu("POST", "/api/storno", {posten:p.id})) break; }
      aktualisieren();
    }, true);
}

/* =========================================================
   Sortiment: gemeinsamer Katalog, Bestand je Shop – Kühlschrank (wird verkauft) und Lager (Vorrat)
   ========================================================= */
let sKat = "Alle";
let sOrt = "kuehlschrank";                       // Umschalter: kuehlschrank | lager | gesamt
const ORT_NAME = {kuehlschrank:"Kühlschrank", lager:"Lager", gesamt:"Gesamt"};
const bestandIn = (p, sid) => p.bestand[sid];                       // Kühlschrank
const lagerIn = (p, sid) => (p.lager || {})[sid] || 0;              // Lager
const gefuehrtIn = (p, sid) => p.bestand[sid] !== undefined;
const mengeIn = (p, sid, ort = sOrt) => ort === "lager" ? lagerIn(p, sid)
  : ort === "gesamt" ? (bestandIn(p, sid) || 0) + lagerIn(p, sid) : (bestandIn(p, sid) || 0);
try{ sOrt = localStorage.getItem("kaffeekasse.sortOrt") || "kuehlschrank"; }catch(e){}
if (!ORT_NAME[sOrt]) sOrt = "kuehlschrank";

function renderProdTable(){
  const ids = vShopIds(), einShop = ids.length === 1 ? ids[0] : null;
  const q = ($("#pSuche").value || "").toLowerCase().trim();
  $$("#sortOrt [data-o]").forEach(b => b.classList.toggle("active", b.dataset.o === sOrt));
  $("#newProdBtn").disabled = !ids.length;
  $("#inventurBtn").disabled = !ids.length;
  $("#inventurBtn").textContent = sOrt === "gesamt" ? "Inventur …" : "Inventur " + ORT_NAME[sOrt];
  const imShop = D.produkte.filter(p => ids.some(sid => gefuehrtIn(p, sid)));
  const nicht = D.produkte.filter(p => !ids.some(sid => gefuehrtIn(p, sid)));
  const kats = [...new Set(imShop.map(p => p.kat))].sort((a,b) => a.localeCompare(b, "de"));
  if (sKat !== "Alle" && sKat !== "__nicht" && !kats.includes(sKat)) sKat = "Alle";
  chips("#sortKats", [["Alle","Alle",imShop.length], ...kats.map(k => [k,k,imShop.filter(p => p.kat === k).length]),
    ...(nicht.length ? [["__nicht", einShop ? "Nicht in diesem Shop" : "Nicht geführt", nicht.length]] : [])], sKat, c => { sKat = c; renderProdTable(); });
  const summeB = p => ids.reduce((a,sid) => a + mengeIn(p, sid), 0);
  const reihenIndex = new Map(nachReihe(D.produkte, einShop ?? -1).map((p, i) => [p.id, i]));
  const cmp = {
    reihe: (a,b) => reihenIndex.get(a.id) - reihenIndex.get(b.id),
    name: (a,b) => a.name.localeCompare(b.name, "de"),
    kat: (a,b) => a.kat.localeCompare(b.kat, "de") || a.name.localeCompare(b.name, "de"),
    bestand: (a,b) => summeB(a) - summeB(b) || a.name.localeCompare(b.name, "de"),
    preis: (a,b) => a.preis - b.preis || a.name.localeCompare(b.name, "de"),
    "preis-": (a,b) => b.preis - a.preis || a.name.localeCompare(b.name, "de")
  }[$("#pSort").value];
  const liste = (sKat === "__nicht" ? nicht : imShop.filter(p => sKat === "Alle" || p.kat === sKat))
    .filter(p => !q || p.name.toLowerCase().includes(q) || p.kat.toLowerCase().includes(q)).sort(cmp);
  const summeAlle = liste.reduce((a,p) => a + summeB(p), 0);
  $("#pAnzahl").textContent = liste.length + (liste.length === 1 ? " Produkt" : " Produkte") + (sKat === "__nicht" ? "" : ` · ${summeAlle} Stk ${sOrt === "gesamt" ? "gesamt" : "im " + ORT_NAME[sOrt]}`);
  const min = D.einstellungen.minBestand;
  const pill = (n, warnen) => `<span class="pill num ${warnen && n <= 0 ? "pill-red" : warnen && n <= min ? "pill-orange" : ""}">${n} Stk</span>`;
  // je Shop: Kühlschrank- und/oder Lagerzahl je nach Ansicht
  const bestandHtml = (p, sid) => sOrt === "gesamt"
    ? `<span class="ort-zahlen"><span class="ort-k" title="Kühlschrank">❄ ${pill(bestandIn(p, sid) || 0, true)}</span><span class="ort-l" title="Lager">▦ ${pill(lagerIn(p, sid), false)}</span></span>`
    : pill(mengeIn(p, sid), sOrt === "kuehlschrank");
  const hauptaktion = sKat === "__nicht" ? (einShop ? "Hier führen" : "Führen …") : sOrt === "lager" ? "Wareneingang" : "Auffüllen";
  $("#prodListe").innerHTML = liste.map(p => {
    const inShops = ids.filter(sid => gefuehrtIn(p, sid));
    const wert = sKat === "__nicht" ? `<span class="pill">nicht geführt</span>`
      : einShop ? bestandHtml(p, einShop)
      : `<div class="bestand-je">${inShops.map(sid => `<span class="small muted">${esc(shopName(sid))}</span> ${bestandHtml(p, sid)}`).join("")}</div>`;
    const lagerHinweis = sOrt === "kuehlschrank" && einShop && sKat !== "__nicht" ? ` · im Lager <span class="num">${lagerIn(p, einShop)}</span>` : "";
    return listeZeile({
      id: p.id, avatar: `<span class="line-thumb">${art(p)}</span>`, klasse: "mit-bild",
      titel: esc(p.name),
      zeilen: [`${esc(p.kat)} · <span class="num">${EUR(p.preis)}</span>` + (p.ek != null ? ` · EK <span class="num">${EUR(p.ek)}</span>` : ` · <span style="color:var(--orange-text)">kein Einkaufspreis</span>`) + lagerHinweis],
      wert, aktion: hauptaktion, menu: true
    });
  }).join("") || `<div class="empty">${ids.length ? "Keine Produkte gefunden." : "Dir ist noch kein Shop zugeordnet – das macht ein Administrator unter „Shops“."}</div>`;
  const finde = id => D.produkte.find(p => p.id === +id);
  listeVerdrahten("#prodListe", id => {
    const p = finde(id);
    if (sKat === "__nicht") return einShop ? fuehren(p, einShop, true) : prodDialog(p);
    if (sOrt === "lager") eingangDialog(p, einShop); else auffuellenDialog(p, einShop);
  }, (id, b) => {
    const p = finde(id);
    const e = [];
    if (sKat !== "__nicht"){
      e.push(["↑", "Kühlschrank auffüllen (aus dem Lager)", () => auffuellenDialog(p, einShop)]);
      e.push(["↓", "Ins Lager zurücklegen (aus dem Kühlschrank)", () => zuruecklagernDialog(p, einShop)]);
      e.push(["+", "Wareneingang buchen", () => eingangDialog(p, einShop)]);
    }
    e.push(["✎", "Bearbeiten (Name, Preise, Bild, Shops)", () => prodDialog(p)]);
    if (einShop) e.push(gefuehrtIn(p, einShop)
      ? ["⊘", "In " + shopName(einShop) + " nicht mehr führen", () => fuehren(p, einShop, false)]
      : ["✓", "In " + shopName(einShop) + " führen", () => fuehren(p, einShop, true)]);
    e.push(["≡", "Lagerbewegungen", () => lagerDialog(p)]);
    if (istInhaber()) e.push(["🗑", "Aus dem Katalog löschen", () => confirmBox("Produkt löschen?",
      p.name + " verschwindet aus allen Shops. Bereits gebuchte Verkäufe bleiben erhalten.", "Löschen",
      async () => { if (await tu("DELETE", "/api/produkte/" + p.id)) aktualisieren(); }, true), true]);
    menuOeffnen(b, p.name, e, "p" + id);
  });
}
$("#pSuche").addEventListener("input", renderProdTable);
$("#pSort").onchange = renderProdTable;
$("#newProdBtn").onclick = () => prodDialog(null);
$("#inventurBtn").onclick = () => sOrt === "gesamt" ? inventurWahl() : inventurDialog(null, sOrt);
$("#reiheBtn").onclick = () => reihenfolgeDialog();

/* ---------- Eigene Reihenfolge je Shop: ziehen (⋮⋮) oder mit ↑/↓ ---------- */
function reihenfolgeDialog(sid){
  sid = sid || (vShop !== "alle" ? vShop : null);
  if (!sid){
    openModal(`<h2>Reihenfolge</h2><p class="muted" style="margin:6px 0 14px">Für welchen Shop? Jeder Shop hat seine eigene Reihenfolge.</p>
      <div class="stack">${shopFeld("rfShop", null)}</div>
      <div class="modal-actions"><button class="btn" id="rfAb">Abbrechen</button><button class="btn btn-primary" id="rfWeiter">Weiter</button></div>`);
    $("#rfAb").onclick = closeModal;
    $("#rfWeiter").onclick = () => reihenfolgeDialog(+$("#rfShop").value);
    return;
  }
  let liste = nachReihe(D.produkte.filter(p => gefuehrtIn(p, sid)), sid);
  openModal(`<h2>Reihenfolge · ${esc(shopName(sid))}</h2>
    <p class="muted small" style="margin:6px 0 10px">So erscheinen die Produkte im Einkauf – am besten so, wie sie im Kühlschrank stehen.
      Am Griff <b>⋮⋮</b> ziehen oder mit ↑ / ↓ verschieben.</p>
    <div class="row" style="margin-bottom:10px">
      <button class="btn btn-sm" id="rfKat">Nach Kategorie ordnen</button>
      <button class="btn btn-sm" id="rfName">Nach Name ordnen</button>
    </div>
    <ol class="reihe-liste" id="rfListe"></ol>
    <div class="modal-actions"><button class="btn" id="rfAb">Abbrechen</button><button class="btn btn-primary" id="rfOk">Speichern</button></div>`);
  const zeichnen = () => {
    $("#rfListe").innerHTML = liste.map((p, i) => `<li data-i="${i}">
      <span class="griff" title="Ziehen zum Verschieben" aria-hidden="true">⋮⋮</span>
      <span class="line-thumb">${art(p)}</span>
      <span class="rf-name"><b>${esc(p.name)}</b><span class="small muted">${esc(p.kat)}</span></span>
      <span class="num muted small rf-nr">${i + 1}</span>
      <button class="btn btn-sm btn-ghost" data-hoch="${i}" ${i === 0 ? "disabled" : ""} aria-label="nach oben">↑</button>
      <button class="btn btn-sm btn-ghost" data-runter="${i}" ${i === liste.length - 1 ? "disabled" : ""} aria-label="nach unten">↓</button></li>`).join("");
  };
  const tauschen = (a, b) => { const x = liste[a]; liste[a] = liste[b]; liste[b] = x; zeichnen(); };
  $("#rfListe").addEventListener("click", e => {
    const h = e.target.closest("[data-hoch]"), r = e.target.closest("[data-runter]");
    if (h) tauschen(+h.dataset.hoch, +h.dataset.hoch - 1);
    if (r) tauschen(+r.dataset.runter, +r.dataset.runter + 1);
  });
  // Ziehen mit Maus oder Finger (Pointer-Events)
  $("#rfListe").addEventListener("pointerdown", e => {
    const griff = e.target.closest(".griff"); if (!griff) return;
    e.preventDefault();
    const li = griff.closest("li"); let von = +li.dataset.i;
    li.classList.add("zieht");
    griff.setPointerCapture(e.pointerId);
    const bewegen = ev => {
      const ziel = document.elementFromPoint(ev.clientX, ev.clientY);
      const ueber = ziel && ziel.closest("#rfListe li");
      if (!ueber || ueber === li) return;
      const nach = +ueber.dataset.i;
      const [p] = liste.splice(von, 1); liste.splice(nach, 0, p);
      von = nach; zeichnen();
      const neu = $(`#rfListe li[data-i="${nach}"]`); neu.classList.add("zieht");
      const box = $("#rfListe").closest(".modal"), r = neu.getBoundingClientRect();   // mitscrollen
      if (r.bottom > window.innerHeight - 40) box.scrollBy(0, 40); else if (r.top < 60) box.scrollBy(0, -40);
    };
    const ende = () => { griff.removeEventListener("pointermove", bewegen); zeichnen(); };
    griff.addEventListener("pointermove", bewegen);
    griff.addEventListener("pointerup", ende, {once:true});
    griff.addEventListener("pointercancel", ende, {once:true});
  });
  $("#rfKat").onclick = () => { liste.sort((a,b) => a.kat.localeCompare(b.kat, "de") || a.name.localeCompare(b.name, "de")); zeichnen(); };
  $("#rfName").onclick = () => { liste.sort((a,b) => a.name.localeCompare(b.name, "de")); zeichnen(); };
  $("#rfAb").onclick = closeModal;
  $("#rfOk").onclick = async () => {
    busy($("#rfOk"), true);
    const r = await tu("PUT", `/api/shops/${sid}/reihenfolge`, {produkte: liste.map(p => p.id)});
    busy($("#rfOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
  zeichnen();
}
$$("#sortOrt [data-o]").forEach(b => b.onclick = () => {
  sOrt = b.dataset.o;
  try{ localStorage.setItem("kaffeekasse.sortOrt", sOrt); }catch(e){}
  renderProdTable();
});

async function fuehren(p, sid, an){
  const los = async () => { if (await tu("PUT", `/api/produkte/${p.id}/fuehren`, {shop:sid, an})) aktualisieren(); };
  if (an) return los();
  const b = (bestandIn(p, sid) || 0) + lagerIn(p, sid);
  confirmBox("Nicht mehr führen?", `${p.name} verschwindet aus ${shopName(sid)}.` + (b ? ` Der Restbestand von ${b} Stück (Kühlschrank und Lager) bleibt gespeichert und ist wieder da, wenn du es erneut führst.` : ""),
    "Nicht mehr führen", los, true);
}

/* Shop wählen, wenn in der Verwaltung "Alle Shops" eingestellt ist */
function shopFeld(id, vorschlag, nurGefuehrt){
  const liste = meineShops().filter(s => !nurGefuehrt || nurGefuehrt(s.id));
  if (!liste.length) return "";
  const wahl = liste.some(s => s.id === vorschlag) ? vorschlag : liste[0].id;
  return `<div><label class="label" for="${id}">Shop</label><select id="${id}" class="field" ${liste.length < 2 ? "disabled" : ""}>
    ${liste.map(s => `<option value="${s.id}" ${s.id === wahl ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>`;
}
/* Schalter Lager/Kühlschrank in Dialogen */
function ortWahl(id, wert){
  return `<div class="seg ort-seg" id="${id}">${["lager","kuehlschrank"].map(o =>
    `<button type="button" data-o="${o}" class="${o === wert ? "active" : ""}">${o === "lager" ? "▦ Lager" : "❄ Kühlschrank"}</button>`).join("")}</div>`;
}
function ortWahlVerdrahten(id, onChange){
  $$(`#${id} [data-o]`).forEach(b => b.onclick = () => { $$(`#${id} [data-o]`).forEach(x => x.classList.toggle("active", x === b)); onChange && onChange(); });
}
const ortGewaehlt = id => ($(`#${id} .active`) || {}).dataset?.o || "lager";

function eingangDialog(p, sid){
  sid = sid || (vShop !== "alle" ? vShop : null);
  openModal(`<h2>Wareneingang</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(p.name)} – neue Ware kommt normalerweise ins Lager.</p>
    <div class="stack">
      ${shopFeld("weShop", sid)}
      <div><label class="label">Wohin?</label>${ortWahl("weOrt", "lager")}</div>
      <div class="grid-2">
        <div><label class="label" for="weMenge">Menge (Stück)</label><input id="weMenge" class="field num" type="number" min="1" step="1" inputmode="numeric" placeholder="z. B. 24"></div>
        <div><label class="label" for="weEk">Einkaufspreis je Stück (€)</label><input id="weEk" class="field num" type="number" min="0" step="0.01" inputmode="decimal" value="${p.ek != null ? p.ek : ""}" placeholder="optional"></div>
      </div>
      <div class="row">${[6,12,24,48].map(n => `<button class="btn btn-sm" data-n="${n}">${n}</button>`).join("")}</div>
      <p class="small muted" id="weInfo" style="margin:0"></p>
    </div>
    <div class="modal-actions"><button class="btn" id="weAb">Abbrechen</button><button class="btn btn-primary" id="weOk">Buchen</button></div>`);
  const info = () => {
    const s = +$("#weShop").value, m = parseInt($("#weMenge").value) || 0, ort = ortGewaehlt("weOrt");
    const alt = mengeIn(p, s, ort);
    $("#weInfo").textContent = `${ORT_NAME[ort]} in ${shopName(s)}: ${alt}` + (m > 0 ? ` → ${alt + m}` : "") + (gefuehrtIn(p, s) ? "" : " · wird dort ab jetzt geführt");
  };
  ortWahlVerdrahten("weOrt", info);
  $$("#modal [data-n]").forEach(b => b.onclick = () => { $("#weMenge").value = b.dataset.n; info(); });
  $("#weMenge").addEventListener("input", info); $("#weShop").addEventListener("change", info); info();
  $("#weAb").onclick = closeModal;
  $("#weOk").onclick = async () => {
    const menge = parseInt($("#weMenge").value);
    if (!(menge > 0)){ toast("Bitte eine Menge eingeben.","err"); return; }
    const ek = $("#weEk").value.trim();
    busy($("#weOk"), true);
    const r = await tu("POST", `/api/produkte/${p.id}/eingang`, {shop:+$("#weShop").value, menge, ort:ortGewaehlt("weOrt"), ek: ek === "" ? null : parseFloat(ek)});
    busy($("#weOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
}

/* Kühlschrank aus dem Lager derselben Halle auffüllen */
function auffuellenDialog(p, sid){
  sid = sid || (vShop !== "alle" ? vShop : null);
  openModal(`<h2>Kühlschrank auffüllen</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(p.name)} – aus dem Lager in den Kühlschrank.</p>
    <div class="stack">
      ${shopFeld("afShop", sid, s => gefuehrtIn(p, s))}
      <div class="umlager">
        <div><span class="small muted">▦ Lager</span><b class="num" id="afLager">–</b></div>
        <div class="pfeil">→</div>
        <div><span class="small muted">❄ Kühlschrank</span><b class="num" id="afKuehl">–</b></div>
      </div>
      <div><label class="label" for="afMenge">Menge (Stück)</label><input id="afMenge" class="field num" type="number" min="1" step="1" inputmode="numeric"></div>
      <div class="row" id="afSchnell"></div>
      <p class="small" id="afInfo" style="margin:0"></p>
    </div>
    <div class="modal-actions">
      <button class="btn" id="afAb">Abbrechen</button>
      <button class="btn" id="afEingang">Wareneingang …</button>
      <button class="btn btn-primary" id="afOk">Auffüllen</button></div>`);
  const shopWahl = () => +($("#afShop") || {}).value || sid;
  const info = () => {
    const s = shopWahl(), lg = lagerIn(p, s), k = bestandIn(p, s) || 0, m = Math.min(parseInt($("#afMenge").value) || 0, 99999);
    $("#afLager").textContent = m > 0 ? `${lg} → ${lg - m}` : lg;
    $("#afKuehl").textContent = m > 0 ? `${k} → ${k + m}` : k;
    $("#afInfo").innerHTML = !lg ? `<span style="color:var(--orange-text)">Das Lager ist leer – erst einen Wareneingang buchen.</span>`
      : m > lg ? `<span style="color:var(--red-text)">Im Lager sind nur ${lg} Stück.</span>` : "";
    $("#afOk").disabled = !lg || !(m > 0) || m > lg;
    $("#afSchnell").innerHTML = lg ? [6,12,24].filter(n => n < lg).map(n => `<button class="btn btn-sm" data-n="${n}">${n}</button>`).join("")
      + `<button class="btn btn-sm" data-n="${lg}">Alles (${lg})</button>` : "";
    $$("#afSchnell [data-n]").forEach(b => b.onclick = () => { $("#afMenge").value = b.dataset.n; info(); });
  };
  $("#afMenge").addEventListener("input", info);
  if ($("#afShop")) $("#afShop").addEventListener("change", info);
  info();
  setTimeout(() => $("#afMenge").focus(), 60);
  $("#afAb").onclick = closeModal;
  $("#afEingang").onclick = () => eingangDialog(p, shopWahl());
  $("#afOk").onclick = async () => {
    const menge = parseInt($("#afMenge").value);
    busy($("#afOk"), true);
    const r = await tu("POST", `/api/produkte/${p.id}/auffuellen`, {shop:shopWahl(), menge});
    busy($("#afOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
}

/* Platz im Kühlschrank schaffen: zurück ins Lager derselben Halle */
function zuruecklagernDialog(p, sid){
  sid = sid || (vShop !== "alle" ? vShop : null);
  openModal(`<h2>Ins Lager zurücklegen</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(p.name)} – aus dem Kühlschrank zurück ins Lager, z. B. um Platz für Neues zu schaffen.</p>
    <div class="stack">
      ${shopFeld("zlShop", sid, s => gefuehrtIn(p, s))}
      <div class="umlager">
        <div><span class="small muted">❄ Kühlschrank</span><b class="num" id="zlKuehl">–</b></div>
        <div class="pfeil">→</div>
        <div><span class="small muted">▦ Lager</span><b class="num" id="zlLager">–</b></div>
      </div>
      <div><label class="label" for="zlMenge">Menge (Stück)</label><input id="zlMenge" class="field num" type="number" min="1" step="1" inputmode="numeric"></div>
      <div class="row" id="zlSchnell"></div>
      <p class="small" id="zlInfo" style="margin:0"></p>
    </div>
    <div class="modal-actions">
      <button class="btn" id="zlAb">Abbrechen</button>
      <button class="btn btn-primary" id="zlOk">Ins Lager legen</button></div>`);
  const shopWahl = () => +($("#zlShop") || {}).value || sid;
  const info = () => {
    const s = shopWahl(), lg = lagerIn(p, s) || 0, k = Math.max(bestandIn(p, s) || 0, 0), m = Math.min(parseInt($("#zlMenge").value) || 0, 99999);
    $("#zlKuehl").textContent = m > 0 ? `${k} → ${k - m}` : k;
    $("#zlLager").textContent = m > 0 ? `${lg} → ${lg + m}` : lg;
    $("#zlInfo").innerHTML = !k ? `<span style="color:var(--orange-text)">Im Kühlschrank ist nichts davon.</span>`
      : m > k ? `<span style="color:var(--red-text)">Im Kühlschrank sind nur ${k} Stück.</span>` : "";
    $("#zlOk").disabled = !k || !(m > 0) || m > k;
    $("#zlSchnell").innerHTML = k ? [6,12,24].filter(n => n < k).map(n => `<button class="btn btn-sm" data-n="${n}">${n}</button>`).join("")
      + `<button class="btn btn-sm" data-n="${k}">Alles (${k})</button>` : "";
    $$("#zlSchnell [data-n]").forEach(b => b.onclick = () => { $("#zlMenge").value = b.dataset.n; info(); });
  };
  $("#zlMenge").addEventListener("input", info);
  if ($("#zlShop")) $("#zlShop").addEventListener("change", info);
  info();
  setTimeout(() => $("#zlMenge").focus(), 60);
  $("#zlAb").onclick = closeModal;
  $("#zlOk").onclick = async () => {
    const menge = parseInt($("#zlMenge").value);
    busy($("#zlOk"), true);
    const r = await tu("POST", `/api/produkte/${p.id}/zuruecklagern`, {shop:shopWahl(), menge});
    busy($("#zlOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
}

function inventurWahl(){
  openModal(`<h2>Inventur</h2><p class="muted" style="margin:6px 0 14px">Was wird gezählt?</p>
    <div class="stack">${ortWahl("ivOrt", "kuehlschrank")}</div>
    <div class="modal-actions"><button class="btn" id="ivAb">Abbrechen</button><button class="btn btn-primary" id="ivWeiter">Weiter</button></div>`);
  ortWahlVerdrahten("ivOrt");
  $("#ivAb").onclick = closeModal;
  $("#ivWeiter").onclick = () => inventurDialog(null, ortGewaehlt("ivOrt"));
}
function inventurDialog(sid, ort = "kuehlschrank"){
  sid = sid || (vShop !== "alle" ? vShop : null);
  if (!sid){
    openModal(`<h2>Inventur ${ORT_NAME[ort]}</h2><p class="muted" style="margin:6px 0 14px">In welchem Shop wird gezählt?</p>
      <div class="stack">${shopFeld("ivShop", null)}</div>
      <div class="modal-actions"><button class="btn" id="ivAb">Abbrechen</button><button class="btn btn-primary" id="ivWeiter">Weiter</button></div>`);
    $("#ivAb").onclick = closeModal;
    $("#ivWeiter").onclick = () => inventurDialog(+$("#ivShop").value, ort);
    return;
  }
  const soll = p => mengeIn(p, sid, ort);
  const liste = D.produkte.filter(p => gefuehrtIn(p, sid)).sort((a,b) => a.kat.localeCompare(b.kat, "de") || a.name.localeCompare(b.name, "de"));
  let kat = null;
  openModal(`<h2>Inventur ${ORT_NAME[ort]} · ${esc(shopName(sid))}</h2>
    <p class="muted small" style="margin:6px 0 12px">Gezählten Bestand ${ort === "lager" ? "im Lager" : "im Kühlschrank"} eintragen. Leere Felder bleiben unverändert. Abweichungen landen im Lagerbuch (Schwund bzw. Mehrbestand).</p>
    <table class="inventur"><thead><tr><th>Produkt</th><th class="t-right">Soll</th><th class="t-right">Gezählt</th><th class="t-right">Diff.</th></tr></thead><tbody>
    ${liste.map(p => { const kopf = p.kat !== kat ? `<tr class="kat-zeile"><td colspan="4">${esc(kat = p.kat)}</td></tr>` : "";
      return kopf + `<tr><td>${esc(p.name)}</td><td class="t-right num">${soll(p)}</td>
      <td class="t-right"><input class="field num iv" data-pid="${p.id}" data-soll="${soll(p)}" type="number" min="0" step="1" inputmode="numeric"></td>
      <td class="t-right num diff" data-d="${p.id}"></td></tr>`; }).join("")}</tbody></table>
    <p class="small" id="ivSumme" style="margin:12px 0 0"></p>
    <div class="modal-actions"><button class="btn" id="ivAb">Abbrechen</button><button class="btn btn-primary" id="ivOk">Inventur buchen</button></div>`);
  $("#modal").classList.add("modal-breit");
  const rechnen = () => {
    let n = 0, wert = 0;
    $$("#modal .iv").forEach(i => {
      const d = i.value === "" ? 0 : (parseInt(i.value) || 0) - +i.dataset.soll;
      const z = $(`#modal [data-d="${i.dataset.pid}"]`);
      z.textContent = i.value === "" || !d ? "" : (d > 0 ? "+" : "") + d;
      z.style.color = d < 0 ? "var(--red-text)" : "var(--green-text)";
      if (d){ n++; const p = D.produkte.find(x => x.id === +i.dataset.pid); wert += d * (p.ek || 0); }
    });
    $("#ivSumme").textContent = n ? `${n} Abweichungen · Wert ${EUR(wert)}` : "";
  };
  $$("#modal .iv").forEach(i => i.addEventListener("input", rechnen));
  $("#ivAb").onclick = () => { $("#modal").classList.remove("modal-breit"); closeModal(); };
  $("#ivOk").onclick = async () => {
    const zaehlung = $$("#modal .iv").filter(i => i.value !== "").map(i => ({pid:+i.dataset.pid, ist:parseInt(i.value) || 0}));
    if (!zaehlung.length){ toast("Es wurde nichts eingetragen.","err"); return; }
    busy($("#ivOk"), true);
    const r = await tu("POST", "/api/inventur", {shop:sid, ort, zaehlung});
    busy($("#ivOk"), false);
    if (!r) return;
    $("#modal").classList.remove("modal-breit");
    closeModal();
    aktualisieren();
  };
}

const ORT_TEXT = {kuehlschrank:"Kühlschrank", lager:"Lager", umlagerung:"Lager → Kühlschrank", rueckumlagerung:"Kühlschrank → Lager"};
const UMLAGERUNG = ["Auffüllen", "Zurücklagern"];
async function lagerDialog(p){
  const ids = vShopIds();
  const zeilen = await tu("GET", `/api/lagerbuch?von=0&produkt=${p.id}` + shopParam());
  if (!zeilen) return;
  const stand = ids.filter(sid => gefuehrtIn(p, sid)).map(sid =>
    `${ids.length > 1 ? esc(shopName(sid)) + ": " : ""}❄ Kühlschrank <b class="num">${bestandIn(p, sid)}</b> · ▦ Lager <b class="num">${lagerIn(p, sid)}</b>`).join("<br>");
  openModal(`<h2>Lagerbewegungen</h2><p class="muted" style="margin:6px 0 10px">${esc(p.name)} · Wareneingänge, Auffüllen und Inventur. Verkäufe stehen unter „Verkäufe“.</p>
    ${stand ? `<p class="small" style="margin:0 0 12px">${stand}</p>` : ""}
    ${zeilen.length ? `<table>${zeilen.map(z => `<tr><td class="small muted num" style="white-space:nowrap">${dt(z.zeit)}</td>
      <td class="small">${esc(z.art)} · ${esc(ORT_TEXT[z.ort] || z.ort)}${ids.length > 1 ? " · " + esc(shopName(z.shop_id)) : ""}<br><span class="muted">${esc(z.von)}${z.notiz && !UMLAGERUNG.includes(z.art) ? " · " + esc(z.notiz) : ""}${z.ek != null && z.art === "Eingang" ? " · EK " + EUR(z.ek) : ""}</span></td>
      <td class="t-right num" style="color:${UMLAGERUNG.includes(z.art) ? "var(--muted)" : z.menge < 0 ? "var(--red-text)" : "var(--green-text)"}">${UMLAGERUNG.includes(z.art) ? "" : z.menge > 0 ? "+" : ""}${z.menge}</td></tr>`).join("")}</table>`
      : `<div class="empty">Noch keine Lagerbewegungen.</div>`}
    <div class="modal-actions"><button class="btn btn-primary" id="lgZu">Schließen</button></div>`);
  $("#lgZu").onclick = closeModal;
}

/* Foto im Browser verkleinern, damit die Datenbank klein bleibt */
/* Foto für die Datenbank vorbereiten.
   - Kleine PNG/WebP/JPEG-Dateien werden UNVERÄNDERT übernommen (Transparenz bleibt garantiert erhalten).
   - Große Fotos (z. B. vom Handy) werden verkleinert; PNG/WebP bleiben dabei transparent. */
const MAX_ORIGINAL = 600 * 1024;
function dateiAlsDataUrl(file){
  return new Promise((ok, fail) => { const r = new FileReader(); r.onerror = fail; r.onload = () => ok(r.result); r.readAsDataURL(file); });
}
async function fotoVerkleinern(file){
  const typ = (file.type || "").toLowerCase();
  const mitAlpha = /png|webp|gif/.test(typ) || /\.(png|webp|gif)$/i.test(file.name || "");
  if (/^image\/(png|webp|jpeg)$/.test(typ) && file.size <= MAX_ORIGINAL) return dateiAlsDataUrl(file);
  const quelle = await dateiAlsDataUrl(file);
  const img = await new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => fail(new Error("Bild nicht lesbar")); i.src = quelle; });
  const max = 600, s = Math.min(1, max / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(img.width * s)); c.height = Math.max(1, Math.round(img.height * s));
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.drawImage(img, 0, 0, c.width, c.height);
  if (!mitAlpha) return c.toDataURL("image/jpeg", 0.85);
  const webp = c.toDataURL("image/webp", 0.9);
  return webp.startsWith("data:image/webp") ? webp : c.toDataURL("image/png");  // Safari erzeugt kein WebP → PNG
}


function prodDialog(p){
  const neu = !p;
  const erlaubt = meineShops();
  const vorschlag = vShop !== "alle" ? [vShop] : erlaubt.map(s => s.id);
  p = p || {name:"", kat:"Softdrinks", preis:1.2, ek:null, form:"dose", ml:330, farbe:"#D2122E", farbe2:"#8E0C1F", bild:null, bestand:{}};
  const kats = [...new Set([...D.produkte.map(x => x.kat), "Softdrinks","Energy","Riegel"])];
  const andere = (D.shops || []).filter(s => !erlaubt.some(e => e.id === s.id) && gefuehrtIn(p, s.id));
  openModal(`<h2>${neu ? "Neues Produkt" : "Produkt bearbeiten"}</h2>
    <p class="muted small" style="margin:6px 0 0">Der Katalog gilt für alle Shops – Name, Preis und Bild sind überall gleich, der Bestand zählt je Shop.</p>
    <div class="stack" style="margin-top:14px">
      <div><label class="label" for="pName">Name</label><input id="pName" class="field" value="${esc(p.name)}" maxlength="80"></div>
      <div class="grid-2">
        <div><label class="label" for="pKatSel">Kategorie</label>
          <select id="pKatSel" class="field">
            ${kats.map(k => `<option value="${esc(k)}" ${k === p.kat ? "selected" : ""}>${esc(k)}</option>`).join("")}
            <option value="__neu">+ Neue Kategorie …</option>
          </select>
          <input id="pKat" class="field" maxlength="40" placeholder="Name der neuen Kategorie" style="margin-top:8px" hidden></div>
        <div><label class="label" for="pForm">Darstellung</label>
          <select id="pForm" class="field">
            <option value="dose|330" ${p.form!=="riegel" && (p.ml||330)<500 ? "selected":""}>Dose 0,33 l</option>
            <option value="dose|500" ${p.form!=="riegel" && (p.ml||330)>=500 ? "selected":""}>Dose 0,5 l</option>
            <option value="riegel|0" ${p.form==="riegel" ? "selected":""}>Riegel</option>
          </select></div>
      </div>
      <div class="grid-2">
        <div><label class="label" for="pPreis">Verkaufspreis (€)</label><input id="pPreis" class="field num" type="number" step="0.05" min="0" value="${p.preis}"></div>
        <div><label class="label" for="pEk">Einkaufspreis je Stück (€)</label><input id="pEk" class="field num" type="number" step="0.01" min="0" value="${p.ek != null ? p.ek : ""}" placeholder="für Rohertrag"></div>
      </div>
      <div class="grid-2">
        <div><label class="label" for="pF1">Farbe oben</label><input id="pF1" class="field" type="color" value="${esc(p.farbe || "#D2122E")}" style="height:42px;padding:4px"></div>
        <div><label class="label" for="pF2">Farbe unten</label><input id="pF2" class="field" type="color" value="${esc(p.farbe2 || "#8E0C1F")}" style="height:42px;padding:4px"></div>
      </div>
      <div>
        <label class="label">Foto</label>
        <div class="filebox" id="pDrop">
          <div id="pPreview" style="height:86px;display:grid;place-items:center">${p.bild ? `<img src="${esc(p.bild)}" style="max-height:82px;border-radius:8px" alt="">` : art(p)}</div>
          <div class="small" style="margin-top:6px" id="pFotoTxt">${p.bild ? "Eigenes Foto" : "Gezeichnete Grafik – eigenes Foto überschreibt sie"}</div>
          <div class="row" style="justify-content:center;margin-top:10px">
            <button class="btn btn-sm" id="pPick">Foto wählen</button>
            <button class="btn btn-sm btn-ghost" id="pClear" ${p.bild ? "" : "hidden"}>Foto entfernen</button>
          </div>
          <input type="file" id="pFile" accept="image/*" hidden>
        </div>
      </div>
      <div><label class="label">In diesen Shops führen</label>
        <div class="check-liste">${erlaubt.map(s => `<label><input type="checkbox" value="${s.id}" ${(neu ? vorschlag.includes(s.id) : gefuehrtIn(p, s.id)) ? "checked" : ""}>
          ${esc(s.name)}${neu ? "" : ` <span class="muted small num">· Bestand ${gefuehrtIn(p, s.id) ? bestandIn(p, s.id) : "–"}</span>`}</label>`).join("")}</div>
        ${andere.length ? `<p class="muted small" style="margin:8px 0 0">Außerdem geführt in: ${andere.map(s => esc(s.name)).join(", ")}</p>` : ""}</div>
      ${neu && erlaubt.length ? `<div class="grid-2">${shopFeld("pAnfShop", vShop !== "alle" ? vShop : null)}
        <div><label class="label" for="pAnfang">Anfangsbestand <span class="muted">(Wareneingang)</span></label><input id="pAnfang" class="field num" type="number" min="0" step="1" value="0"></div></div>
        <div><label class="label">Anfangsbestand kommt ins</label>${ortWahl("pAnfOrt", "lager")}</div>` : ""}
    </div>
    <div class="modal-actions"><button class="btn" id="pAb">Abbrechen</button>
      <button class="btn btn-primary" id="pOk">Speichern</button></div>`);

  if ($("#pAnfOrt")) ortWahlVerdrahten("pAnfOrt");
  $("#pKatSel").onchange = () => {
    const n = $("#pKatSel").value === "__neu";
    $("#pKat").hidden = !n;
    if (n) $("#pKat").focus();
  };
  // bild: undefined = unverändert, "" = entfernen, data:-URL = neu
  let bild;
  const vorschau = () => {
    const [form, ml] = $("#pForm").value.split("|");
    return art({form, ml:+ml, farbe:$("#pF1").value, farbe2:$("#pF2").value});
  };
  ["#pForm","#pF1","#pF2"].forEach(s => $(s).addEventListener("input", () => {
    if (bild === "" || (bild === undefined && !p.bild)) $("#pPreview").innerHTML = vorschau();
  }));
  $("#pPick").onclick = () => $("#pFile").click();
  $("#pFile").onchange = async e => {
    const f = e.target.files[0];
    if (!f) return;
    try{
      bild = await fotoVerkleinern(f);
      $("#pPreview").innerHTML = `<img src="${bild}" style="max-height:82px;border-radius:8px" alt="">`;
      $("#pFotoTxt").textContent = "Eigenes Foto";
      $("#pClear").hidden = false;
    }catch(err){ toast("Das Foto konnte nicht gelesen werden.","err"); }
  };
  $("#pClear").onclick = () => {
    bild = "";
    $("#pPreview").innerHTML = vorschau();
    $("#pFotoTxt").textContent = "Gezeichnete Grafik – eigenes Foto überschreibt sie";
    $("#pClear").hidden = true;
  };
  $("#pAb").onclick = closeModal;
  $("#pOk").onclick = async () => {
    const name = $("#pName").value.trim();
    if (!name){ toast("Bitte einen Namen eingeben.","err"); return; }
    if ($("#pKatSel").value === "__neu" && !$("#pKat").value.trim()){ toast("Bitte einen Namen für die neue Kategorie eingeben.","err"); return; }
    const [form, ml] = $("#pForm").value.split("|");
    const ek = $("#pEk").value.trim();
    const daten = {
      name, kat:($("#pKatSel").value === "__neu" ? $("#pKat").value.trim() : $("#pKatSel").value) || "Sonstiges", form, ml:+ml,
      farbe:$("#pF1").value, farbe2:$("#pF2").value,
      preis:Math.max(0, parseFloat($("#pPreis").value) || 0), ek: ek === "" ? null : Math.max(0, parseFloat(ek) || 0),
      fuehren:$$("#modal .check-liste input:checked").map(i => +i.value)
    };
    if (bild !== undefined) daten.bild = bild;
    if (neu && $("#pAnfang")){ daten.anfang = Math.max(0, parseInt($("#pAnfang").value) || 0); daten.shop = +$("#pAnfShop").value; daten.anfangOrt = ortGewaehlt("pAnfOrt"); }
    busy($("#pOk"), true);
    const r = neu ? await tu("POST", "/api/produkte", daten) : await tu("PUT", "/api/produkte/" + p.id, daten);
    busy($("#pOk"), false);
    if (!r) return;
    closeModal();
    aktualisieren();
  };
}

/* =========================================================
   Mitglieder (alle Konten: Mitglieder, Shopverantwortliche, Administratoren)
   ========================================================= */
let mRolle = "alle";
const ROLLEN_FILTER = [["alle","Alle"],["kunde","Mitglieder"],["personal","Shopverantwortliche"],["inhaber","Administratoren"],["still","Stillgelegt"]];
// Stillgelegte Konten erscheinen nur unter „Stillgelegt“
const passtFilter = (k, r) => r === "still" ? !!k.stillgelegt : !k.stillgelegt && (r === "alle" || k.rolle === r);
/* Vier-Augen-Prinzip: Shopverantwortliche buchen nicht auf ihr eigenes Konto */
const selbstGesperrt = k => k.id === user.id && !istInhaber();
const darfBearbeiten = k => istInhaber() || k.rolle === "kunde" || k.id === user.id;

function renderCustomers(){
  const q = ($("#custSearch").value || "").toLowerCase().trim();
  chips("#rollenFilter", ROLLEN_FILTER.map(([r,l]) => [r, l, D.konten.filter(k => passtFilter(k, r)).length]),
    mRolle, r => { mRolle = r; renderCustomers(); });
  const sortierung = $("#custSort").value;
  const nachVor = (a,b) => a.nachname.localeCompare(b.nachname, "de") || a.vorname.localeCompare(b.vorname, "de");
  const cmp = {
    "nachname": nachVor,
    "nachname-": (a,b) => -nachVor(a,b),
    "vorname": (a,b) => a.vorname.localeCompare(b.vorname, "de") || a.nachname.localeCompare(b.nachname, "de"),
    "nummer": (a,b) => a.nummer.localeCompare(b.nummer),
    "tel": (a,b) => (!a.tel) - (!b.tel) || a.tel.localeCompare(b.tel) || nachVor(a,b),
    "guthaben": (a,b) => a.guthaben - b.guthaben || nachVor(a,b),
    "guthaben-": (a,b) => b.guthaben - a.guthaben || nachVor(a,b)
  }[sortierung] || nachVor;
  const liste = D.konten
    .filter(k => passtFilter(k, mRolle))
    .filter(k => !q || [k.vorname, k.nachname, k.name].some(x => (x || "").toLowerCase().includes(q)) || k.nummer.includes(q) || (k.tel || "").includes(q))
    .sort(cmp);
  // Beim Sortieren nach Vorname "Vorname Nachname", sonst "Nachname, Vorname"
  const anzeige = k => sortierung === "vorname" ? k.name : (k.vorname ? `${k.nachname}, ${k.vorname}` : k.nachname);
  $("#mAnzahl").textContent = liste.length + (liste.length === 1 ? " Konto" : " Konten");

  $("#custList").innerHTML = liste.map(k => {
    const gesperrt = schuldenGesperrt(k);
    const cls = gesperrt ? "pill-red" : (k.guthaben < 0 ? "pill-orange" : "pill-green");
    const kontakt = k.raum || "";
    const status = [!k.hatPin ? "noch keine PIN" : k.pinMussAendern ? "PIN zurückgesetzt" : "",
                    istTeam(k) && k.pwMussAendern ? "Passwort zurückgesetzt" : "",
                    gesperrt ? "Konto gesperrt" : "", k.gesperrt ? "Login gesperrt" : "",
                    k.stillgelegt ? "stillgelegt seit " + new Date(k.stillgelegt).toLocaleDateString("de-DE") : ""].filter(Boolean).join(" · ");
    return listeZeile({
      id: k.id, avatar: esc(((k.vorname || "")[0] || "") + ((k.nachname || "?")[0])),
      titel: `${esc(anzeige(k))}${k.id === user.id ? ' <span class="muted small">(du)</span>' : ""}${istTeam(k) ? ` <span class="pill ${k.rolle === "inhaber" ? "pill-orange" : "pill-green"} rolle-pill">${rolleName(k.rolle)}</span>` : ""}`,
      zeilen: [`<span class="num">${esc(k.nummer)}</span>${k.tel ? ` · <span class="tel num" title="Interne Rufnummer">☎ ${esc(k.tel)}</span>` : ""}${status ? " · " + esc(status) : ""}`,
               istTeam(k) ? "Shops: " + esc(k.rolle === "inhaber" ? "alle" : (k.shops || []).map(shopName).join(", ") || "noch keiner zugeordnet") : "",
               kontakt ? esc(kontakt) : ""],
      wert: `<span class="pill num ${cls}">${EUR(k.guthaben)}</span>`,
      aktion: k.stillgelegt ? (darfBearbeiten(k) ? "Reaktivieren" : "") : selbstGesperrt(k) ? "" : "Aufladen", menu: true
    });
  }).join("") || `<div class="empty">Kein Mitglied gefunden.</div>`;
  const finde = id => D.konten.find(x => x.id === +id);
  listeVerdrahten("#custList", id => { const k = finde(id); k.stillgelegt ? stilllegenDialog(k) : buchenDialog(k, "auf"); },
                  (id, b) => mitgliedMenu(finde(id), b));
}
$("#custSearch").addEventListener("input", renderCustomers);
$("#custSort").onchange = renderCustomers;

function mitgliedMenu(k, anker){
  const darf = darfBearbeiten(k), chef = istInhaber(), selbst = k.id === user.id;
  menuOeffnen(anker, k.name, [
    ["−", "Betrag abbuchen", () => buchenDialog(k, "ab"), false, !selbstGesperrt(k)],
    ["≡", "Kontoverlauf", () => verlaufDialog(k), false, true],
    ["✎", "Konto", () => kontoDialogVerwaltung(k), false, darf],
    ["⇅", "Rolle ändern", () => rolleDialog(k), false, chef && !selbst],
    ["⌂", "Zuständige Shops", () => kontoShopsDialog(k), false, chef && k.rolle === "personal"],
    ["⏸", k.stillgelegt ? "Reaktivieren" : "Stilllegen", () => stilllegenDialog(k), false, darf && !selbst],
    ["🗑", "Konto löschen", () => loeschDialog(k), true, darf && !selbst]
  ].filter(x => x[4]), "m" + k.id);
}

function kontoShopsDialog(k){
  openModal(`<h2>Zuständige Shops</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(k.name)} pflegt Sortiment, Bestand und Verkäufe in diesen Shops.</p>
    <div class="check-liste">${(D.shops || []).map(x => `<label><input type="checkbox" value="${x.id}" ${(k.shops || []).includes(x.id) ? "checked" : ""}>
      ${esc(x.name)}${x.ort ? ` <span class="muted small">· ${esc(x.ort)}</span>` : ""}${x.aktiv ? "" : ` <span class="shop-tag">geschlossen</span>`}</label>`).join("")}</div>
    <div class="modal-actions"><button class="btn" id="ksAb">Abbrechen</button><button class="btn btn-primary" id="ksOk">Speichern</button></div>`);
  $("#ksAb").onclick = closeModal;
  $("#ksOk").onclick = async () => {
    const shops = $$("#modal .check-liste input:checked").map(i => +i.value);
    if (await tu("PUT", `/api/konten/${k.id}/shops`, {shops})){ closeModal(); aktualisieren(); }
  };
}


/* =========================================================
   Shops (nur Administrator)
   ========================================================= */
let shFilter = "alle";
const verantwortliche = () => (D.konten || []).filter(k => k.rolle === "personal");
function renderShops(){
  const alle = D.shops || [];
  const q = ($("#shSuche").value || "").toLowerCase().trim();
  chips("#shFilter", [["alle","Alle",alle.length],["offen","Geöffnet",alle.filter(x => x.aktiv).length],["zu","Geschlossen",alle.filter(x => !x.aktiv).length]],
    shFilter, c => { shFilter = c; renderShops(); });
  const umsatz = x => SHKZ && SHKZ.proShop[String(x.id)] ? SHKZ.proShop[String(x.id)].umsatz : 0;
  const cmp = {reihe: () => 0, name: (a,b) => a.name.localeCompare(b.name, "de"), "umsatz-": (a,b) => umsatz(b) - umsatz(a)}[$("#shSort").value];
  const liste = alle.filter(x => shFilter === "alle" || (shFilter === "offen") === x.aktiv)
    .filter(x => !q || x.name.toLowerCase().includes(q) || (x.ort || "").toLowerCase().includes(q)).slice().sort(cmp);
  $("#shopListe").innerHTML = liste.map(x => {
    const n = D.produkte.filter(p => gefuehrtIn(p, x.id)).length;
    const team = (D.konten || []).filter(kk => (x.team || []).includes(kk.id));
    const vk = SHKZ && SHKZ.proShop[String(x.id)];
    return listeZeile({
      id: x.id, avatar: "⌂", klasse: x.aktiv ? "" : "zu",
      titel: `${esc(x.name)}${x.ort ? ` <span class="muted small">· ${esc(x.ort)}</span>` : ""}`,
      zeilen: [`${n} Produkte · diesen Monat <span class="num">${EUR(vk ? vk.umsatz : 0)}</span> (${vk ? vk.verkaeufe : 0} Einkäufe)`,
               "Shopverantwortliche: " + (team.map(t => esc(t.name)).join(", ") || "noch niemand – nur Administratoren")],
      wert: `<span class="pill ${x.aktiv ? "pill-green" : "pill-red"}">${x.aktiv ? "geöffnet" : "geschlossen"}</span>`,
      aktion: "Shopverantwortliche", menu: true
    });
  }).join("") || `<div class="empty">Kein Shop gefunden.</div>`;
  const finde = id => alle.find(y => y.id === +id);
  listeVerdrahten("#shopListe", id => shopTeamDialog(finde(id)), (id, b) => {
    const x = finde(id);
    menuOeffnen(b, x.name, [
      ["✎", "Name & Ort bearbeiten", () => shopDialog(x)],
      ["⎙", "QR-Code drucken", () => qrDialog([x])],
      [x.aktiv ? "⊘" : "✓", x.aktiv ? "Schließen" : "Öffnen", () => shopAufZu(x), x.aktiv],
      ["🗑", "Löschen", () => shopLoeschen(x), true]
    ], "s" + id);
  });
}
$("#shSuche").addEventListener("input", renderShops);
$("#shSort").onchange = renderShops;
$("#neuShopBtn").onclick = () => shopDialog(null);
/* ---------- QR-Codes je Shop drucken ---------- */
const shopAdresse = (basis, x) => basis.replace(/\/+$/, "") + "/?shop=" + x.id;
/* Aushang im Format ca. A6 (100 × 143 mm): vier Stück passen auf ein A4-Blatt, Schnittlinien gestrichelt */
function qrKarte(x, url, svg){
  const tasse = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V8z" fill="currentColor"/>
    <path d="M16 10h1.5a2.5 2.5 0 0 1 0 5H16" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 21h15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M8 2.5c-.8 1 .8 2 0 3M11 2.5c-.8 1 .8 2 0 3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;
  return `<div class="qr-karte">
    <div class="qk-band"><span class="qk-tasse">${tasse}</span><span>${esc(D.einstellungen.shopName)}</span></div>
    <div class="qk-shop">${esc(x.name)}</div>
    <div class="qk-ort">${x.ort ? esc(x.ort) : "&nbsp;"}</div>
    <div class="qk-code">${svg}</div>
    <div class="qk-titel">Scannen &amp; einkaufen</div>
    <ol class="qk-schritte">
      <li><b>1</b><span>Code mit der Handy-Kamera scannen</span></li>
      <li><b>2</b><span>Personalnummer &amp; PIN eingeben</span></li>
      <li><b>3</b><span>Getränk wählen &amp; bezahlen</span></li>
    </ol>
  </div>`;
}
function qrDialog(liste){
  liste = liste.filter(Boolean);
  if (!liste.length){ toast("Kein geöffneter Shop vorhanden.","err"); return; }
  const eins = liste.length === 1;
  openModal(`<h2>${eins ? "Aushang für " + esc(liste[0].name) : "Aushänge für " + liste.length + " Shops"}</h2>
    <p class="muted small" style="margin:6px 0 14px">Format ca. A6 – vier Aushänge passen auf ein A4-Blatt und werden an den gestrichelten Linien ausgeschnitten.
      Wer den Code scannt, landet direkt im richtigen Shop; angemeldet wird damit niemand.</p>
    <div class="stack">
      <div class="grid-2">
        <div><label class="label" for="qrBasis">Adresse der Kaffeekasse</label>
          <input id="qrBasis" class="field mono" inputmode="url" value="${esc(location.origin)}"></div>
        <div><label class="label" for="qrAnzahl">Exemplare ${eins ? "" : "je Shop"}</label>
          <select id="qrAnzahl" class="field">${[1,2,3,4].map(n => `<option value="${n}" ${n === (eins ? 4 : 1) ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      </div>
      <p class="muted small" style="margin:-4px 0 0">Die Adresse so, wie die Handys die Kaffeekasse im Netz erreichen (nicht „localhost“).</p>
      <div class="qk-vorschau" id="qrVorschau"></div>
      ${eins ? "" : `<p class="muted small" style="margin:0;text-align:center">Vorschau: ${esc(liste[0].name)}</p>`}
    </div>
    <div class="modal-actions">
      <button class="btn" id="qrAb">Abbrechen</button>
      ${eins ? `<button class="btn" id="qrKopie">Adresse kopieren</button>` : ""}
      <button class="btn btn-primary" id="qrDruck">Drucken</button></div>`);
  const vorschau = async () => {
    const url = shopAdresse($("#qrBasis").value.trim(), liste[0]);
    try{ $("#qrVorschau").innerHTML = qrKarte(liste[0], url, await qrSvg(url)); }
    catch(e){ $("#qrVorschau").innerHTML = `<p class="small" style="color:var(--red-text)">${esc(e.message)}</p>`; }
  };
  let t; $("#qrBasis").addEventListener("input", () => { clearTimeout(t); t = setTimeout(vorschau, 300); });
  vorschau();
  $("#qrAb").onclick = closeModal;
  if ($("#qrKopie")) $("#qrKopie").onclick = async () => {
    try{ await navigator.clipboard.writeText(shopAdresse($("#qrBasis").value.trim(), liste[0])); }
    catch(e){ toast("Kopieren nicht möglich – bitte die Adresse abschreiben.","warn"); }
  };
  $("#qrDruck").onclick = async () => {
    const basis = $("#qrBasis").value.trim();
    if (!/^https?:\/\/[^\s/]+/.test(basis)){ toast("Bitte eine Adresse mit http:// oder https:// eingeben.","err"); return; }
    const anzahl = +$("#qrAnzahl").value || 1;
    busy($("#qrDruck"), true);
    try{
      const karten = [];
      for (const x of liste){
        const url = shopAdresse(basis, x), svg = await qrSvg(url);
        for (let n = 0; n < anzahl; n++) karten.push(qrKarte(x, url, svg));
      }
      const boegen = [];
      for (let i = 0; i < karten.length; i += 4)
        boegen.push(`<div class="qr-bogen">${karten.slice(i, i + 4).map(k => `<div class="qr-zelle">${k}</div>`).join("")}</div>`);
      $("#printarea").innerHTML = boegen.join("");
      closeModal();
      setTimeout(() => window.print(), 50);
    }catch(e){ toast(e.message, "err"); }
    finally{ busy($("#qrDruck"), false); }
  };
}
async function qrSvg(url){
  const r = await fetch("/api/qr.svg?text=" + encodeURIComponent(url), {credentials:"same-origin", headers:SITZUNG ? {"X-Sitzung":SITZUNG} : {}});
  if (!r.ok){ let j = {}; try{ j = await r.json(); }catch(e){} throw new Error(j.fehler || "QR-Code konnte nicht erstellt werden."); }
  return await r.text();
}
$("#qrAlleBtn").onclick = () => qrDialog((D.shops || []).filter(x => x.aktiv));

function shopDialog(x){
  const neu = !x;
  x = x || {name:"", ort:""};
  const vorlagen = (D.shops || []).filter(y => D.produkte.some(p => p.bestand[y.id] !== undefined));
  openModal(`<h2>${neu ? "Neuer Shop" : "Shop bearbeiten"}</h2>
    <div class="stack" style="margin-top:14px">
      <div class="grid-2">
        <div><label class="label" for="shName">Name</label><input id="shName" class="field" value="${esc(x.name)}" maxlength="40" placeholder="z. B. Halle 3"></div>
        <div><label class="label" for="shOrt">Ort / Beschreibung</label><input id="shOrt" class="field" value="${esc(x.ort)}" maxlength="80" placeholder="z. B. Werkstatt, EG"></div>
      </div>
      ${neu ? `<div><label class="label" for="shKopie">Sortiment übernehmen</label>
        <select id="shKopie" class="field"><option value="">Leer starten – Produkte später unter Sortiment führen</option>
          ${vorlagen.map(y => `<option value="${y.id}">wie ${esc(y.name)} (Bestand startet bei 0)</option>`).join("")}</select></div>
      <div><label class="label">Shopverantwortliche</label>
        <div class="check-liste">${verantwortliche().map(k => `<label><input type="checkbox" value="${k.id}"> ${esc(k.name)} <span class="muted small num">· ${esc(k.nummer)}</span></label>`).join("")
          || `<span class="small muted">Noch keine Shopverantwortlichen – Rolle unter „Mitglieder“ → ☰ → Rolle ändern vergeben.</span>`}</div></div>` : ""}
    </div>
    <div class="modal-actions"><button class="btn" id="shAb">Abbrechen</button><button class="btn btn-primary" id="shOk">${neu ? "Anlegen" : "Speichern"}</button></div>`);
  $("#shAb").onclick = closeModal;
  $("#shOk").onclick = async () => {
    const d = {name:$("#shName").value.trim(), ort:$("#shOrt").value.trim()};
    if (!d.name){ toast("Bitte einen Namen eingeben.","err"); return; }
    if (neu){ d.kopieVon = +$("#shKopie").value || null; d.team = $$("#modal .check-liste input:checked").map(i => +i.value); }
    const r = neu ? await tu("POST", "/api/shops", d) : await tu("PUT", `/api/shops/${x.id}`, d);
    if (r){ closeModal(); await aktualisieren(); }
  };
}
function shopTeamDialog(x){
  const liste = verantwortliche();
  openModal(`<h2>Shopverantwortliche für ${esc(x.name)}</h2>
    <p class="muted" style="margin:6px 0 14px">Sie pflegen hier Sortiment, Bestand und Verkäufe. Administratoren haben ohnehin Zugriff auf alle Shops.</p>
    <div class="check-liste">${liste.map(k => `<label><input type="checkbox" value="${k.id}" ${(x.team || []).includes(k.id) ? "checked" : ""}>
      ${esc(k.name)} <span class="muted small num">· ${esc(k.nummer)}</span></label>`).join("")
      || `<span class="small muted">Noch keine Shopverantwortlichen. Unter „Mitglieder“ → ☰ → „Rolle ändern“ wird jemand zur/zum Shopverantwortlichen.</span>`}</div>
    <div class="modal-actions"><button class="btn" id="stAb">Abbrechen</button><button class="btn btn-primary" id="stOk" ${liste.length ? "" : "disabled"}>Speichern</button></div>`);
  $("#stAb").onclick = closeModal;
  $("#stOk").onclick = async () => {
    const konten = $$("#modal .check-liste input:checked").map(i => +i.value);
    if (await tu("PUT", `/api/shops/${x.id}/team`, {konten})){ closeModal(); aktualisieren(); }
  };
}
function shopAufZu(x){
  const zu = x.aktiv;
  confirmBox(zu ? "Shop schließen?" : "Shop öffnen?",
    zu ? x.name + " verschwindet aus der Shop-Auswahl. Sortiment, Bestand und Verkäufe bleiben erhalten – du kannst ihn jederzeit wieder öffnen."
       : x.name + " erscheint wieder in der Shop-Auswahl.",
    zu ? "Schließen" : "Öffnen", async () => { if (await tu("PUT", `/api/shops/${x.id}`, {aktiv:!zu})) aktualisieren(); }, zu);
}
function shopLoeschen(x){
  confirmBox("Shop löschen?", x.name + " mit Bestand und Lagerbuch wird gelöscht. Das geht nur, solange dort noch nichts verkauft wurde – sonst bitte schließen.",
    "Löschen", async () => { if (await tu("DELETE", `/api/shops/${x.id}`)) aktualisieren(); }, true);
}


/* Konto: Name, Personalnummer, dienstliche Erreichbarkeit und Zugang (PIN/Passwort zurücksetzen) */
function kontoDialogVerwaltung(k){
  const chef = istInhaber();
  const pwReset = chef && istTeam(k);
  const status = [!k.hatPin ? "noch keine PIN" : k.pinMussAendern ? "Einmal-PIN ausgegeben, noch nicht geändert" : "eigene PIN gesetzt",
                  istTeam(k) ? (k.pwMussAendern ? "Einmalpasswort ausgegeben, noch nicht geändert" : "eigenes Passwort gesetzt") : "",
                  k.gesperrt ? "Anmeldung nach Fehlversuchen gesperrt" : ""].filter(Boolean);
  openModal(`<h2>Konto</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(rolleName(k.rolle))} · Guthaben <b class="num">${EUR(k.guthaben)}</b></p>
    <div class="stack">
      <div class="grid-2">
        <div><label class="label" for="kdVorname">Vorname</label><input id="kdVorname" class="field" value="${esc(k.vorname)}" maxlength="60"></div>
        <div><label class="label" for="kdName">Nachname <span class="muted">(Pflicht)</span></label><input id="kdName" class="field" value="${esc(k.nachname)}" maxlength="60"></div>
      </div>
      <div class="grid-2">
        <div><label class="label" for="kdNr">Personalnummer <span class="muted">(Login)</span></label>
          <input id="kdNr" class="field mono" inputmode="numeric" maxlength="8" value="${esc(k.nummer)}"></div>
        <div><label class="label" for="kdTel">Interne Rufnummer <span class="muted">(5 Ziffern)</span></label>
          <input id="kdTel" class="field mono" inputmode="numeric" maxlength="5" value="${esc(k.tel)}" placeholder="optional"></div>
      </div>
      <div><label class="label" for="kdRaum">Dienststelle / Gebäude / Raum</label><input id="kdRaum" class="field" value="${esc(k.raum)}" maxlength="80" placeholder="optional"></div>
      <div class="konto-zugang">
        <div class="label" style="margin-bottom:4px">Zugang</div>
        <p class="small muted" style="margin:0 0 10px">${esc(status.join(" · "))}</p>
        <div class="row">
          <button class="btn btn-sm" id="kdPin">${k.hatPin ? "PIN zurücksetzen" : "PIN vergeben"}</button>
          ${pwReset ? `<button class="btn btn-sm" id="kdPw">Passwort zurücksetzen</button>` : ""}
        </div>
        <p class="small muted" style="margin:8px 0 0">Erzeugt eine Einmal-PIN${pwReset ? " bzw. ein Einmalpasswort" : ""}. Der bisherige Wert gilt sofort nicht mehr, laufende Anmeldungen werden beendet.</p>
      </div>
    </div>
    <div class="modal-actions"><button class="btn" id="kdAb">Abbrechen</button><button class="btn btn-primary" id="kdOk">Speichern</button></div>`);
  nurZiffern($("#kdNr")); telFeld($("#kdTel"));
  $("#kdAb").onclick = closeModal;
  $("#kdPin").onclick = () => zugangZuruecksetzen(k, "pin");
  if ($("#kdPw")) $("#kdPw").onclick = () => zugangZuruecksetzen(k, "passwort");
  $("#kdOk").onclick = async () => {
    const d = {vorname:$("#kdVorname").value.trim(), nachname:$("#kdName").value.trim(), nummer:$("#kdNr").value.trim(),
               tel:$("#kdTel").value.trim(), raum:$("#kdRaum").value.trim()};
    if (!d.nachname){ toast("Bitte den Nachnamen eingeben.","err"); return; }
    if (!/^\d{8}$/.test(d.nummer)){ toast("Die Personalnummer hat genau 8 Ziffern.","err"); return; }
    if (d.tel && d.tel !== k.tel && !/^\d{5}$/.test(d.tel)){ toast("Die interne Rufnummer hat genau 5 Ziffern.","err"); return; }
    if (await tu("PUT", `/api/konten/${k.id}`, d)){ closeModal(); aktualisieren(); }
  };
}

const ROLLEN_INFO = {
  kunde:    ["Mitglied", "Kauft mit seiner PIN ein. Kein Zugang zur Verwaltung."],
  personal: ["Shopverantwortliche(r)", "Zusätzlich Verwaltung mit Passwort: Mitglieder und Guthaben, dazu Sortiment, Bestand und Verkäufe der zugeordneten Shops."],
  inhaber:  ["Administrator", "Alles – alle Shops, Shops anlegen, Rollen vergeben, Einstellungen, Sicherung einlesen und zurücksetzen."]
};
function rolleDialog(k){
  let wahl = k.rolle;
  const hinweis = r => r === k.rolle ? "Aktuelle Rolle."
    : k.rolle === "kunde" ? "Bekommt ein Einmalpasswort für die Verwaltung. PIN und Guthaben bleiben."
    : r === "kunde" ? "Das Passwort wird gelöscht – kein Zugang mehr zur Verwaltung. PIN, Guthaben und Historie bleiben."
    : "Das bisherige Passwort bleibt gültig.";
  openModal(`<h2>Rolle ändern</h2>
    <p class="muted" style="margin:6px 0 14px">${esc(k.name)} · <span class="num">${esc(k.nummer)}</span></p>
    <div class="stack" id="rWahl">${Object.entries(ROLLEN_INFO).map(([r,[t,b]]) => `
      <button type="button" class="rolle-karte ${r === wahl ? "active" : ""}" data-r="${r}">
        <b>${t}</b><span class="small muted">${b}</span></button>`).join("")}</div>
    <p class="small" id="rHinweis" style="margin:12px 0 0;color:var(--orange-text)">${hinweis(wahl)}</p>
    <div class="modal-actions"><button class="btn" id="rAb">Abbrechen</button><button class="btn btn-primary" id="rOk">Übernehmen</button></div>`);
  $$("#rWahl [data-r]").forEach(b => b.onclick = () => {
    wahl = b.dataset.r;
    $$("#rWahl [data-r]").forEach(x => x.classList.toggle("active", x === b));
    $("#rHinweis").textContent = hinweis(wahl);
  });
  $("#rAb").onclick = closeModal;
  $("#rOk").onclick = async () => {
    if (wahl === k.rolle){ closeModal(); return; }
    const r = await tu("PUT", `/api/konten/${k.id}/rolle`, {rolle:wahl});
    if (!r) return;
    closeModal();
    await aktualisieren();
    if (r.einmalPw) zugangDialog(r.konto, {pw:r.einmalPw});
  };
}

function loeschDialog(k){
  openModal(`<h2>Konto löschen?</h2>
    <p class="muted" style="margin:6px 0 12px"><b>${esc(k.name)}</b> (<span class="num">${esc(k.nummer)}</span>) kann sich danach nicht mehr anmelden.
      Bisherige Käufe und Buchungen bleiben für die Abrechnung erhalten. Die Personalnummer wird wieder frei.</p>
    ${k.guthaben ? `<div class="card ${k.guthaben < 0 ? "danger-card" : "warn-card"}" style="margin-bottom:12px"><p class="small" style="margin:0">
      Das Konto steht bei <b class="num">${EUR(k.guthaben)}</b>. ${k.guthaben > 0 ? "Bitte das Restguthaben vorher auszahlen –" : "Bitte die offenen Schulden vorher einfordern –"}
      der Betrag wird beim Löschen ausgebucht und steht im Kassenbuch.</p></div>` : ""}
    <label class="label" for="lwTxt">Zur Bestätigung den Namen eingeben</label>
    <input id="lwTxt" class="field" autocomplete="off" placeholder="${esc(k.name)}">
    <div class="modal-actions"><button class="btn" id="lwAb">Abbrechen</button><button class="btn btn-danger" id="lwOk">Endgültig löschen</button></div>`);
  $("#lwAb").onclick = closeModal;
  $("#lwOk").onclick = async () => {
    if ($("#lwTxt").value.trim().toLowerCase() !== k.name.trim().toLowerCase()){ toast("Der Name stimmt nicht überein.","err"); return; }
    if (await tu("DELETE", `/api/konten/${k.id}`)){ closeModal(); aktualisieren(); }
  };
}

function buchenDialog(k, art){
  const auf = art === "auf";
  openModal(`<h2>${auf ? "Guthaben aufladen" : "Betrag abbuchen"}</h2>
    <p class="muted" style="margin:6px 0 16px">${esc(k.name)} · aktuell <b class="num">${EUR(k.guthaben)}</b></p>
    <label class="label" for="bBetrag">Betrag in €</label>
    <input id="bBetrag" class="field num" type="number" step="0.5" min="0" value="10">
    <div class="row" style="margin-top:8px">
      ${[5,10,20,50].map(v => `<button class="btn btn-sm" data-v="${v}">${v} €</button>`).join("")}
    </div>
    ${auf ? `<p class="small muted" style="margin:12px 0 0">Aufladungen gelten als Bargeld und erhöhen den Kassenstand.</p>`
      : `<label class="row small check-zeile" style="margin-top:14px"><input type="checkbox" id="bBar" checked>
        <span>Bargeld aus der Kasse ausgezahlt <span class="muted">(Kassenstand ${EUR(D.kasse || 0)}) – ohne Haken nur Korrektur am Guthaben</span></span></label>`}
    <label class="label" for="bNotiz" style="margin-top:14px">Notiz</label>
    <input id="bNotiz" class="field" placeholder="${auf ? "optional" : "z. B. Restguthaben ausgezahlt"}" maxlength="200">
    <div class="modal-actions"><button class="btn" id="bAb">Abbrechen</button>
      <button class="btn btn-primary" id="bOk">${auf ? "Aufladen" : "Abbuchen"}</button></div>`);
  $$('#modal [data-v]').forEach(b => b.onclick = () => $("#bBetrag").value = b.dataset.v);
  $("#bAb").onclick = closeModal;
  $("#bOk").onclick = async () => {
    const v = parseFloat($("#bBetrag").value);
    if (!(v > 0)){ toast("Bitte einen Betrag über 0 eingeben.","err"); return; }
    busy($("#bOk"), true);
    const r = await tu("POST", `/api/konten/${k.id}/buchen`, {art, betrag:v, notiz:$("#bNotiz").value, bar: auf || $("#bBar").checked});
    busy($("#bOk"), false);
    if (!r) return;
    closeModal();
    toast(`${k.name}: neues Guthaben ${EUR(r.guthaben)}`);
    aktualisieren();
  };
}

async function verlaufDialog(k){
  const zeilen = await tu("GET", `/api/konten/${k.id}/verlauf`);
  if (!zeilen) return;
  openModal(`<h2>Kontoverlauf</h2><p class="muted" style="margin:6px 0 14px">${esc(k.name)} · ${esc(k.nummer)} · <b class="num">${EUR(k.guthaben)}</b></p>
    ${verlaufTabelle(zeilen)}
    <div class="modal-actions"><button class="btn btn-primary" id="vClose">Schließen</button></div>`);
  $("#vClose").onclick = closeModal;
}

/* Zeigt Einmal-PIN und/oder Einmalpasswort genau einmal an */
function zugangDialog(k, geheim){
  const {pin, pw} = geheim || {};
  const zeilen = [];
  if (pin) zeilen.push(["Einmal-PIN (Einkaufen)", pin]);
  if (pw) zeilen.push(["Einmalpasswort (Verwaltung)", pw]);
  openModal(`<h2>Zugangsdaten</h2>
    <p class="muted" style="margin:6px 0 16px">Einmalig anzeigen, dann aushändigen.</p>
    <div class="card" style="background:var(--surface-2)">
      <div class="sum-row"><span class="muted">Name</span><b>${esc(k.name)}</b></div>
      <div class="sum-row"><span class="muted">Personalnummer</span><b class="num">${esc(k.nummer)}</b></div>
      ${zeilen.map(([l,v]) => `<div class="sum-row"><span class="muted">${l}</span><b class="num">${esc(v)}</b></div>`).join("")}
      <div class="sum-row"><span class="muted">Adresse</span><span>${esc(D.einstellungen.adresse)}</span></div>
    </div>
    <p class="small" style="color:var(--orange-text);margin:14px 0 0">Wird nur dieses eine Mal angezeigt. Beim ersten Anmelden wird ${pin && pw ? "jeweils ein eigener Wert" : pin ? "eine eigene PIN" : "ein eigenes Passwort"} festgelegt.</p>
    <div class="modal-actions"><button class="btn" id="zAb">Schließen</button>
      <button class="btn btn-primary" id="zPrint">Zettel drucken</button></div>`);
  $("#modalScrim").dataset.fest = "1";
  $("#zAb").onclick = closeModal;
  $("#zPrint").onclick = () => zettelDrucken([[k, zeilen]]);
}
/* Zugangszettel im Scheckkartenformat drucken: eintraege = [[konto, [[Beschriftung, Wert], …]], …] */
function zettelDrucken(eintraege){
  $("#printarea").innerHTML = `<div class="karten">${eintraege.map(([k, zeilen]) => `<div class="print-card">
      <div><div style="font-size:9pt;color:#666">${esc(D.einstellungen.shopName)}</div>
      <div style="font-size:13pt;font-weight:600">${esc(k.name)}</div></div>
      <div style="font-size:10pt;line-height:1.6">
        Adresse: ${esc(D.einstellungen.adresse)}<br>
        Personalnummer: <b>${esc(k.nummer)}</b><br>
        ${zeilen.map(([l,v]) => `${esc(l)}: <b>${esc(v)}</b>`).join("<br>")}
      </div>
      <div style="font-size:8pt;color:#666">Bitte beim ersten Anmelden eigene Werte festlegen.</div></div>`).join("")}</div>`;
  window.print();
}

function zugangZuruecksetzen(k, art){
  const pin = art === "pin";
  confirmBox(pin ? "Neue Einmal-PIN?" : "Neues Einmalpasswort?",
    "Für " + k.name + (pin ? " wird eine Einmal-PIN erzeugt. Die bisherige PIN" : " wird ein Einmalpasswort erzeugt. Das bisherige Passwort") +
    " gilt sofort nicht mehr. Der Vorgang steht mit deinem Namen im Protokoll.",
    "Zurücksetzen", async () => {
      const r = await tu("POST", `/api/konten/${k.id}/zuruecksetzen`, {art});
      if (!r) return;
      await aktualisieren();
      zugangDialog(r.konto, {pin:r.einmalPin, pw:r.einmalPw});
    }, true);
}

/* ---------- Neues Mitglied (Knopf neben der Suche) ---------- */
function neuesKontoDialog(){
  const chef = istInhaber();
  openModal(`<h2>Neues Mitglied</h2>
    <div class="stack" style="margin-top:14px">
      <div class="grid-2">
        <div><label class="label" for="nkVorname">Vorname</label><input id="nkVorname" class="field" maxlength="60" autocomplete="off"></div>
        <div><label class="label" for="nkName">Nachname <span class="muted">(Pflicht)</span></label><input id="nkName" class="field" maxlength="60" autocomplete="off"></div>
      </div>
      <div class="grid-2">
        <div><label class="label" for="nkBenutzer">Personalnummer</label><input id="nkBenutzer" class="field mono" inputmode="numeric" maxlength="8" placeholder="8 Ziffern"></div>
        <div><label class="label" for="nkTel">Interne Rufnummer <span class="muted">(5 Ziffern)</span></label><input id="nkTel" class="field mono" inputmode="numeric" maxlength="5" placeholder="optional"></div>
      </div>
      <div class="grid-2">
        <div><label class="label" for="nkStart">Startguthaben (€)</label><input id="nkStart" class="field num" type="number" step="0.01" value="0"></div>
        <div></div>
      </div>
      ${chef ? `<div><label class="label" for="nkRolle">Rolle</label>
        <select id="nkRolle" class="field">${Object.entries(ROLLEN_INFO).map(([r,[t,b]]) => `<option value="${r}">${t} – ${b}</option>`).join("")}</select></div>` : ""}
      <div><label class="label" for="nkRaum">Dienststelle / Gebäude / Raum</label><input id="nkRaum" class="field" maxlength="80" placeholder="optional"></div>
      <p class="muted small" style="margin:0">Die Personalnummer ist der Login. Es wird eine Einmal-PIN erzeugt – bei Shopverantwortlichen und Administratoren zusätzlich ein Einmalpasswort.</p>
    </div>
    <div class="modal-actions"><button class="btn" id="nkAb">Abbrechen</button>
      <button class="btn btn-primary" id="nkSave">Anlegen</button></div>`);
  nurZiffern($("#nkBenutzer")); telFeld($("#nkTel"));
  $("#nkAb").onclick = closeModal;
  $("#nkSave").onclick = async () => {
    const d = {vorname:$("#nkVorname").value.trim(), nachname:$("#nkName").value.trim(), nummer:$("#nkBenutzer").value.trim(), start:parseFloat($("#nkStart").value) || 0,
               rolle:chef ? $("#nkRolle").value : "kunde", tel:$("#nkTel").value.trim(), raum:$("#nkRaum").value.trim()};
    if (!d.nachname){ toast("Bitte den Nachnamen eingeben.","err"); return; }
    if (!/^\d{8}$/.test(d.nummer)){ toast("Bitte die Personalnummer eintragen (genau 8 Ziffern).","err"); return; }
    if (d.tel && !/^\d{5}$/.test(d.tel)){ toast("Die interne Rufnummer hat genau 5 Ziffern.","err"); return; }
    busy($("#nkSave"), true);
    const r = await tu("POST", "/api/konten", d);
    busy($("#nkSave"), false);
    if (!r) return;
    closeModal();
    await aktualisieren();
    zugangDialog(r.konto, {pin:r.einmalPin, pw:r.einmalPw});
  };
}
$("#neuKontoBtn").onclick = neuesKontoDialog;

/* ---------- Mehrere Mitglieder auf einmal (Liste aus Excel einfügen oder CSV-Datei) ---------- */
/* ---------- Mehrere Mitglieder auf einmal: Tabelle zum Ausfüllen (unten kommt immer eine leere Zeile dazu) ---------- */
const MASSEN_SPALTEN = [
  ["nachname", "Nachname", "Pflicht", 60, ""], ["vorname", "Vorname", "", 60, ""],
  ["nummer", "Personalnummer", "8 Ziffern", 8, "numeric"], ["tel", "Rufnummer", "5 Ziffern", 5, "numeric"],
  ["raum", "Dienststelle / Raum", "", 80, ""], ["start", "Start €", "0", 8, "decimal"]
];
const leereZeile = () => Object.fromEntries(MASSEN_SPALTEN.map(([f]) => [f, ""]));
const zeileLeer = d => MASSEN_SPALTEN.every(([f]) => !String(d[f] || "").trim());
/* Text aus Excel (Tab) oder CSV (; oder ,) in Zeilen zerlegen; Kopfzeile wird erkannt und übersprungen */
function massenLesen(text){
  const zeilen = text.replace(/^﻿/, "").split(/\r?\n/).filter(z => z.trim());
  if (!zeilen.length) return [];
  const trenner = zeilen[0].includes("\t") ? "\t" : zeilen[0].includes(";") ? ";" : ",";
  let liste = zeilen.map(z => z.split(trenner).map(x => x.trim().replace(/^"(.*)"$/, "$1").replace(/""/g, '"').trim()));
  if (!/\d{8}/.test(liste[0].join(" ")) && /name|nummer/i.test(liste[0].join(" "))) liste = liste.slice(1);
  return liste;
}
function massenDialog(){
  let zeilen = [leereZeile()];
  let serverFehler = {};                                   // Zeilenindex → Meldung vom Server
  const vorhanden = new Set((D.konten || []).map(k => k.nummer));
  openModal(`<h2>Mehrere Mitglieder anlegen</h2>
    <p class="muted small" style="margin:6px 0 12px">Einfach Zeile für Zeile ausfüllen – unten kommt automatisch eine neue Zeile dazu.
      Pflicht sind nur <b>Nachname</b> und <b>Personalnummer</b>. Aus Excel kannst du ganze Bereiche direkt in die Tabelle einfügen.
      Alle bekommen die Rolle „Mitglied“ und eine Einmal-PIN.</p>
    <div class="massen-tabelle massen-eingabe"><table>
      <thead><tr><th class="nr">#</th>${MASSEN_SPALTEN.map(([f,t,h]) => `<th class="sp-${f}">${t}${h === "Pflicht" ? ' <span class="pflicht">*</span>' : ""}</th>`).join("")}<th></th></tr></thead>
      <tbody id="msBody"></tbody></table></div>
    <div class="massen-fuss">
      <span class="small" id="msStatus"></span>
      <span class="row">
        <button class="btn btn-sm btn-ghost" id="msDatei">CSV-Datei laden …</button>
        <button class="btn btn-sm btn-ghost" id="msVorlage">Vorlage</button>
        <input type="file" id="msFile" accept=".csv,.txt,text/csv,text/plain" hidden>
      </span>
    </div>
    <div class="modal-actions"><button class="btn" id="msAb">Abbrechen</button><button class="btn btn-primary" id="msOk" disabled>Anlegen</button></div>`);
  $("#modal").classList.add("modal-breit", "modal-sehr-breit");
  const zu = () => { $("#modal").classList.remove("modal-breit", "modal-sehr-breit"); closeModal(); };

  // Prüfen: je Zeile {feld: Meldung}
  const pruefen = () => {
    const zaehler = {};
    zeilen.forEach(d => { const n = d.nummer.trim(); if (n) zaehler[n] = (zaehler[n] || 0) + 1; });
    return zeilen.map((d, i) => {
      if (zeileLeer(d)) return null;
      const f = {};
      if (!d.nachname.trim()) f.nachname = "Nachname fehlt";
      const n = d.nummer.trim();
      if (!/^\d{8}$/.test(n)) f.nummer = "Personalnummer braucht 8 Ziffern";
      else if (vorhanden.has(n)) f.nummer = "Personalnummer ist schon vergeben";
      else if (zaehler[n] > 1) f.nummer = "Personalnummer doppelt in der Liste";
      if (d.tel.trim() && !/^\d{5}$/.test(d.tel.trim())) f.tel = "Rufnummer braucht 5 Ziffern";
      if (d.start.trim() && !/^\d+([.,]\d{1,2})?$/.test(d.start.trim().replace("€", ""))) f.start = "Betrag ungültig";
      if (serverFehler[i] && !Object.keys(f).length) f._ = serverFehler[i];
      return f;
    });
  };
  const zeileHtml = (d, i) => `<tr data-r="${i}"><td class="nr num muted">${i + 1}</td>${MASSEN_SPALTEN.map(([f,,h,max,mode]) =>
      `<td class="sp-${f}"><input class="zelle ${mode ? "num" : ""}" data-f="${f}" value="${esc(d[f])}" maxlength="${max}"
        ${mode ? `inputmode="${mode}"` : ""} placeholder="${esc(h === "Pflicht" ? "" : h)}" autocomplete="off" spellcheck="false"></td>`).join("")}
      <td class="aktion"><button class="btn btn-sm btn-ghost weg" data-weg="${i}" title="Zeile entfernen" aria-label="Zeile entfernen" ${zeileLeer(d) && i === zeilen.length - 1 ? "hidden" : ""}>✕</button></td></tr>`;
  const leereAmEnde = () => {                          // genau eine leere Zeile am Ende
    while (zeilen.length > 1 && zeileLeer(zeilen[zeilen.length - 1]) && zeileLeer(zeilen[zeilen.length - 2])) zeilen.pop();
    if (!zeileLeer(zeilen[zeilen.length - 1])) zeilen.push(leereZeile());
  };
  const zeichnen = (fokus) => {
    leereAmEnde();
    $("#msBody").innerHTML = zeilen.map(zeileHtml).join("");
    status();
    if (fokus) { const el = $(`#msBody tr[data-r="${fokus[0]}"] [data-f="${fokus[1]}"]`); if (el) el.focus(); }
  };
  const status = () => {
    const fehler = pruefen();
    let ok = 0, falsch = 0;
    $$("#msBody tr").forEach(tr => {
      const i = +tr.dataset.r, f = fehler[i];
      tr.classList.toggle("zeile-fehler", !!f && Object.keys(f).length > 0);
      tr.querySelectorAll("[data-f]").forEach(inp => {
        const m = f && (f[inp.dataset.f]);
        inp.classList.toggle("falsch", !!m);
        inp.title = m || "";
      });
      if (f) (Object.keys(f).length ? falsch++ : ok++);
    });
    const liste = fehler.map((f, i) => f && Object.keys(f).length ? `Zeile ${i + 1}: ${Object.values(f)[0]}` : "").filter(Boolean);
    $("#msStatus").innerHTML = !ok && !falsch ? `<span class="muted">Noch keine Mitglieder eingetragen.</span>`
      : falsch ? `<span style="color:var(--red-text)">${falsch} Zeile${falsch > 1 ? "n" : ""} mit Fehler</span> <span class="muted">· ${esc(liste[0])}${liste.length > 1 ? " …" : ""}</span>`
      : `<span style="color:var(--green-text)">✓ ${ok} Mitglied${ok > 1 ? "er" : ""} bereit</span>`;
    $("#msOk").disabled = !ok || falsch > 0;
    $("#msOk").textContent = ok ? `${ok} anlegen` : "Anlegen";
  };
  const body = $("#msBody");
  body.addEventListener("input", e => {
    const inp = e.target.closest("[data-f]"); if (!inp) return;
    const tr = inp.closest("tr"), i = +tr.dataset.r;
    let v = inp.value;
    if (inp.dataset.f === "nummer" || inp.dataset.f === "tel"){ v = v.replace(/\D/g, ""); if (v !== inp.value) inp.value = v; }
    zeilen[i][inp.dataset.f] = v;
    delete serverFehler[i];
    if (i === zeilen.length - 1 && !zeileLeer(zeilen[i])){                 // letzte Zeile benutzt → neue leere anhängen
      zeilen.push(leereZeile());
      body.insertAdjacentHTML("beforeend", zeileHtml(zeilen[zeilen.length - 1], zeilen.length - 1));
      tr.querySelector(".weg").hidden = false;
    }
    status();
  });
  body.addEventListener("keydown", e => {               // Enter = nächste Zeile, gleiche Spalte
    const inp = e.target.closest("[data-f]"); if (!inp || e.key !== "Enter") return;
    e.preventDefault();
    const i = +inp.closest("tr").dataset.r + (e.shiftKey ? -1 : 1);
    const el = $(`#msBody tr[data-r="${i}"] [data-f="${inp.dataset.f}"]`); if (el) el.focus();
  });
  body.addEventListener("paste", e => {                 // Bereich aus Excel einfügen
    const inp = e.target.closest("[data-f]"); if (!inp) return;
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (!/[\t\n;]/.test(text.trim())) return;          // einzelner Wert: normal einfügen
    e.preventDefault();
    const start = +inp.closest("tr").dataset.r, spalte = MASSEN_SPALTEN.findIndex(([f]) => f === inp.dataset.f);
    massenLesen(text).forEach((werte, k) => {
      const r = start + k;
      while (zeilen.length <= r) zeilen.push(leereZeile());
      werte.forEach((w, s) => { const sp = MASSEN_SPALTEN[spalte + s]; if (sp) zeilen[r][sp[0]] = sp[4] === "numeric" ? w.replace(/\D/g, "") : w; });
      delete serverFehler[r];
    });
    zeichnen();
  });
  body.addEventListener("click", e => {
    const b = e.target.closest("[data-weg]"); if (!b) return;
    const i = +b.dataset.weg;
    zeilen.splice(i, 1);
    const neu = {}; Object.entries(serverFehler).forEach(([k, v]) => { if (+k < i) neu[k] = v; else if (+k > i) neu[k - 1] = v; });
    serverFehler = neu;
    if (!zeilen.length) zeilen = [leereZeile()];
    zeichnen();
  });
  $("#msDatei").onclick = () => $("#msFile").click();
  $("#msFile").onchange = e => {
    const f = e.target.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = () => {
      const neu = massenLesen(r.result).map(w => Object.fromEntries(MASSEN_SPALTEN.map(([f, , , , mode], s) =>
        [f, mode === "numeric" ? (w[s] || "").replace(/\D/g, "") : (w[s] || "")])));
      zeilen = zeilen.filter(d => !zeileLeer(d)).concat(neu);
      serverFehler = {};
      zeichnen();
    };
    r.readAsText(f, "utf-8");
    e.target.value = "";
  };
  $("#msVorlage").onclick = () => dateiSpeichern("mitglieder-vorlage.csv",
    "Nachname;Vorname;Personalnummer;Interne Rufnummer;Dienststelle/Raum;Startguthaben\nMuster;Max;20001234;41234;Geb. 12, Raum 104;10\n");
  $("#msAb").onclick = () => {
    if (zeilen.some(d => !zeileLeer(d))) confirmBox("Abbrechen?", "Die eingetragenen Zeilen gehen verloren.", "Verwerfen", zu, true);
    else zu();
  };
  $("#msOk").onclick = async () => {
    const index = [];                                   // Zeile in der Anfrage → Zeile in der Tabelle
    const daten = [];
    zeilen.forEach((d, i) => { if (!zeileLeer(d)){ index.push(i);
      daten.push({nachname:d.nachname.trim(), vorname:d.vorname.trim(), nummer:d.nummer.trim(), tel:d.tel.trim(),
                  raum:d.raum.trim(), start:(d.start.trim().replace("€", "") || "0")}); } });
    busy($("#msOk"), true);
    let r;
    try{ r = await api("POST", "/api/konten/massen", {konten: daten}); }
    catch(e){
      busy($("#msOk"), false);
      toast(e.message, "err");
      if (e.zeilen){ serverFehler = {}; e.zeilen.forEach(z => serverFehler[index[z.zeile - 1]] = z.fehler); status(); }
      return;
    }
    zu();
    await aktualisieren();
    massenErgebnis(r.angelegt);
  };
  zeichnen();
  setTimeout(() => { const el = $('#msBody tr[data-r="0"] [data-f="nachname"]'); if (el) el.focus(); }, 80);   // nach openModal-Fokus
}
function massenErgebnis(angelegt){
  openModal(`<h2>${angelegt.length} Mitglieder angelegt</h2>
    <p class="small" style="color:var(--orange-text);margin:6px 0 12px">Die Einmal-PINs werden nur jetzt angezeigt. Bitte Zettel drucken oder die Liste speichern, bevor du dieses Fenster schließt.</p>
    <div class="massen-tabelle"><table><thead><tr><th>Name</th><th>Personalnr.</th><th>Einmal-PIN</th></tr></thead><tbody>
      ${angelegt.map(a => `<tr><td>${esc(a.konto.name)}</td><td class="num">${esc(a.konto.nummer)}</td><td class="num"><b>${esc(a.einmalPin)}</b></td></tr>`).join("")}
    </tbody></table></div>
    <div class="modal-actions">
      <button class="btn" id="meListe">Liste speichern (CSV)</button>
      <button class="btn" id="meDruck">Zettel drucken</button>
      <button class="btn btn-primary" id="meZu">Fertig</button></div>`);
  $("#modalScrim").dataset.fest = "1";
  $("#meDruck").onclick = () => zettelDrucken(angelegt.map(a => [a.konto, [["Einmal-PIN (Einkaufen)", a.einmalPin]]]));
  $("#meListe").onclick = () => dateiSpeichern(`einmal-pins-${isoTag(new Date())}.csv`,
    "Nachname;Vorname;Personalnummer;Einmal-PIN\n" + angelegt.map(a => [a.konto.nachname, a.konto.vorname, a.konto.nummer, a.einmalPin]
      .map(v => `"${String(v).replace(/"/g, '""')}"`).join(";")).join("\n") + "\n");
  $("#meZu").onclick = () => {                     // zweimal klicken, damit die PINs nicht aus Versehen weg sind
    if ($("#meZu").dataset.sicher){ closeModal(); return; }
    $("#meZu").dataset.sicher = "1";
    $("#meZu").textContent = "Wirklich schließen? PINs sind dann weg";
    $("#meZu").classList.replace("btn-primary", "btn-danger");
  };
}
function dateiSpeichern(name, text){
  const url = URL.createObjectURL(new Blob(["\uFEFF" + text], {type:"text/csv;charset=utf-8"}));
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
$("#massenBtn").onclick = massenDialog;


/* =========================================================
   Kasse (Bargeld): Kassenstand, Kassenbuch, Ausgaben/Einlagen, Kassenprüfung
   ========================================================= */
let kFilter = "alle";
const K_FILTER = [["alle","Alle"],["ein","Einnahmen"],["aus","Ausgaben"],["Aufladung","Aufladungen"],["Auszahlung","Auszahlungen"],["pruef","Kassenprüfungen"]];
function renderKasse(){
  const k = KA; if (!k) return;
  const lp = k.letztePruefung;
  $("#kasseKpis").innerHTML = [
    ["Kassenstand", EUR(k.stand), "so viel Bargeld sollte in der Kasse sein", "gross"],
    ["Einnahmen", EUR(k.einnahmen), zrText()],
    ["Ausgaben", EUR(Math.abs(k.ausgaben)), zrText()],
    ["Letzte Kassenprüfung", lp ? dt(lp.zeit).split(",")[0] : "noch keine",
      lp ? `Differenz ${EUR(lp.differenz)} · seitdem ${k.seitPruefung.buchungen} Buchungen` : "Bitte einmal prüfen – setzt den Anfangsbestand"]
  ].map(([l,v,s,g]) => `<div class="kpi ${g ? "kpi-kasse" : ""}"><div class="kpi-label">${l}</div><div class="kpi-val">${v}</div><div class="kpi-sub small muted">${esc(s)}</div></div>`).join("");
  const zaehl = {alle:k.eintraege.length, ein:k.eintraege.filter(e => e.betrag > 0).length, aus:k.eintraege.filter(e => e.betrag < 0).length,
    Aufladung:k.eintraege.filter(e => e.art === "Aufladung").length, Auszahlung:k.eintraege.filter(e => e.art === "Auszahlung").length, pruef:k.pruefungen.length};
  chips("#kFilter", K_FILTER.filter(([f]) => f === "alle" || zaehl[f]).map(([f,l]) => [f, l, zaehl[f]]), kFilter, f => { kFilter = f; renderKasse(); });
  const q = ($("#kSuche").value || "").toLowerCase().trim();
  const buch = kAnsicht === "buch";
  $$("#kAnsicht [data-a]").forEach(b => b.classList.toggle("active", b.dataset.a === kAnsicht));
  $("#kSort").hidden = buch;
  $("#kListe").hidden = buch;
  $("#kBuch").hidden = !buch;
  if (buch){ kassenbuchZeichnen(k, q); return; }
  if (kFilter === "pruef"){
    const liste = k.pruefungen.filter(p => !q || (p.pruefer + " " + p.pruefer2 + " " + p.bemerkung).toLowerCase().includes(q));
    $("#kAnzahl").textContent = liste.length + " Prüfungen";
    $("#kListe").innerHTML = liste.map(p => listeZeile({
      id: p.id, avatar: "✓",
      titel: `Kassenprüfung #${p.id} <span class="muted small">· ${dt(p.zeit)}</span>`,
      zeilen: [`Soll <span class="num">${EUR(p.soll)}</span> · gezählt <span class="num">${EUR(p.ist)}</span>`,
               `Geprüft von ${esc(p.pruefer)}${p.pruefer2 ? " und " + esc(p.pruefer2) : ""}${p.bemerkung ? " · " + esc(p.bemerkung) : ""}`],
      wert: `<span class="pill num ${p.differenz ? (p.differenz < 0 ? "pill-red" : "pill-orange") : "pill-green"}">${p.differenz ? EUR(p.differenz) : "stimmt"}</span>`,
      aktion: "Protokoll", menu: false
    })).join("") || `<div class="empty">Keine Kassenprüfung in diesem Zeitraum.</div>`;
    listeVerdrahten("#kListe", id => protokollDialog(+id), () => {});
    return;
  }
  const passt = e => kFilter === "alle" || (kFilter === "ein" ? e.betrag > 0 : kFilter === "aus" ? e.betrag < 0 : e.art === kFilter);
  const sortierung = {neu: (a,b) => b.zeit - a.zeit || b.id - a.id, alt: (a,b) => a.zeit - b.zeit || a.id - b.id,
    "betrag-": (a,b) => Math.abs(b.betrag) - Math.abs(a.betrag)}[$("#kSort").value];
  const liste = k.eintraege.filter(passt)
    .filter(e => !q || [e.art, e.kategorie, e.zweck, e.beleg, e.von].join(" ").toLowerCase().includes(q)).sort(sortierung);
  const summe = liste.reduce((a,e) => a + e.betrag, 0);
  $("#kAnzahl").textContent = `${liste.length} Buchungen · ${summe >= 0 ? "+" : ""}${EUR(summe)}`;
  $("#kListe").innerHTML = liste.slice(0, 500).map(e => listeZeile({
    id: e.id, avatar: e.betrag >= 0 ? "+" : "−", klasse: "kasse-zeile " + (e.betrag >= 0 ? "ein" : "aus") + (e.storniert ? " storniert" : ""),
    titel: `${esc(e.art)}${e.kategorie && e.kategorie !== e.art ? ` <span class="shop-tag">${esc(e.kategorie)}</span>` : ""}${e.storniert ? ' <span class="shop-tag">storniert</span>' : ""}`,
    zeilen: [e.zweck ? esc(e.zweck) : "",
             `<span class="num">${dt(e.zeit)}</span> · ${esc(e.von)}${e.beleg ? ` · Beleg <span class="num">${esc(e.beleg)}</span>` : ""}`],
    wert: `<div class="kasse-wert"><span class="pill num ${e.betrag >= 0 ? "pill-green" : "pill-red"}">${e.betrag >= 0 ? "+" : ""}${EUR(e.betrag)}</span>
      <span class="small muted num">Stand ${EUR(e.saldo)}</span></div>`,
    aktion: e.pruefungId ? "Protokoll" : "", menu: istInhaber() && ["Ausgabe","Einlage"].includes(e.art) && !e.storniert || !!e.kontoId
  })).join("") + (liste.length > 500 ? `<p class="muted small" style="text-align:center">… weitere im CSV-Export.</p>` : "")
    || `<div class="empty">Keine Buchungen in diesem Zeitraum.</div>`;
  const finde = id => k.eintraege.find(e => e.id === +id);
  listeVerdrahten("#kListe", id => { const e = finde(id); if (e.pruefungId) protokollDialog(e.pruefungId); }, (id, b) => {
    const e = finde(id), m = [];
    if (e.kontoId){ const kt = (D.konten || []).find(x => x.id === e.kontoId); if (kt) m.push(["≡", "Kontoverlauf von " + kt.name, () => verlaufDialog(kt)]); }
    if (istInhaber() && ["Ausgabe","Einlage"].includes(e.art) && !e.storniert)
      m.push(["↺", "Buchung stornieren (Gegenbuchung)", () => confirmBox("Buchung stornieren?",
        `${e.art} über ${EUR(Math.abs(e.betrag))} („${e.zweck}“) wird durch eine Gegenbuchung aufgehoben. Beide Buchungen bleiben im Kassenbuch sichtbar.`,
        "Stornieren", async () => { if (await tu("POST", `/api/kasse/${e.id}/storno`, {})) aktualisieren(); }, true), true]);
    menuOeffnen(b, `${e.art} · ${EUR(e.betrag)}`, m, "k" + id);
  });
}
$("#kSuche").addEventListener("input", renderKasse);
$("#kSort").onchange = renderKasse;
$$("#kAnsicht [data-a]").forEach(b => b.onclick = () => {
  kAnsicht = b.dataset.a; kbSeite = null;
  try { localStorage.setItem("kaffeekasse.kasseAnsicht", kAnsicht); } catch (_) {}
  renderKasse();
});

/* ---------- Kassenbuch als Buch: liniertes Papier, Handschrift, Übertrag je Seite, Prüfstempel ---------- */
let kAnsicht = (() => { try { return localStorage.getItem("kaffeekasse.kasseAnsicht") === "liste" ? "liste" : "buch"; } catch (_) { return "buch"; } })();
let kbSeite = null;            // Index der Doppelseite (Computer) bzw. Seite (Handy); null = neueste
let kbDaten = null;            // erkennt neu geladene Daten → wieder auf die neueste Seite
const KB_HANDY = matchMedia("(max-width: 900px)");
KB_HANDY.addEventListener("change", () => { kbSeite = null; if (aktTab === "t-kasse" && kAnsicht === "buch") renderKasse(); });
const KB_BELEG = {Aufladung:"A", Auszahlung:"Z", Ausgabe:"B", Einlage:"E", Kassendifferenz:"K", Storno:"S"};
const kbBetrag = c => Math.abs(c).toLocaleString("de-DE", {minimumFractionDigits:2, maximumFractionDigits:2});
const kbTag = ts => new Date(ts).toLocaleDateString("de-DE", {day:"2-digit", month:"2-digit"}).replace(/\.?$/, ".");
const kbKurzname = n => { const t = String(n || "").trim().split(/\s+/); return t.length > 1 ? `${t[0][0]}. ${t.slice(1).join(" ")}` : t.join(""); };
function kbText(e){
  if (e.art === "Aufladung" || e.art === "Auszahlung"){
    const [name, ...rest] = String(e.zweck || "").split(" · ");
    return [`${e.art} ${kbKurzname(name)}`.trim(), ...rest].join(" · ");
  }
  if (e.art === "Kassendifferenz") return "Kassendifferenz lt. Prüfung";
  if (e.art === "Ausgabe" || e.art === "Einlage")
    return e.kategorie && e.kategorie !== "Sonstiges" && !(e.zweck || "").toLowerCase().includes(e.kategorie.toLowerCase())
      ? `${e.kategorie} – ${e.zweck}` : (e.zweck || e.art);
  return e.zweck || e.art;
}
function kbMonatWechseln(schritt){
  const [v] = zeitraum();
  const ziel = new Date(v.getFullYear(), v.getMonth() + schritt, 1), heute = new Date();
  if (ziel > heute) return;
  if (ziel.getFullYear() === heute.getFullYear() && ziel.getMonth() === heute.getMonth()) ZR = {art:"monat", von:null, bis:null};
  else ZR = {art:"frei", von:isoTag(ziel), bis:isoTag(new Date(ziel.getFullYear(), ziel.getMonth() + 1, 0))};
  kbSeite = null; zeitraumZeichnen(); ladeTab();
}
function kassenbuchZeichnen(k, q){
  const handy = KB_HANDY.matches;
  const passt = e => kFilter === "alle" || (kFilter === "ein" ? e.betrag > 0 : kFilter === "aus" ? e.betrag < 0
    : kFilter === "pruef" ? !!e.pruefungId : e.art === kFilter);
  const posten = k.eintraege.filter(passt)
    .filter(e => !q || [e.art, e.kategorie, e.zweck, e.beleg, e.von].join(" ").toLowerCase().includes(q))
    .map(e => ({typ:"b", zeit:e.zeit, ord:e.id, e}));
  if (["alle","pruef"].includes(kFilter) && !q) k.pruefungen.forEach(p => posten.push({typ:"p", zeit:p.zeit, ord:Infinity, p}));
  posten.sort((a,b) => a.zeit - b.zeit || a.ord - b.ord);
  // Seiten füllen: Computer 15 Zeilen je Seite, Handy 9 zweizeilige Einträge; ein Stempel braucht 3 bzw. 2 Plätze
  const PLAETZE = handy ? 9 : 15, STEMPEL = handy ? 2 : 3;
  const seiten = [];
  let seite = null, stand = k.anfang;
  const neueSeite = () => { seite = {posten:[], frei:PLAETZE - 1, uebertrag:stand, ein:0, aus:0, nr:seiten.length + 1}; seiten.push(seite); };
  neueSeite();
  for (const x of posten){
    const bedarf = x.typ === "p" ? STEMPEL : 1;
    if (bedarf > seite.frei) neueSeite();
    seite.posten.push(x); seite.frei -= bedarf;
    if (x.typ === "b"){
      if (x.e.betrag > 0) seite.ein += x.e.betrag; else seite.aus += -x.e.betrag;
      stand = x.e.saldo;
    }
    seite.ende = stand;
  }
  seiten.forEach(s => { if (s.ende === undefined) s.ende = s.uebertrag; });
  const [zv, zb] = zeitraum();
  const ende = new Date(zb); ende.setDate(ende.getDate() - 1);
  const einMonat = zv.getDate() === 1 && zv.getMonth() === ende.getMonth() && zv.getFullYear() === ende.getFullYear();
  const monatTxt = d => d.toLocaleDateString("de-DE", {month:"long", year:"numeric"});
  const kopfTxt = einMonat ? monatTxt(zv) : zrText();
  const vormonat = new Date(zv.getFullYear(), zv.getMonth() - 1, 1).toLocaleDateString("de-DE", {month:"long"});
  const proAnsicht = handy ? 1 : 2, ansichten = Math.max(1, Math.ceil(seiten.length / proAnsicht));
  const schluessel = `${zv.getTime()}|${k.eintraege.length}|${k.stand}|${kFilter}|${q}|${handy}`;
  if (kbDaten !== schluessel){ kbDaten = schluessel; kbSeite = null; }
  if (kbSeite === null || kbSeite >= ansichten) kbSeite = ansichten - 1;
  const zeigen = seiten.slice(kbSeite * proAnsicht, kbSeite * proAnsicht + proAnsicht);
  const lp = k.letztePruefung;
  const stempel = p => `<button type="button" class="kb-stempel" data-pruef="${p.id}" aria-label="Protokoll der Kassenprüfung #${p.id} öffnen">
      <span class="kb-st-titel">Kassenprüfung</span>
      <span class="kb-st-datum">${new Date(p.zeit).toLocaleDateString("de-DE")}</span>
      <span class="kb-st-werte"><span>Soll<b>${kbBetrag(p.soll)}</b></span><span>Gezählt<b>${kbBetrag(p.ist)}</b></span>
        <span>Differenz<b>${p.differenz ? (p.differenz < 0 ? "−" : "+") + kbBetrag(p.differenz) : "stimmt"}</b></span></span>
      <span class="kb-st-sign"><span><i>${esc(p.pruefer)}</i>Kassenführer</span><span><i>${esc(p.pruefer2 || "—")}</i>2. Prüfer</span></span>
    </button>`;
  const zeile = x => {
    if (x.typ === "p") return stempel(x.p);
    const e = x.e, beleg = e.beleg || `${KB_BELEG[e.art] || "X"}-${e.id}`;
    const ein = e.betrag > 0 ? kbBetrag(e.betrag) : "", aus = e.betrag < 0 ? kbBetrag(e.betrag) : "";
    const kl = `kb-z${e.storniert ? " kb-storniert" : ""}${e.pruefungId ? " kb-klick" : ""}`;
    const attr = e.pruefungId ? ` data-pruef="${e.pruefungId}" role="button" tabindex="0"` : "";
    const titel = esc(`${kbText(e)} · gebucht von ${e.von} · ${dt(e.zeit)}`);
    return handy
      ? `<div class="${kl}"${attr} title="${titel}"><div class="kb-z1"><span class="kb-txt"><span class="kb-dat">${kbTag(e.zeit)}</span> ${esc(kbText(e))}</span>
          <span class="kb-betrag ${e.betrag < 0 ? "kb-rot" : ""}">${e.betrag < 0 ? "−" : "+"}${kbBetrag(e.betrag)}</span></div>
          <div class="kb-z2"><span>Beleg ${esc(beleg)}</span><span>Bestand <b>${kbBetrag(e.saldo)}</b></span></div></div>`
      : `<div class="${kl} kb-raster"${attr} title="${titel}"><span>${kbTag(e.zeit)}</span><span class="kb-beleg">${esc(beleg)}</span>
          <span class="kb-txt">${esc(kbText(e))}</span><span class="kb-r">${ein}</span><span class="kb-r kb-rot">${aus}</span><span class="kb-r kb-fett">${kbBetrag(e.saldo)}</span></div>`;
  };
  const uebertrag = s => {
    const txt = s.nr === 1 ? (einMonat ? `Übertrag aus ${vormonat}` : "Übertrag") : `Übertrag von S. ${s.nr - 1}`;
    return handy
      ? `<div class="kb-z kb-ueb"><div class="kb-z1"><span class="kb-txt">${txt}</span><span></span></div><div class="kb-z2"><span></span><span>Bestand <b>${kbBetrag(s.uebertrag)}</b></span></div></div>`
      : `<div class="kb-z kb-raster kb-ueb"><span></span><span></span><span class="kb-txt">${txt}</span><span></span><span></span><span class="kb-r kb-fett">${kbBetrag(s.uebertrag)}</span></div>`;
  };
  const leer = n => Array.from({length:Math.max(0, n)}, () => `<div class="kb-z kb-leer" aria-hidden="true"></div>`).join("");
  const kopfSpalten = `<div class="kb-kopfzeile kb-raster"><span>Datum</span><span>Beleg</span><span>Buchungstext</span><span class="kb-r">Einnahme</span><span class="kb-r">Ausgabe</span><span class="kb-r">Bestand</span></div>`;
  const blatt = (s, links) => `<section class="kb-seite ${links ? "kb-links" : "kb-rechts"}" aria-label="Seite ${s.nr}">
      ${!links ? `<div class="kb-band" aria-hidden="true"></div>` : ""}
      <div class="kb-kopf">
        ${links || handy ? `<div><div class="kb-titel">Kassenbuch</div><div class="kb-unter">${esc(D.einstellungen.shopName)} · Bargeld</div></div>`
          : `<div class="kb-titel kb-titel-klein">Fortsetzung</div>`}
        <div class="kb-monat">${esc(kopfTxt)}</div>
      </div>
      ${handy ? `<div class="kb-kurz"><div><span>Kassenstand</span><b>${EUR(k.stand)}</b></div>
          <div><span>Letzte Prüfung</span><b>${lp ? kbTag(lp.zeit) + (lp.differenz ? " · " + (lp.differenz < 0 ? "−" : "+") + kbBetrag(lp.differenz) : " · stimmt") : "noch keine"}</b></div></div>
        <div class="kb-kopfzeile kb-kopf-handy"><span>Datum · Buchungstext</span><span>Betrag · Bestand</span></div>` : kopfSpalten}
      <div class="kb-linien">${uebertrag(s)}${s.posten.map(zeile).join("")}${leer(s.frei)}</div>
      <div class="kb-fuss ${handy ? "" : "kb-raster"}">
        ${handy ? `<span class="kb-kursiv">Seite ${s.nr} · <span class="kb-blau">+${kbBetrag(s.ein)}</span> · <span class="kb-rot">−${kbBetrag(s.aus)}</span></span>
            <span class="kb-hand kb-fett kb-doppelt">${kbBetrag(s.ende)}</span>`
          : `<span class="kb-kursiv">Summe Seite / ${s.nr === seiten.length ? "Bestand" : "Übertrag"}</span>
            <span class="kb-r kb-hand kb-blau">${kbBetrag(s.ein)}</span><span class="kb-r kb-hand kb-rot">${kbBetrag(s.aus)}</span>
            <span class="kb-r kb-hand kb-fett kb-doppelt">${kbBetrag(s.ende)}</span>`}
      </div>
      ${handy ? "" : `<div class="kb-seitennr">— ${s.nr} —</div>`}
    </section>`;
  const leereSeite = `<section class="kb-seite kb-rechts kb-unbeschrieben" aria-hidden="true"><div class="kb-band"></div></section>`;
  const heute = new Date();
  const naechsterMonatMoeglich = new Date(zv.getFullYear(), zv.getMonth() + 1, 1) <= heute;
  const mName = s => new Date(zv.getFullYear(), zv.getMonth() + s, 1).toLocaleDateString("de-DE", {month:"long"});
  $("#kBuch").innerHTML = `<div class="kb-tisch">
      <div class="kb-buch ${handy ? "kb-handy" : ""}">
        ${zeigen.map((s, i) => blatt(s, !handy && i === 0)).join("")}${!handy && zeigen.length === 1 ? leereSeite : ""}
      </div>
      <nav class="kb-nav" aria-label="Blättern">
        <button type="button" class="kb-knopf" data-kb="monat-1">‹ ${esc(mName(-1))}</button>
        <button type="button" class="kb-knopf" data-kb="seite-1" ${kbSeite <= 0 ? "disabled" : ""} aria-label="Vorige Seite">‹ S.</button>
        <span class="kb-stand">Kassenstand <b>${EUR(k.stand)}</b>${lp ? ` · letzte Prüfung ${kbTag(lp.zeit)}` : ""}<br>
          <span class="small">Seite ${zeigen.map(s => s.nr).join("–")} von ${seiten.length}</span></span>
        <button type="button" class="kb-knopf" data-kb="seite+1" ${kbSeite >= ansichten - 1 ? "disabled" : ""} aria-label="Nächste Seite">S. ›</button>
        <button type="button" class="kb-knopf" data-kb="monat+1" ${naechsterMonatMoeglich ? "" : "disabled"}>${esc(mName(1))} ›</button>
      </nav>
      <p class="kb-hinweis small muted">Stornieren und Kontoverlauf: in der Ansicht „Liste“. Stempel oder Differenzzeile antippen öffnet das Prüfprotokoll.</p>
    </div>`;
  $$("#kBuch [data-kb]").forEach(b => b.onclick = () => {
    const a = b.dataset.kb;
    if (a === "monat-1") kbMonatWechseln(-1);
    else if (a === "monat+1") kbMonatWechseln(1);
    else { kbSeite += a === "seite+1" ? 1 : -1; kassenbuchZeichnen(k, q); }
  });
  $$("#kBuch [data-pruef]").forEach(el => {
    const auf = () => protokollDialog(+el.dataset.pruef);
    el.onclick = auf;
    if (el.getAttribute("role") === "button") el.onkeydown = ev => { if (ev.key === "Enter" || ev.key === " "){ ev.preventDefault(); auf(); } };
  });
}
$("#kAusgabeBtn").onclick = () => kasseBuchenDialog("ausgabe");
$("#kEinlageBtn").onclick = () => kasseBuchenDialog("einlage");
$("#kPruefungBtn").onclick = () => pruefungDialog();

function kasseBuchenDialog(art){
  const aus = art === "ausgabe";
  const kats = (KA && KA.kategorien) || ["Wareneinkauf","Versand","Event","Sonstiges"];
  const vorschlag = aus ? ["Versand","Event","Wareneinkauf","Sonstiges"] : ["Spende","Pfand","Anfangsbestand","Sonstiges"];
  const stand = KA ? KA.stand : (D.kasse || 0);
  openModal(`<h2>${aus ? "Geld ausbuchen" : "Einlage buchen"}</h2>
    <p class="muted" style="margin:6px 0 14px">${aus ? "Bargeld, das aus der Kasse genommen wird – z. B. für Versand, ein Event oder den Wareneinkauf." : "Bargeld, das in die Kasse kommt, ohne dass es ein Guthaben ist – z. B. Spende, Pfand oder Anfangsbestand."}
      Kassenstand jetzt: <b class="num">${EUR(stand)}</b></p>
    <div class="stack">
      <div><label class="label">Kategorie</label>
        <div class="seg kat-seg" id="kbKat">${vorschlag.map((k_, i) => `<button type="button" data-k="${esc(k_)}" class="${i === 0 ? "active" : ""}">${esc(k_)}</button>`).join("")}</div></div>
      <div class="grid-2">
        <div><label class="label" for="kbBetrag">Betrag (€)</label><input id="kbBetrag" class="field num" type="number" step="0.01" min="0.01" inputmode="decimal"></div>
        <div><label class="label" for="kbBeleg">Beleg-Nr. <span class="muted">(optional)</span></label><input id="kbBeleg" class="field" maxlength="60" placeholder="z. B. Rechnung 2026-17"></div>
      </div>
      <div><label class="label" for="kbZweck">Wofür? <span class="muted">(Pflicht)</span></label><input id="kbZweck" class="field" maxlength="200" placeholder="${aus ? "z. B. Paketversand Kaffeebohnen, Grillfest" : "z. B. Pfandrückgabe Kisten"}"></div>
      <p class="small" id="kbInfo" style="margin:0"></p>
    </div>
    <div class="modal-actions"><button class="btn" id="kbAb">Abbrechen</button><button class="btn ${aus ? "btn-danger" : "btn-primary"}" id="kbOk">${aus ? "Ausbuchen" : "Einlage buchen"}</button></div>`);
  $$("#kbKat [data-k]").forEach(b => b.onclick = () => $$("#kbKat [data-k]").forEach(x => x.classList.toggle("active", x === b)));
  const info = () => {
    const v = parseFloat($("#kbBetrag").value) || 0;
    $("#kbInfo").innerHTML = v > 0 ? (aus && v > stand + 1e-9 ? `<span style="color:var(--red-text)">So viel ist nicht in der Kasse.</span>`
      : `<span class="muted">Kassenstand danach: <b class="num">${EUR(aus ? stand - v : stand + v)}</b></span>`) : "";
  };
  $("#kbBetrag").addEventListener("input", info);
  setTimeout(() => $("#kbBetrag").focus(), 60);
  $("#kbAb").onclick = closeModal;
  $("#kbOk").onclick = async () => {
    const d = {art, betrag:parseFloat($("#kbBetrag").value), kategorie:($("#kbKat .active") || {}).dataset?.k || "Sonstiges",
               zweck:$("#kbZweck").value.trim(), beleg:$("#kbBeleg").value.trim()};
    if (!(d.betrag > 0)){ toast("Bitte einen Betrag eingeben.","err"); return; }
    if (!d.zweck){ toast("Bitte angeben, wofür das Geld ist.","err"); return; }
    busy($("#kbOk"), true);
    const r = await tu("POST", "/api/kasse", d);
    busy($("#kbOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
}

/* Kassenprüfung mit Zählhilfe (Scheine und Münzen) */
function pruefungDialog(){
  const stand = KA ? KA.stand : (D.kasse || 0);
  const stueck = (KA && KA.stueckelung) || [50000,20000,10000,5000,2000,1000,500,200,100,50,20,10,5,2,1];
  const name = c => c >= 100 ? (c / 100) + " €" : c + " ct";
  const andere = (D.konten || []).filter(x => x.rolle !== "kunde" && x.id !== user.id);
  openModal(`<h2>Kassenprüfung</h2>
    <p class="muted small" style="margin:6px 0 12px">Bargeld zählen und eintragen. Soll laut Kassenbuch: <b class="num">${EUR(stand)}</b>.
      Am besten zu zweit – der zweite Prüfer steht mit im Protokoll.</p>
    <div class="zaehl-raster">${stueck.map(c => `<label class="zaehl"><span class="num">${name(c)}</span>
      <input class="field num zz" data-c="${c}" type="number" min="0" step="1" inputmode="numeric" placeholder="0">
      <span class="small muted num zz-summe" data-s="${c}"></span></label>`).join("")}</div>
    <div class="pruef-summe">
      <div><span class="small muted">Soll</span><b class="num">${EUR(stand)}</b></div>
      <div><span class="small muted">Gezählt</span><b class="num" id="pzIst">0,00 €</b></div>
      <div><span class="small muted">Differenz</span><b class="num" id="pzDiff">–</b></div>
    </div>
    <div class="stack" style="margin-top:12px">
      <div class="grid-2">
        <div><label class="label">Geprüft von</label><input class="field" value="${esc(user.name)}" disabled></div>
        <div><label class="label" for="pzZweiter">Zweiter Prüfer</label><input id="pzZweiter" class="field" list="pzNamen" maxlength="80" placeholder="Name">
          <datalist id="pzNamen">${andere.map(x => `<option value="${esc(x.name)}">`).join("")}</datalist></div>
      </div>
      <div><label class="label" for="pzBem">Bemerkung</label><input id="pzBem" class="field" maxlength="500" placeholder="optional"></div>
      <label class="row small check-zeile" id="pzBuchenBox" hidden><input type="checkbox" id="pzBuchen" checked>
        <span>Differenz als „Kassendifferenz“ buchen – danach stimmt der Kassenstand wieder mit dem gezählten Geld überein</span></label>
    </div>
    <div class="modal-actions"><button class="btn" id="pzAb">Abbrechen</button><button class="btn btn-primary" id="pzOk">Prüfung speichern</button></div>`);
  $("#modal").classList.add("modal-breit");
  let ist = 0;
  const rechnen = () => {
    ist = 0;
    $$("#modal .zz").forEach(i => { const n = parseInt(i.value) || 0, c = +i.dataset.c; ist += n * c;
      $(`#modal [data-s="${c}"]`).textContent = n ? EUR(n * c / 100) : ""; });
    const diff = Math.round(ist - stand * 100) / 100;
    $("#pzIst").textContent = EUR(ist / 100);
    $("#pzDiff").textContent = diff ? (diff > 0 ? "+" : "") + EUR(diff) : "stimmt ✓";
    $("#pzDiff").style.color = diff ? (diff < 0 ? "var(--red-text)" : "var(--orange-text)") : "var(--green-text)";
    $("#pzBuchenBox").hidden = !diff;
  };
  $$("#modal .zz").forEach(i => i.addEventListener("input", rechnen));
  rechnen();
  const zu = () => { $("#modal").classList.remove("modal-breit"); closeModal(); };
  $("#pzAb").onclick = zu;
  $("#pzOk").onclick = async () => {
    const zaehlung = {};
    $$("#modal .zz").forEach(i => { const n = parseInt(i.value) || 0; if (n) zaehlung[i.dataset.c] = n; });
    if (!Object.keys(zaehlung).length && stand !== 0){ toast("Bitte das gezählte Geld eintragen.","err"); return; }
    busy($("#pzOk"), true);
    const r = await tu("POST", "/api/kasse/pruefung", {zaehlung, ist: ist / 100, pruefer2:$("#pzZweiter").value.trim(),
      bemerkung:$("#pzBem").value.trim(), buchen:$("#pzBuchen").checked});
    busy($("#pzOk"), false);
    if (!r) return;
    zu();
    await aktualisieren();
    protokollDialog(r.id);
  };
}

async function protokollDialog(pid){
  const d = await tu("GET", `/api/kasse/pruefung/${pid}`);
  if (!d) return;
  const p = d.pruefung;
  const name = c => +c >= 100 ? (+c / 100) + " €" : c + " ct";
  const zaehl = Object.entries(p.zaehlung).sort((a,b) => b[0] - a[0]);
  const inhalt = `
    <div class="pp-kopf"><div><div class="pp-klein">${esc(D.einstellungen.shopName)}</div><h2>Kassenprüfungsprotokoll #${p.id}</h2></div>
      <div class="pp-klein" style="text-align:right">${dt(p.zeit)}</div></div>
    <table class="pp-tab">
      <tr><td>Soll laut Kassenbuch</td><td class="t-right num">${EUR(p.soll)}</td></tr>
      <tr><td>Gezählter Bestand</td><td class="t-right num">${EUR(p.ist)}</td></tr>
      <tr class="pp-summe"><td>Differenz</td><td class="t-right num">${p.differenz ? (p.differenz > 0 ? "+" : "") + EUR(p.differenz) : "keine"}</td></tr>
    </table>
    ${p.differenz ? `<p class="pp-klein">${p.gebucht ? "Die Differenz wurde als „Kassendifferenz“ gebucht." : "Die Differenz wurde nicht gebucht."}</p>` : ""}
    ${zaehl.length ? `<h3>Zählliste</h3><table class="pp-tab">${zaehl.map(([c,n]) => `<tr><td>${n} × ${name(c)}</td><td class="t-right num">${EUR(c * n / 100)}</td></tr>`).join("")}</table>` : ""}
    <h3>Bewegungen seit ${d.vorige ? "der letzten Prüfung (" + dt(d.vorige.zeit) + ")" : "Beginn des Kassenbuchs"}</h3>
    <table class="pp-tab">${d.buchungen.map(([a,n,s]) => `<tr><td>${esc(a)} (${n})</td><td class="t-right num">${s >= 0 ? "+" : ""}${EUR(s)}</td></tr>`).join("")
      || `<tr><td class="pp-klein">keine</td><td></td></tr>`}</table>
    ${p.bemerkung ? `<h3>Bemerkung</h3><p>${esc(p.bemerkung)}</p>` : ""}
    <div class="pp-unterschriften">
      <div><div class="pp-linie"></div>${esc(p.pruefer)}</div>
      <div><div class="pp-linie"></div>${esc(p.pruefer2 || "Zweiter Prüfer")}</div>
    </div>`;
  openModal(`<div class="protokoll">${inhalt}</div>
    <div class="modal-actions"><button class="btn" id="ppZu">Schließen</button><button class="btn btn-primary" id="ppDruck">Drucken</button></div>`);
  $("#modal").classList.add("modal-breit");
  $("#ppZu").onclick = () => { $("#modal").classList.remove("modal-breit"); closeModal(); };
  $("#ppDruck").onclick = () => { $("#printarea").innerHTML = `<div class="print-protokoll protokoll">${inhalt}</div>`; window.print(); };
}

/* =========================================================
   Einstellungen: Allgemein · Benachrichtigungen · Export · Sicherung & Daten · Protokoll
   ========================================================= */
let einstTeil = "allg";
function renderEinstTeil(){
  if (!istInhaber() && ["allg","melde"].includes(einstTeil)) einstTeil = "export";
  $$("#einstSeg [data-e]").forEach(b => b.classList.toggle("active", b.dataset.e === einstTeil));
  $$("#t-einst .einst").forEach(el => el.hidden = el.dataset.e !== einstTeil);
}
$$("#einstSeg [data-e]").forEach(b => b.onclick = () => { einstTeil = b.dataset.e; renderEinstTeil(); ladeTab(); });

function renderSettings(){
  const e = D.einstellungen;
  $("#eShop").value = e.shopName; $("#eLimit").value = e.limit; $("#eAuto").value = e.autoLogout;
  $("#eAutoT").value = e.autoLogoutTeam; $("#eMin").value = e.minBestand; $("#eAdr").value = e.adresse;
  $("#eWarn").value = e.warnGuthaben ?? 5;
  $("#eDsKontakt").value = e.datenschutzKontakt || "";
  $("#eStil").innerHTML = Object.entries(STILE).map(([k,l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("");
  $("#eStil").value = e.stil || "standard";
  $("#eStilHinweis").textContent = STIL_HINWEIS[$("#eStil").value] || "";
  if (!istInhaber()) return;
  $("#nUrl").value = e.meldeUrl || "";
  meldeFormatSetzen(e.meldeFormat || "ntfy");
}
let meldeFormat = "ntfy";
const FORMAT_HINWEIS = {
  ntfy: "Adresse des Themas, z. B. https://ntfy.sh/mein-geheimer-kanal. Titel und Text kommen als Push-Nachricht.",
  gotify: "Adresse: http://<gotify-server>/message?token=<App-Token>. Den Token gibt es in Gotify unter „Apps“ → „Create Application“ (nicht den Client-Token nehmen).",
  json: "Sendet {titel, text, shop, produkte} als JSON – z. B. an http://homeassistant:8123/api/webhook/<id>."
};
function meldeFormatSetzen(f){
  meldeFormat = f;
  $$("#nFormat [data-f]").forEach(x => x.classList.toggle("active", x.dataset.f === f));
  $("#nFormatHinweis").textContent = FORMAT_HINWEIS[f] || "";
}
$$("#nFormat [data-f]").forEach(b => b.onclick = () => meldeFormatSetzen(b.dataset.f));
$("#nUrl").addEventListener("input", () => {        // Gotify-Adresse erkannt → Format passend wählen
  try{ if (new URL($("#nUrl").value.trim()).pathname.replace(/\/+$/, "").endsWith("/message")) meldeFormatSetzen("gotify"); }catch(e){}
});
// Vorschau: Design sofort zeigen; ohne Speichern gilt beim nächsten Neuladen wieder das gespeicherte
$("#eStil").onchange = () => { stilSetzen($("#eStil").value); $("#eStilHinweis").textContent = STIL_HINWEIS[$("#eStil").value] || ""; };
$("#eSave").onclick = async () => {
  const r = await tu("PUT", "/api/einstellungen", {
    shopName:$("#eShop").value, limit:$("#eLimit").value, autoLogout:+$("#eAuto").value,
    autoLogoutTeam:+$("#eAutoT").value, minBestand:+$("#eMin").value, adresse:$("#eAdr").value,
    stil:$("#eStil").value, warnGuthaben:$("#eWarn").value, datenschutzKontakt:$("#eDsKontakt").value});
  if (!r) return;
  await aktualisieren();
  resetTimer();
};
async function meldeSpeichern(){
  return await tu("PUT", "/api/einstellungen", {meldeUrl:$("#nUrl").value.trim(), meldeFormat});
}
$("#nSave").onclick = async () => { if (await meldeSpeichern()){ $("#nErgebnis").textContent = "Gespeichert."; aktualisieren(); } };
$("#nTest").onclick = async () => {
  if (!await meldeSpeichern()) return;
  $("#nErgebnis").textContent = "Wird gesendet …";
  busy($("#nTest"), true);
  const r = await tu("POST", "/api/benachrichtigung/test", {shop: vShop !== "alle" ? vShop : undefined});
  busy($("#nTest"), false);
  $("#nErgebnis").innerHTML = r ? r.ergebnis.map(esc).join("<br>") : "";
};

/* ---------- Export ---------- */
function renderExport(){
  if (!$("#exportListe")) return;
  const sp = shopParam(), zr = zrQuery();
  const shopTxt = vShop === "alle" ? ((D.meineShops || []).length > 1 ? "alle deine Shops" : shopName((D.meineShops || [])[0])) : shopName(vShop);
  const eintraege = [
    ["Verkäufe", `Jeder verkaufte Artikel mit Mitglied, Preis, Einkaufspreis und Rohertrag · ${shopTxt} · ${zrText()}`, `/api/export/verkaeufe.csv?${zr}${sp}`],
    ["Kassenbuch (Bargeld)", `Einnahmen, Ausgaben, Kassenprüfungen mit laufendem Kassenstand · ${zrText()}`, `/api/export/kasse.csv?${zr}`],
    ["Kontobuchungen der Mitglieder", `Aufladungen, Auszahlungen, Korrekturen, Stornos · alle Shops · ${zrText()}`, `/api/export/kassenbuch.csv?${zr}`],
    ["Lagerbuch", `Wareneingänge, Auffüllen und Inventur-Korrekturen · ${shopTxt} · ${zrText()}`, `/api/export/lagerbuch.csv?${zr}${sp}`],
    ["Bestand", `Aktueller Bestand (Kühlschrank und Lager) mit Warenwert · ${shopTxt}`, `/api/export/bestand.csv?${sp.slice(1)}`],
    ["Kontostände", "Alle Mitglieder mit Guthaben und dienstlicher Erreichbarkeit · Stand jetzt", "/api/export/kontostaende.csv"],
    ["Protokoll", `Alle Änderungen mit Name und Uhrzeit · ${zrText()}`, `/api/export/protokoll.csv?${zr}`]
  ];
  $("#exportListe").innerHTML = eintraege.map(([t,b,u]) => `<div class="list-item">
    <div class="avatar">⇩</div><div class="li-main"><div class="li-titel">${t}</div><div class="small muted li-zeile">${esc(b)}</div></div>
    <div class="li-actions"><a class="btn btn-sm" href="${u}">CSV speichern</a></div></div>`).join("");
}

/* ---------- Sicherung & Datenbank ---------- */
function renderFolder(){
  const d = D.daten;
  if (!d) return;
  $("#folderPath").textContent = d.pfad;
  $("#folderView").innerHTML = d.tabellen.map(([n,c]) => `<div class="folder-file"><span>▦</span><b>${esc(n)}</b>
      <span class="muted small" style="flex:1;text-align:right">${c} Einträge</span></div>`).join("")
    + `<p class="muted small" style="margin:10px 0 0">Größe: ${(d.groesse/1024).toFixed(1)} KB · Letzte Änderung: ${d.gespeichert ? dt(d.gespeichert) : "–"}</p>`;
}
const SICH_ART = {auto:"Automatisch", manuell:"Von Hand", "vor-einlesen":"Vor Einlesen/Zurücksetzen"};
function renderSicherungen(){
  if (!SB) return;
  $("#sichInfo").textContent = SB.behalten > 0
    ? `Jeden Tag um ${SB.uhr} Uhr automatisch auf dem Server (Ordner ${SB.ordner}). Aufbewahrt werden die letzten ${SB.behalten} je Art.`
    : "Automatische Sicherung ist ausgeschaltet (BACKUP_TAGE = 0).";
  $("#sichListe").innerHTML = SB.dateien.map(f => listeZeile({
    id: f.name, avatar: "⛁",
    titel: `${dt(f.zeit)} <span class="shop-tag">${esc(SICH_ART[f.art] || f.art)}</span>`,
    zeilen: [`<span class="num">${esc(f.name)} · ${(f.groesse/1024).toFixed(0)} KB</span>`],
    aktion: "Herunterladen", menu: istInhaber()
  })).join("") || `<div class="empty">Noch keine Sicherung auf dem Server.</div>`;
  listeVerdrahten("#sichListe", name => { location.href = "/api/sicherungen/" + encodeURIComponent(name); },
    (name, b) => menuOeffnen(b, name, [["↺", "Diese Sicherung einlesen", () => einlesenBestaetigen(() => tu("POST", `/api/sicherungen/${encodeURIComponent(name)}/einlesen`, {})), true]], "b" + name));
}
function einlesenBestaetigen(los){
  confirmBox("Sicherung einlesen?", "Alle aktuellen Mitglieder, Shops, Produkte und Buchungen werden durch die Sicherung ersetzt. Der jetzige Stand wird vorher automatisch auf dem Server gesichert. Das Protokoll bleibt erhalten.",
    "Einlesen", async () => { if (await los()){ lokalAbmelden(); toast("Sicherung eingelesen – bitte neu anmelden.", "warn"); } }, true);
}
$("#backupServerBtn").onclick = async () => { if (await tu("POST", "/api/sicherungen", {})) ladeTab(); };
$("#restoreBtn").onclick = () => {
  if (!istInhaber()){ toast("Das darf nur ein Administrator.","err"); return; }
  $("#restoreFile").click();
};
$("#restoreFile").onchange = e => {
  const f = e.target.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    let neu;
    try{ neu = JSON.parse(r.result); if (!["kaffeekasse-sicherung","kiosk-sicherung"].includes(neu.format)) throw 0; }
    catch(err){ toast("Die Datei passt nicht zu dieser App.","err"); return; }
    einlesenBestaetigen(() => tu("POST", "/api/sicherung", neu));
  };
  r.readAsText(f);
  e.target.value = "";
};
$("#resetBtn").onclick = () => {
  if (!istInhaber()){ toast("Das darf nur ein Administrator.","err"); return; }
  openModal(`<h2>Alles zurücksetzen?</h2>
    <p class="muted" style="margin:6px 0 14px">Alle Mitglieder, Shopverantwortlichen, Shops, Verkäufe und Buchungen werden gelöscht. Der jetzige Stand wird vorher auf dem Server gesichert.</p>
    <label class="label" for="rsTxt">Zur Bestätigung <b>LÖSCHEN</b> eingeben</label>
    <input id="rsTxt" class="field" autocomplete="off">
    <div class="modal-actions"><button class="btn" id="rsAb">Abbrechen</button>
      <button class="btn btn-danger" id="rsOk">Zurücksetzen</button></div>`);
  $("#rsAb").onclick = closeModal;
  $("#rsOk").onclick = async () => {
    if (await tu("POST", "/api/zuruecksetzen", {bestaetigung:$("#rsTxt").value.trim()})){
      closeModal(); await aktualisieren(); zeigeTab("t-ueber");
    }
  };
};

/* ---------- Protokoll ---------- */
const LOG_TYPEN = [["alle","Alle"],["konto","Mitglieder & Zugänge",/konto|pin|passwort|rolle|verwaltungsrecht|zugang|anmeld|mitglied|einrichtung|zuständige/i],
  ["geld","Guthaben",/guthaben|aufgeladen|abgebucht|storn/i],["ware","Sortiment & Lager",/produkt|sortiment|wareneingang|inventur/i],
  ["shop","Shops",/shop|verantwortliche zugeordnet/i],["system","System",/einstellung|sicherung|zurückgesetzt/i]];
let logTyp = "alle";
function logTypVon(l){ const t = LOG_TYPEN.slice(1).find(([,,re]) => re.test(l.was)); return t ? t[0] : "system"; }
function renderLog(){
  if (!$("#logListe")) return;
  const q = ($("#logSuche").value || "").toLowerCase().trim();
  chips("#logFilter", LOG_TYPEN.map(([k,l]) => [k, l, k === "alle" ? JR.length : JR.filter(x => logTypVon(x) === k).length]).filter(x => x[0] === "alle" || x[2]),
    logTyp, c => { logTyp = c; renderLog(); });
  const liste = JR.filter(l => logTyp === "alle" || logTypVon(l) === logTyp)
    .filter(l => !q || (l.wer + " " + l.was + " " + l.detail).toLowerCase().includes(q))
    .sort((a,b) => $("#logSort").value === "alt" ? a.zeit - b.zeit : b.zeit - a.zeit);
  $("#logAnzahl").textContent = liste.length + (liste.length === 1 ? " Eintrag" : " Einträge");
  $("#logListe").innerHTML = `<div class="card log-karte">${liste.slice(0, 500).map(l => `<div class="log-zeile">
    <span class="muted num small">${dt(l.zeit)}</span><span><b>${esc(l.was)}</b> <span class="muted">· ${esc(l.wer)}</span>
    ${l.detail ? `<br><span class="muted small">${esc(l.detail)}</span>` : ""}</span></div>`).join("")
    || `<div class="empty">Keine Einträge in diesem Zeitraum.</div>`}</div>`
    + (liste.length > 500 ? `<p class="muted small" style="text-align:center">… ${liste.length - 500} weitere im CSV-Export.</p>` : "");
}
$("#logSuche").addEventListener("input", renderLog);
$("#logSort").onchange = renderLog;

/* =========================================================
   7. Start
   ========================================================= */
$$("[data-tabs]").forEach(g => {
  g.querySelectorAll(".tab").forEach(t => t.onclick = () => zeigeTab(t.dataset.panel));
});

$("#logoutBtn").onclick = () => logout();

let loginMitPasswort = false;
/* Nur Beschriftung und Tastatur – ob PIN oder Passwort, erkennt die Anmeldung selbst */
function loginModus(pw){
  loginMitPasswort = pw;
  const f = $("#lpass");
  $("#lpassLabel").textContent = pw ? "Passwort" : "PIN";
  f.setAttribute("inputmode", pw ? "text" : "numeric");
  f.placeholder = pw ? "Passwort (Verwaltung)" : "4–8 Ziffern";
  f.classList.toggle("mono", !pw);
  $("#lMode").textContent = pw ? "Zurück zur PIN-Anmeldung" : "Verwaltung: mit Passwort anmelden";
}
$("#lMode").onclick = () => { loginModus(!loginMitPasswort); $("#lpass").value = ""; $("#lpass").focus(); };
loginModus(false);
$("#lpass").addEventListener("input", () => {
  // Buchstaben getippt? Dann ist es ein Passwort – Beschriftung passend umstellen
  if (!loginMitPasswort && /\D/.test($("#lpass").value)) loginModus(true);
});

/* ---------- Hinweis: Website als App speichern ---------- */
let installEreignis = null;
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); installEreignis = e; });
const alsAppGestartet = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function appInfo(){
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const android = /Android/.test(ua);
  const teile = [
    ["ios", "iPhone / iPad (Safari)", `<ol><li>Seite in <b>Safari</b> öffnen.</li><li>Unten auf <b>Teilen</b> tippen (Quadrat mit Pfeil nach oben).</li>
      <li><b>„Zum Home-Bildschirm“</b> wählen – ggf. etwas nach unten scrollen.</li><li>Oben rechts auf <b>„Hinzufügen“</b> tippen.</li></ol>`],
    ["android", "Android (Chrome)", `<ol><li>Seite in <b>Chrome</b> öffnen.</li><li>Oben rechts auf <b>⋮</b> tippen.</li>
      <li><b>„App installieren“</b> oder <b>„Zum Startbildschirm hinzufügen“</b> wählen.</li><li>Mit <b>„Installieren“</b> bzw. <b>„Hinzufügen“</b> bestätigen.</li></ol>`],
    ["pc", "Computer (Chrome / Edge)", `<ol><li>In der Adressleiste rechts auf das Symbol <b>„App installieren“</b> klicken
      (Bildschirm mit Pfeil) – oder im Menü <b>⋮ → „Streamen, speichern und teilen“ → „Seite als App installieren“</b>.</li></ol>`]
  ];
  const zuerst = ios ? "ios" : android ? "android" : "pc";
  teile.sort((a,b) => (b[0] === zuerst) - (a[0] === zuerst));
  openModal(`<h2>Als App speichern</h2>
    <p class="muted" style="margin:6px 0 14px">Leg die Kaffeekasse auf den Home-Bildschirm deines Handys. Sie öffnet sich dann wie eine App – ohne Adressleiste und direkt im gewählten Shop${aktShop != null && SHOPS.length > 1 ? ` (<b>${esc(shopName(aktShop))}</b>)` : ""}.</p>
    ${installEreignis ? `<button class="btn btn-primary" id="jetztInstallieren" style="width:100%;margin-bottom:14px">Jetzt installieren</button>` : ""}
    ${teile.map(([k,t,html], i) => `<details class="app-anleitung" ${i === 0 ? "open" : ""}><summary>${t}</summary>${html}</details>`).join("")}
    <p class="muted small" style="margin:12px 0 0">Du bleibst dabei nicht automatisch angemeldet – dafür beim Anmelden „Auf diesem Gerät angemeldet bleiben“ ankreuzen (nur auf dem eigenen Handy).</p>
    <div class="modal-actions"><button class="btn btn-primary" id="appInfoZu">Verstanden</button></div>`);
  $("#appInfoZu").onclick = closeModal;
  if ($("#jetztInstallieren")) $("#jetztInstallieren").onclick = async () => {
    installEreignis.prompt();
    try{ await installEreignis.userChoice; }catch(e){}
    installEreignis = null; closeModal();
  };
}
$("#appInfoBtn").onclick = appInfo;
if (alsAppGestartet()) $("#appInfoBtn").hidden = true;

/* ---------- Ersteinrichtung (nur beim allerersten Start) ---------- */
function zeigeEinrichtung(e){
  $("#suCodeBox").hidden = !e.code;
  if (!$("#suKasse").value) $("#suKasse").value = $("#shopNameLogin").textContent || "Kaffeekasse";
  if (!$("#suShop").value) $("#suShop").value = (SHOPS[0] || {}).name || "Halle 1";
  ["#suNr","#suPin","#suPin2"].forEach(id => nurZiffern($(id)));
  showView("setup");
  setTimeout(() => { if (!document.activeElement || document.activeElement === document.body) $(e.code ? "#suCode" : "#suKasse").focus(); }, 100);
}
$("#suOk").onclick = async () => {
  const d = {shopName:$("#suKasse").value.trim(), ersterShop:$("#suShop").value.trim(),
             vorname:$("#suVorname").value.trim(), nachname:$("#suName").value.trim(), nummer:$("#suNr").value.trim(),
             passwort:$("#suPw").value, pin:$("#suPin").value, code:$("#suCode").value};
  if (!d.shopName){ toast("Bitte einen Namen für die Kaffeekasse eingeben.","err"); return; }
  if (!d.ersterShop){ toast("Bitte einen Namen für den ersten Shop eingeben.","err"); return; }
  if (!d.nachname){ toast("Bitte den Nachnamen eingeben.","err"); return; }
  if (!/^\d{8}$/.test(d.nummer)){ toast("Die Personalnummer hat genau 8 Ziffern.","err"); return; }
  if (d.passwort.length < 10){ toast("Das Passwort braucht mindestens 10 Zeichen.","err"); return; }
  if (d.passwort !== $("#suPw2").value){ toast("Die Passwörter stimmen nicht überein.","err"); return; }
  if (!/^\d{4,8}$/.test(d.pin)){ toast("Die PIN besteht aus 4 bis 8 Ziffern.","err"); return; }
  if (pinZuEinfach(d.pin)){ toast("Diese PIN ist zu leicht zu erraten.","err"); return; }
  if (d.pin !== $("#suPin2").value){ toast("Die PINs stimmen nicht überein.","err"); return; }
  busy($("#suOk"), true);
  const r = await tu("POST", "/api/einrichtung", d);
  busy($("#suOk"), false);
  if (!r) return;
  toast("Kaffeekasse eingerichtet.");
  await starteSitzung(false);
};

/* Verwaltung <-> Einkauf */
function zurVerwaltung(){
  if (!user || user.modus !== "verwaltung") return;
  closeCart();
  $("#adminBtn").classList.add("active");
  showView("admin"); renderAdmin();
}
function zumEinkauf(){
  $("#adminBtn").classList.remove("active");
  showView("shop"); renderShop();
}
$("#adminBtn").onclick = () => $("#view-admin").classList.contains("active") ? zumEinkauf() : zurVerwaltung();
$("#shopBtn").onclick = zumEinkauf;

$("#loginBtn").onclick = login;
nurZiffern($("#luser"));
$("#luser").addEventListener("input", () => { if ($("#luser").value.length === 8) $("#lpass").focus(); });
["#luser","#lpass"].forEach(sel =>
  $(sel).addEventListener("keydown", e => { if (e.key === "Enter") login(); }));

function renderDemoCards(demo){
  if (!demo || !demo.length){ $("#demoCards").innerHTML = ""; return; }
  $("#demoCards").innerHTML = `<p class="muted small" style="margin:14px 0 2px">Demo-Zugänge zum Ausprobieren:</p>` +
    demo.map(([b,pw,m,r]) => `<button class="demo-card" data-b="${esc(b)}" data-p="${esc(pw)}" data-m="${esc(m)}">
      <span><b class="num">${esc(b)}</b><br><span class="muted small">${esc(r)}</span></span>
      <span class="pill">${esc(pw)}</span></button>`).join("");
  $$("#demoCards .demo-card").forEach(b => b.onclick = () => {
    loginModus(b.dataset.m === "passwort");
    $("#luser").value = b.dataset.b;
    $("#lpass").value = b.dataset.p;
    login();
  });
}

themeColor();
/* Installierbar als App ("Zum Home-Bildschirm") – nur über HTTPS oder localhost */
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")){
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
/* =========================================================
   Einkauf: „Wie letztes Mal“ – den letzten Einkauf in diesem Shop mit einem Tipp in den Warenkorb
   ========================================================= */
function wieImmerZeichnen(gesperrt){
  const box = $("#wieImmer");
  const letzte = (D.letzterEinkauf || {})[String(aktShop)] || [];
  const posten = letzte.map(x => ({...x, p: D.produkte.find(p => p.id === x.pid)}))
                       .filter(x => x.p && x.p.bestand[aktShop] !== undefined);
  if (!posten.length || gesperrt || cart.length){ box.innerHTML = ""; return; }
  const da = posten.filter(x => (x.p.bestand[aktShop] || 0) >= x.menge);
  const summe = posten.reduce((a, x) => a + x.p.preis * x.menge, 0);
  const text = posten.map(x => `${x.menge}× ${x.p.name}`).join(", ");
  box.innerHTML = `<button class="wie-immer" id="wieImmerBtn" ${da.length ? "" : "disabled"}>
      <span class="wie-immer-titel">Wie letztes Mal</span>
      <span class="wie-immer-text">${esc(text)}</span>
      <span class="wie-immer-preis num">${EUR(summe)}</span>
    </button>${da.length < posten.length ? `<p class="small muted" style="margin:-6px 0 14px">${da.length ? "Nicht alles ist noch da – es kommt nur in den Warenkorb, was im Kühlschrank ist." : "Leider gerade nicht im Kühlschrank."}</p>` : ""}`;
  const b = $("#wieImmerBtn");
  if (b) b.onclick = () => {
    for (const x of da){
      const pos = cart.find(c => c.pid === x.pid);
      if (pos) pos.menge = Math.min(pos.menge + x.menge, x.p.bestand[aktShop] || 0);
      else cart.push({pid:x.pid, name:x.p.name, kat:x.p.kat, preis:x.p.preis, menge:x.menge});
    }
    renderShop();
    openCart();        // nur noch „Bezahlen“ tippen
  };
}

/* =========================================================
   Mitglieder: Konto stilllegen / reaktivieren
   ========================================================= */
function stilllegenDialog(k){
  const an = !k.stillgelegt;
  openModal(`<h2>${an ? "Konto stilllegen?" : "Konto reaktivieren?"}</h2>
    <p class="muted" style="margin:6px 0 12px"><b>${esc(k.name)}</b> (<span class="num">${esc(k.nummer)}</span>) ·
      Guthaben <b class="num">${EUR(k.guthaben)}</b></p>
    ${an ? `<p class="small" style="margin:0 0 12px">Zum Beispiel bei Versetzung oder längerer Abwesenheit: Das Konto kann sich nicht mehr anmelden,
      bleibt aber mit Personalnummer, PIN, Guthaben und Verlauf erhalten und lässt sich jederzeit wieder aktivieren.
      Es erscheint dann unter „Stillgelegt“.</p>
      ${k.guthaben > 0 ? `<label class="row small check-zeile"><input type="checkbox" id="slBar">
        <span>Restguthaben von <b class="num">${EUR(k.guthaben)}</b> jetzt bar auszahlen <span class="muted">(Kassenstand ${EUR(D.kasse || 0)})</span></span></label>` : ""}
      ${k.guthaben < 0 ? `<div class="card warn-card"><p class="small" style="margin:0">Das Konto hat <b class="num">${EUR(k.guthaben)}</b> Schulden.
        Die bleiben beim Stilllegen stehen – am besten vorher begleichen lassen.</p></div>` : ""}`
      : `<p class="small" style="margin:0 0 12px">Das Konto kann sich danach wieder mit seiner bisherigen PIN anmelden.</p>`}
    <div class="modal-actions"><button class="btn" id="slAb">Abbrechen</button>
      <button class="btn ${an ? "btn-danger" : "btn-primary"}" id="slOk">${an ? "Stilllegen" : "Reaktivieren"}</button></div>`);
  $("#slAb").onclick = closeModal;
  $("#slOk").onclick = async () => {
    busy($("#slOk"), true);
    const r = await tu("PUT", `/api/konten/${k.id}/stilllegen`, {an, auszahlen: !!($("#slBar") && $("#slBar").checked)});
    busy($("#slOk"), false);
    if (r){ closeModal(); aktualisieren(); }
  };
}

/* =========================================================
   Sortiment: Bestellvorschlag für die Shopverantwortlichen
   ========================================================= */
let bvTage = 14;
async function bestellvorschlagDialog(){
  const d = await tu("GET", `/api/bestellvorschlag?tage=${bvTage}&basis=28` + shopParam());
  if (!d) return;
  const mehrere = d.shops.length > 1;
  const tageTxt = n => n === null ? "–" : n > 365 ? "> 1 Jahr" : n + " Tage";
  const zeilen = d.produkte.map(z => {
    const teile = z.shops.filter(x => x.bedarf > 0 || mehrere);
    return `<tr class="bv-produkt"><td><b>${esc(z.name)}</b><div class="small muted">${esc(z.kat)}</div></td>
      <td class="t-right num">${z.shops.reduce((a,x) => a + x.proWoche, 0).toLocaleString("de-DE")}</td>
      <td class="t-right num">${z.shops.reduce((a,x) => a + x.vorrat, 0)}</td>
      <td class="t-right num">${mehrere ? "" : tageTxt(z.shops[0].reichweite)}</td>
      <td class="t-right num bv-bedarf">${z.bedarf}</td>
      <td class="t-right num">${z.ek !== null ? EUR(z.ek * z.bedarf) : "–"}</td></tr>`
      + (mehrere ? teile.map(x => `<tr class="bv-shop"><td class="small muted">↳ ${esc(x.shopName)}</td>
          <td class="t-right num small muted">${x.proWoche.toLocaleString("de-DE")}</td><td class="t-right num small muted">${x.vorrat}</td>
          <td class="t-right num small muted">${tageTxt(x.reichweite)}</td><td class="t-right num small">${x.bedarf || "–"}</td><td></td></tr>`).join("") : "");
  }).join("");
  const kopf = `${esc(D.einstellungen.shopName)} · ${esc(d.shops.map(x => x.name).join(", "))}`;
  const inhalt = `
    <div class="pp-kopf"><div><div class="pp-klein">${kopf}</div><h2>Bestellvorschlag</h2></div>
      <div class="pp-klein" style="text-align:right">${new Date().toLocaleDateString("de-DE")}</div></div>
    <p class="pp-klein">Verbrauch der letzten ${d.basis} Tage hochgerechnet auf ${d.tage} Tage, plus ${d.puffer} Stück Puffer
      (Nachfüll-Grenze), abzüglich Kühlschrank und Lager. Nur Produkte, die verkauft wurden.</p>
    ${d.produkte.length ? `<table class="pp-tab bv-tab"><thead><tr><th>Produkt</th><th class="t-right">je Woche</th><th class="t-right">Vorrat</th>
      <th class="t-right">reicht</th><th class="t-right">bestellen</th><th class="t-right">ca. Kosten</th></tr></thead>
      <tbody>${zeilen}</tbody>
      <tfoot><tr class="pp-summe"><td colspan="4">Summe</td><td class="t-right num">${d.produkte.reduce((a,z) => a + z.bedarf, 0)}</td>
        <td class="t-right num">${EUR(d.kosten)}${d.ohneEk ? "*" : ""}</td></tr></tfoot></table>
      ${d.ohneEk ? `<p class="pp-klein">* ohne ${d.ohneEk} Produkt(e) ohne Einkaufspreis</p>` : ""}`
      : `<p>Nichts zu bestellen – der Vorrat reicht für die nächsten ${d.tage} Tage.</p>`}`;
  openModal(`<div class="row" style="justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px">
      <span class="small muted">Vorrat soll reichen für</span>
      <div class="seg" id="bvTage">${[7,14,21,28].map(t => `<button type="button" data-t="${t}" class="${t === d.tage ? "active" : ""}">${t} Tage</button>`).join("")}</div></div>
    <div class="protokoll">${inhalt}</div>
    <div class="modal-actions"><button class="btn" id="bvZu">Schließen</button>
      ${d.produkte.length ? `<button class="btn" id="bvCsv">Als CSV</button><button class="btn btn-primary" id="bvDruck">Drucken</button>` : ""}</div>`);
  $("#modal").classList.add("modal-breit");
  $$("#bvTage [data-t]").forEach(b => b.onclick = () => { bvTage = +b.dataset.t; bestellvorschlagDialog(); });
  $("#bvZu").onclick = () => { $("#modal").classList.remove("modal-breit"); closeModal(); };
  if ($("#bvDruck")) $("#bvDruck").onclick = () => { $("#printarea").innerHTML = `<div class="print-protokoll protokoll">${inhalt}</div>`; window.print(); };
  if ($("#bvCsv")) $("#bvCsv").onclick = () => {
    const zahl = n => String(n).replace(".", ",");
    const rows = [["Produkt","Kategorie","Shop","Verbrauch je Woche","Kühlschrank","Lager","Reicht (Tage)","Bestellen","Einkaufspreis"]];
    d.produkte.forEach(z => z.shops.filter(x => x.bedarf > 0).forEach(x =>
      rows.push([z.name, z.kat, x.shopName, zahl(x.proWoche), x.kuehlschrank, x.lager, x.reichweite ?? "", x.bedarf, z.ek !== null ? zahl(z.ek.toFixed(2)) : ""])));
    const zelle = v => `"${String(v).replace(/"/g, '""').replace(/^([=+\-@])/, "'$1")}"`;
    const blob = new Blob(["﻿" + rows.map(r => r.map(zelle).join(";")).join("\r\n")], {type:"text/csv;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `bestellvorschlag_${isoTag(new Date())}.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };
}
$("#bestellBtn").onclick = bestellvorschlagDialog;

/* =========================================================
   Übersicht: Monats- und Jahresabschluss zum Ausdrucken
   ========================================================= */
function abschlussDialog(){
  const h = new Date();
  const monate = Array.from({length:24}, (_, i) => new Date(h.getFullYear(), h.getMonth() - i, 1));
  const jahre = Array.from({length:5}, (_, i) => h.getFullYear() - i);
  const mName = d => d.toLocaleDateString("de-DE", {month:"long", year:"numeric"});
  openModal(`<h2>Monats- oder Jahresabschluss</h2>
    <p class="muted" style="margin:6px 0 14px">Kasse, Verkäufe, Lager und Mitgliederkonten für einen Zeitraum – mit Unterschriftsfeldern zum Ausdrucken.
      Shop-Auswahl wie oben rechts (${vShop === "alle" ? "alle Shops" : esc(shopName(vShop))}).</p>
    <div class="settings-grid">
      <div><label class="label" for="abMonat">Monat</label><select id="abMonat" class="field">
        ${monate.map((d, i) => `<option value="m${i}" ${i === 1 ? "selected" : ""}>${mName(d)}</option>`).join("")}</select></div>
      <div><label class="label" for="abJahr">oder ganzes Jahr</label><select id="abJahr" class="field"><option value="">–</option>
        ${jahre.map(j => `<option value="${j}">${j}${j === h.getFullYear() ? " (bis heute)" : ""}</option>`).join("")}</select></div>
    </div>
    <div class="modal-actions"><button class="btn" id="abAb">Abbrechen</button><button class="btn btn-primary" id="abOk">Abschluss anzeigen</button></div>`);
  $("#abJahr").onchange = () => $("#abMonat").disabled = !!$("#abJahr").value;
  $("#abAb").onclick = closeModal;
  $("#abOk").onclick = () => {
    let v, b, titel;
    if ($("#abJahr").value){
      const j = +$("#abJahr").value;
      v = new Date(j, 0, 1); b = j === h.getFullYear() ? new Date(h.getFullYear(), h.getMonth(), h.getDate() + 1) : new Date(j + 1, 0, 1);
      titel = "Jahresabschluss " + j;
    } else {
      const m = monate[+$("#abMonat").value.slice(1)];
      v = m; b = new Date(m.getFullYear(), m.getMonth() + 1, 1);
      if (b > h) b = new Date(h.getFullYear(), h.getMonth(), h.getDate() + 1);
      titel = "Monatsabschluss " + mName(m);
    }
    abschlussZeigen(v, b, titel);
  };
}
async function abschlussZeigen(v, b, titel){
  const d = await tu("GET", `/api/abschluss?von=${v.getTime()}&bis=${b.getTime()}` + shopParam());
  if (!d) return;
  const f = x => new Date(x).toLocaleDateString("de-DE");
  const bisTag = new Date(d.bis - 1);
  const zeile = (l, w, cls = "") => `<tr class="${cls}"><td>${l}</td><td class="t-right num">${w}</td></tr>`;
  const vz = n => (n > 0 ? "+" : "") + EUR(n);
  const ums = d.verkauf.jeShop.reduce((a,x) => ({umsatz:a.umsatz + x.umsatz, storno:a.storno + x.storno, einsatz:a.einsatz + x.wareneinsatz,
                                                roh:a.roh + x.rohertrag, n:a.n + x.einkaeufe, st:a.st + x.stueck}), {umsatz:0, storno:0, einsatz:0, roh:0, n:0, st:0});
  const kb = d.konten.buchungen;
  const inhalt = `
    <div class="pp-kopf"><div><div class="pp-klein">${esc(d.shopName)} · ${d.alleShops ? "alle Shops" : esc(d.shops.join(", "))}</div>
      <h2>${esc(titel)}</h2><div class="pp-klein">Zeitraum ${f(d.von)} – ${f(bisTag)}</div></div>
      <div class="pp-klein" style="text-align:right">erstellt ${new Date(d.erstellt).toLocaleString("de-DE")}<br>von ${esc(d.von_wem)}</div></div>

    <h3>1. Kasse (Bargeld, alle Shops)</h3>
    <table class="pp-tab">
      ${zeile("Kassenstand zu Beginn (" + f(d.von) + ")", EUR(d.kasse.anfang))}
      ${d.kasse.arten.map(a => zeile(esc(a.art) + (a.kategorie && a.kategorie !== a.art ? " · " + esc(a.kategorie) : "") + ` <span class="pp-klein">(${a.anzahl})</span>`, vz(a.summe))).join("")
        || zeile('<span class="pp-klein">keine Buchungen</span>', "")}
      ${zeile("Kassenstand am Ende (" + f(bisTag) + ")", EUR(d.kasse.ende), "pp-summe")}
    </table>
    ${d.kasse.pruefungen.length ? `<p class="pp-klein">Kassenprüfungen im Zeitraum: ${d.kasse.pruefungen.map(p =>
      `${f(p.zeit)} (Soll ${EUR(p.soll)}, gezählt ${EUR(p.ist)}, Differenz ${p.differenz ? vz(p.differenz) : "keine"}; ${esc(p.pruefer)}${p.pruefer2 ? " / " + esc(p.pruefer2) : ""})`).join(" · ")}</p>`
      : `<p class="pp-klein">Keine Kassenprüfung im Zeitraum.</p>`}

    <h3>2. Verkäufe</h3>
    <table class="pp-tab"><thead><tr><th>Shop</th><th class="t-right">Einkäufe</th><th class="t-right">Stück</th><th class="t-right">Umsatz</th>
      <th class="t-right">storniert</th><th class="t-right">Wareneinsatz</th><th class="t-right">Rohertrag</th></tr></thead>
      ${d.verkauf.jeShop.map(x => `<tr><td>${esc(x.shopName)}</td><td class="t-right num">${x.einkaeufe}</td><td class="t-right num">${x.stueck}</td>
        <td class="t-right num">${EUR(x.umsatz)}</td><td class="t-right num">${EUR(x.storno)}</td><td class="t-right num">${EUR(x.wareneinsatz)}</td>
        <td class="t-right num">${EUR(x.rohertrag)}</td></tr>`).join("") || `<tr><td colspan="7" class="pp-klein">keine Verkäufe</td></tr>`}
      ${d.verkauf.jeShop.length > 1 ? `<tr class="pp-summe"><td>Summe</td><td class="t-right num">${ums.n}</td><td class="t-right num">${ums.st}</td>
        <td class="t-right num">${EUR(ums.umsatz)}</td><td class="t-right num">${EUR(ums.storno)}</td><td class="t-right num">${EUR(ums.einsatz)}</td>
        <td class="t-right num">${EUR(ums.roh)}</td></tr>` : ""}
    </table>
    ${d.verkauf.top.length ? `<div class="pp-zwei"><div><h4>Meistverkauft</h4><table class="pp-tab">${d.verkauf.top.map(([n,s,u]) =>
      `<tr><td>${esc(n)}</td><td class="t-right num">${s}×</td><td class="t-right num">${EUR(u)}</td></tr>`).join("")}</table></div>
      <div><h4>Nach Kategorie</h4><table class="pp-tab">${d.verkauf.kategorien.map(([k,u]) => zeile(esc(k), EUR(u))).join("")}</table></div></div>` : ""}

    <h3>3. Lager</h3>
    <table class="pp-tab"><thead><tr><th>Shop</th><th class="t-right">Wareneingang</th><th class="t-right">Wert</th>
      <th class="t-right">Schwund</th><th class="t-right">Wert</th><th class="t-right">Mehrbestand</th><th class="t-right">Wert</th></tr></thead>
      ${d.lager.jeShop.map(x => `<tr><td>${esc(x.shopName)}</td><td class="t-right num">${x.eingang}</td><td class="t-right num">${EUR(x.eingangWert)}</td>
        <td class="t-right num">${x.schwund}</td><td class="t-right num">${EUR(x.schwundWert)}</td><td class="t-right num">${x.mehr}</td>
        <td class="t-right num">${EUR(x.mehrWert)}</td></tr>`).join("") || `<tr><td colspan="7" class="pp-klein">keine Wareneingänge oder Inventuren</td></tr>`}
    </table>
    <p class="pp-klein">Warenwert (Kühlschrank + Lager, zum Einkaufspreis) heute: ${EUR(d.lager.warenwertJetzt)}.</p>

    <h3>4. Mitgliederkonten zum ${f(bisTag)}</h3>
    <table class="pp-tab">
      ${zeile("Aufladungen im Zeitraum", EUR(kb["Aufladung"] || 0))}
      ${kb["Auszahlung"] ? zeile("Auszahlungen im Zeitraum", EUR(kb["Auszahlung"])) : ""}
      ${zeile("Guthaben der Mitglieder", EUR(d.konten.guthaben))}
      ${zeile("Schulden der Mitglieder", EUR(d.konten.schulden))}
      ${zeile("Kassenstand abzüglich Mitglieder-Guthaben (netto)", EUR(d.konten.kasseOhneGuthaben), "pp-summe")}
    </table>
    ${d.konten.mitSchulden.length ? `<h4>Konten mit Schulden</h4><table class="pp-tab">${d.konten.mitSchulden.map(([n,nr,w]) =>
      `<tr><td>${esc(n)} <span class="pp-klein num">${esc(nr)}</span></td><td class="t-right num">${EUR(w)}</td></tr>`).join("")}</table>` : ""}

    <div class="pp-unterschriften">
      <div><div class="pp-linie"></div>Kassenführer</div>
      <div><div class="pp-linie"></div>Kassenprüfer</div>
      <div><div class="pp-linie"></div>Ort, Datum</div>
    </div>`;
  openModal(`<div class="protokoll">${inhalt}</div>
    <div class="modal-actions"><button class="btn" id="asZu">Schließen</button><button class="btn btn-primary" id="asDruck">Drucken</button></div>`);
  $("#modal").classList.add("modal-breit");
  $("#asZu").onclick = () => { $("#modal").classList.remove("modal-breit"); closeModal(); };
  $("#asDruck").onclick = () => { $("#printarea").innerHTML = `<div class="print-protokoll protokoll">${inhalt}</div>`; window.print(); };
}
$("#abschlussBtn").onclick = abschlussDialog;

/* =========================================================
   Datenschutzhinweis (Anmeldeseite, Fußzeile, Konto)
   ========================================================= */
let DS_KONTAKT = "";
function datenschutzDialog(){
  const kontakt = (D.einstellungen && D.einstellungen.datenschutzKontakt) || DS_KONTAKT;
  const name = esc($("#shopNameLogin").textContent || "Kaffeekasse");
  openModal(`<h2>Datenschutz</h2>
    <div class="ds-text">
    <p>Die ${name} wird auf einem eigenen Server betrieben. Daten werden nur gespeichert, um Einkäufe, Guthaben und Bargeld abzurechnen.
      Es gibt keine Werbung, keine Weitergabe und keine Verbindung zu fremden Diensten – auch Schriften und Bilder kommen vom eigenen Server.</p>
    <h3>Was gespeichert wird</h3>
    <ul>
      <li><b>Konto:</b> Personalnummer, Vor- und Nachname, Rolle, Guthaben; freiwillig interne Rufnummer und Dienststelle/Raum.</li>
      <li><b>PIN und Passwort</b> nur verschlüsselt (als Hash) – niemand kann sie im Klartext lesen.</li>
      <li><b>Einkäufe:</b> Zeitpunkt, Shop, Artikel und Betrag. <b>Kontobuchungen:</b> Aufladungen, Auszahlungen, Stornos.</li>
      <li><b>Protokoll:</b> Anmeldungen und Änderungen in der Verwaltung mit Name und Uhrzeit – damit nachvollziehbar ist, wer was gebucht hat.</li>
      <li><b>Anmeldung:</b> Die Sitzung liegt auf dem Server und endet beim Abmelden oder automatisch; „angemeldet bleiben“ höchstens 30 Tage.
        Die IP-Adresse wird nur kurz im Arbeitsspeicher gehalten, um Fehlversuche zu bremsen, und nicht in der Datenbank gespeichert.
        Das Zugriffsprotokoll des Servers kann IP-Adressen technisch bedingt kurzzeitig enthalten.</li>
      <li><b>Im Browser</b> merkt sich das Gerät nur Shop-Wahl, Design und – nur bei „angemeldet bleiben“ – die Anmeldung. Ein Cookie wird ausschließlich für die Anmeldung verwendet.</li>
    </ul>
    <h3>Wer was sieht</h3>
    <ul>
      <li><b>Du</b> siehst dein Guthaben und deinen Kontoverlauf (Konto → Buchungen).</li>
      <li><b>Shopverantwortliche</b> sehen alle Konten mit Guthaben sowie die Verkäufe ihrer Shops.</li>
      <li><b>Administratoren</b> sehen alles, auch das Protokoll und die Sicherungen.</li>
    </ul>
    <h3>Wie lange</h3>
    <p>Solange du Mitglied bist. Wird ein Konto gelöscht, werden Zugangsdaten entfernt und die Personalnummer wird frei;
      Einkäufe und Buchungen bleiben mit Namen für die Abrechnung der Kasse erhalten. Sicherungen werden nach der eingestellten Zahl von Tagen überschrieben.</p>
    <h3>Deine Rechte</h3>
    <p>Du kannst Auskunft über deine Daten verlangen, falsche Angaben berichtigen und dein Konto löschen lassen.</p>
    <h3>Ansprechpartner</h3>
    <p>${kontakt ? esc(kontakt).replace(/\n/g, "<br>") : "Die Shopverantwortlichen deines Shops bzw. der Administrator der Kaffeekasse."}</p>
    </div>
    <div class="modal-actions"><button class="btn btn-primary" id="dsZu">Schließen</button></div>`);
  $("#dsZu").onclick = closeModal;
}
$("#dsLink").onclick = datenschutzDialog;
$("#dsLinkFuss").onclick = datenschutzDialog;

(async function start(){
  try{
    const m = await api("GET", "/api/me");
    $("#shopName").textContent = $("#shopNameLogin").textContent = document.title = m.shopName;
    if (m.stile) STILE = m.stile;
    DS_KONTAKT = m.datenschutzKontakt || "";
    stilSetzen(m.stil);
    renderDemoCards(m.demo);
    shopWaehlen(m.shops);
    if (m.einrichtung) return zeigeEinrichtung(m.einrichtung);
    if (m.user) return await starteSitzung(m.bleiben);
  }catch(e){ toast(e.message, "err"); }
  showView("login");
  setTimeout(() => { if (document.activeElement === document.body) $("#luser").focus(); }, 200);
})();

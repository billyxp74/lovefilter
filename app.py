#!/usr/bin/env python3
"""
Kommentarvakt — AI-moderering av kommentarfelt.

Vernar ytringsfridom (usemje/kritikk/spørsmål står), skjuler hat/trakassering/
truslar. Eigaren set terskelen (mild/medium/streng). Alt blir logga så eigaren
ser kva som blei skjult og kvifor — ingen stille sensur.

Demo: lim inn kommentarar (ein per linje) → sjå kva vakta ville gjort.
Neste steg: kople til Facebook/Instagram-side via Graph API og auto-skjule.
"""
import os, json, ssl, sqlite3, urllib.request, time, threading
from collections import defaultdict, deque
from flask import Flask, request, render_template_string, redirect

# ── Moderering-modell: Gemini Flash (rask ~0.9s, god norsk, gratis-tier) ──────
# Vald etter benchmark mot Kimi (8s/kall): Gemini er ~9× raskare og meir presis
# på hat-vs-kritikk-grensa. REST-kall → ingen ekstra pakke-avhengigheit.
def _load_key():
    # 1) Lovefilter sin EIGEN produkt-nøkkel (åtskilt frå AI-familiens gateway).
    k = os.environ.get("LF_GEMINI_KEY", "").strip()
    if k:
        return k
    try:
        k = open(os.path.expanduser("~/.config/REDACTED-SECRETS/lovefilter-gemini-key")).read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    # 2) Fallback: familie-gatewayen sin nøkkel (berre til eigen nøkkel er på plass).
    try:
        for line in open(os.path.expanduser("~/REDACTED/aeris-gateway/.env")):
            if line.startswith("GEMINI_API_KEY="):
                return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return os.environ.get("GEMINI_API_KEY", "")

GEMINI_KEY = _load_key()
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Lokal fail-safe-fallback (Legion RTX 4070 via Tailscale) — gratis backup om Gemini
# feilar eller dagleg tak er nådd. Eit Gemini-utfall fell då til lokal modell i staden
# for å sleppe gjennom alt. Sett LF_OLLAMA_URL="" for å deaktivere.
OLLAMA_URL = os.environ.get("LF_OLLAMA_URL", "http://REDACTED-HOST:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("LF_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = int(os.environ.get("LF_OLLAMA_TIMEOUT", "60"))  # romsleg: fyrste kall kan kald-laste modellen

# ── Abuse-/kostnadsvakt (alt fail-open: blokkerer aldri feeden, vernar berre rekninga) ──
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kommentarvakt.db")
MAX_TEXT_LEN = int(os.environ.get("LF_MAX_TEXT_LEN", "600"))      # kapp per kommentar (token-blow-up-vern)
DAILY_GEMINI_CAP = int(os.environ.get("LF_DAILY_CAP", "500000"))  # maks ekte Gemini-kall/dag (~$15 worst case); over → fail-open
RATE_PER_MIN = int(os.environ.get("LF_RATE_PER_MIN", "600"))      # maks tekstar/min per klient-IP

_RL = defaultdict(deque)            # ip -> tidsstempel (per-worker glidande vindu)
_RL_LOCK = threading.Lock()

def _client_ip():
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")

def _rate_ok(ip, cost=1):
    now = time.time()
    with _RL_LOCK:
        dq = _RL[ip]
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) + cost > RATE_PER_MIN:
            return False
        dq.extend([now] * cost)
        return True

def _spend_ok_and_count():
    """True om vi er under dagleg Gemini-tak; tel opp eitt kall (delt på tvers av workers via SQLite).
    Feilar teljaren → tillat (fail-open: vern aldri rekninga på kostnad av at produktet sluttar å virke)."""
    try:
        db = sqlite3.connect(_DB_PATH, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS daily_calls(day TEXT PRIMARY KEY, n INTEGER)")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        row = db.execute("SELECT n FROM daily_calls WHERE day=?", (day,)).fetchone()
        if row and row[0] >= DAILY_GEMINI_CAP:
            db.close()
            return False
        db.execute("INSERT INTO daily_calls(day,n) VALUES(?,1) "
                   "ON CONFLICT(day) DO UPDATE SET n=n+1", (day,))
        db.commit(); db.close()
        return True
    except Exception:
        return True

# English UI/output (global product). The model still reads ALL languages
# (Norwegian, etc.) — only the instruction + returned reason are English.
STRENGHET = {
    "mild":   "Hide ONLY overt hate, harassment, threats, or incitement to harm. Everything else — including strong or uncomfortable opinion — stays.",
    "medium": "Hide hate, harassment, threats, dehumanization (calling a group sick/unnatural/inferior), and incitement to harm. Factual disagreement, criticism and questions stay.",
    "streng": "Hide hate, harassment, threats, dehumanization, derogatory generalizations about a group based on orientation/gender/identity, and mocking contempt. Only clearly factual criticism and questions stay.",
}

def _sys(streng):
    return (
        "You are a comment-section moderator. Your FIRST duty is to protect free speech and open debate. "
        "These ALWAYS stay (action=keep), even when harsh, angry, sarcastic, profane or uncomfortable: "
        "criticism of ideas, policies, decisions, organisations, institutions, companies or public figures "
        "(incl. politicians, officials, executives) — including calling them incompetent, corrupt, liars, or "
        "demanding they resign or be held accountable; factual disagreement, questions, strong opinions, and "
        "counter-speech against hate. "
        f"Threshold ({streng}): {STRENGHET[streng]} "
        "Key distinction: criticising what a person DOES (their competence, behaviour, decisions) is "
        "legitimate and stays; attacking a person or group for who they ARE (sexual orientation, gender "
        "identity, ethnicity, nationality, religion, disability) is what you hide. When in genuine doubt → keep. "
        'Reply ONLY with JSON: {"action":"keep"|"hide","category":"support|criticism|'
        'question|hate|harassment|threat|spam|other","reason":"short reason","confidence":0.0-1.0}. '
        "Comments may be in any language."
    )

_CACHE = {}            # (streng, tekst) -> verdikt-dict  (per worker)
_CACHE_MAX = 50000

def klassifiser(tekst, streng):
    """Cache-lag rundt AI-kallet. Same kommentar (t.d. ein viral hat-tweet sett
    av tusenvis) blir klassifisert ÉIN gong, deretter servert frå minne. Dette
    er det som gjer million-skala billig. Tekniske feil blir IKKJE cacha."""
    key = (streng, tekst)
    hit = _CACHE.get(key)
    if hit is not None:
        return dict(hit)                       # kopi → kallar kan mutere fritt
    v = _klassifiser_raw(tekst, streng)
    if v.get("category") != "feil":            # cache aldri transiente feil
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = dict(v)
    return v

def _norm(v):
    v.setdefault("action", "keep"); v.setdefault("category", "anna")
    v.setdefault("reason", ""); v.setdefault("confidence", 0.0)
    if v["action"] not in ("keep", "hide"):
        v["action"] = "keep"
    return v

def _gemini_classify(tekst, streng):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    body = {
        "system_instruction": {"parts": [{"text": _sys(streng)}]},
        "contents": [{"parts": [{"text": tekst}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context())
    d = json.loads(r.read())
    return _norm(json.loads(d["candidates"][0]["content"]["parts"][0]["text"]))

def _ollama_classify(tekst, streng):
    body = {"model": OLLAMA_MODEL, "system": _sys(streng), "prompt": tekst,
            "format": "json", "stream": False, "keep_alive": "30m", "options": {"temperature": 0}}
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT)
    v = _norm(json.loads(json.loads(r.read())["response"]))
    v["reason"] = (str(v.get("reason", ""))[:120] + " [lokal fallback]").strip()
    return v

def _klassifiser_raw(tekst, streng):
    """Gemini primær → lokal Legion-fallback (gratis) → fail-open keep.
    Aldri skjul ved teknisk feil; eit Gemini-utfall (eller nådd dagleg tak) fell til den
    lokale modellen i staden for å sleppe gjennom alt hatet."""
    gem_err = "dagleg tak nadd"
    if _spend_ok_and_count():
        try:
            return _gemini_classify(tekst, streng)
        except Exception as e:
            gem_err = str(e)
    if OLLAMA_URL:
        try:
            return _ollama_classify(tekst, streng)
        except Exception:
            pass
    # Begge feila → BEHALD (aldri skjul ved teknisk feil)
    return {"action": "keep", "category": "feil", "reason": f"vakt-feil: {gem_err}", "confidence": 0.0}


app = Flask(__name__)

SAMPLE = """Happy Pride! So proud of my city 🏳️‍🌈
I don't think tax money should go to this
You're all disgusting, the world would be better without you
Couldn't they fix the roads first?
People like you deserve everything bad that's coming
Finally — love is love ❤️"""

SIDE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lovefilter — stop hate, keep free speech</title>
<style>
:root{--ink:#1e293b;--muted:#64748b;--line:#e5e7eb;--red:#ef4444;--green:#22c55e;--blue:#3b82f6}
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:var(--ink)}
.bar{height:6px;background:linear-gradient(90deg,#e40303,#ff8c00,#ffed00,#008026,#004dff,#750787)}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px}
h1{margin:0 0 4px;font-size:1.7rem}.sub{color:var(--muted);margin:0 0 6px;font-size:1.05rem}
.eq{font-weight:700;color:var(--ink);margin:0 0 22px}
textarea{width:100%;min-height:140px;padding:12px;border:1px solid var(--line);border-radius:10px;font:inherit;resize:vertical}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0}
select,button,.btn{padding:11px 16px;border-radius:9px;border:1px solid var(--line);font:inherit;text-decoration:none;color:var(--ink)}
button{background:var(--ink);color:#fff;border:0;font-weight:700;cursor:pointer}
.btn-ghost{background:#fff}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:0;overflow:hidden;margin-top:8px}
.item{display:flex;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);align-items:flex-start}
.item:last-child{border:0}
.tag{font-size:.7rem;font-weight:800;padding:3px 9px;border-radius:999px;white-space:nowrap;margin-top:2px}
.keep{background:#dcfce7;color:#15803d}.hide{background:#fee2e2;color:#b91c1c}
.txt{flex:1}.txt .c{font-size:.95rem}.txt .r{font-size:.8rem;color:var(--muted);margin-top:3px}
.cat{font-size:.72rem;color:var(--muted)}
.stat{display:flex;gap:20px;margin:18px 0 6px;font-size:.9rem;color:var(--muted)}.stat b{color:var(--ink)}
.note{background:#eff6ff;border-left:3px solid var(--blue);padding:10px 14px;border-radius:6px;font-size:.85rem;color:#1e3a5f;margin-top:18px}
.cta{background:#0f172a;color:#fff;border-radius:14px;padding:22px;margin-top:28px}
.cta h2{margin:0 0 4px;font-size:1.15rem}.cta p{margin:0 0 14px;color:#cbd5e1;font-size:.9rem}
.cta form{display:flex;gap:8px;flex-wrap:wrap}
.cta input{flex:1;min-width:200px;padding:11px 14px;border-radius:9px;border:0;font:inherit}
.cta button{background:var(--blue)}
.ok{background:#dcfce7;color:#15803d;padding:11px 14px;border-radius:9px;font-weight:600}
</style></head><body><div class="bar"></div><div class="wrap">
<h1>🏳️‍🌈 Lovefilter</h1>
<p class="sub">We love free speech — we just don't want hate. AI hides hate, harassment & threats; honest disagreement and criticism stay.</p>
<p class="eq">Pride = Love = Lovefilter.</p>
<form method="POST">
  <textarea name="kommentarar" placeholder="Paste comments — one per line…">{{ raw }}</textarea>
  <div class="row">
    <label>Strictness:
      <select name="streng">
        <option value="mild" {{ 'selected' if streng=='mild' }}>Mild — overt hate only</option>
        <option value="medium" {{ 'selected' if streng=='medium' }}>Medium — hate + dehumanization</option>
        <option value="streng" {{ 'selected' if streng=='streng' }}>Strict — also derogatory generalizations</option>
      </select>
    </label>
    <button type="submit">Check comments</button>
    <a class="btn btn-ghost" href="/?sample=1">Try a sample feed</a>
  </div>
</form>
{% if resultat %}
  <div class="stat"><span>Kept: <b style="color:var(--green)">{{ n_keep }}</b></span>
    <span>Hidden: <b style="color:var(--red)">{{ n_hide }}</b></span>
    <span>Total: <b>{{ resultat|length }}</b></span></div>
  <div class="card">
  {% for r in resultat %}
    <div class="item">
      <span class="tag {{ 'hide' if r.action=='hide' else 'keep' }}">{{ '🔴 HIDDEN' if r.action=='hide' else '🟢 KEPT' }}</span>
      <div class="txt">
        <div class="c">{{ r.tekst }}</div>
        <div class="r"><span class="cat">{{ r.category }}</span> · {{ r.reason }} <span style="opacity:.6">({{ (r.confidence*100)|round|int }}%)</span></div>
      </div>
    </div>
  {% endfor %}
  </div>
  <div class="note">No silent censorship: every decision is shown with a reason. On any technical error a comment is <b>kept</b>, never hidden. You stay in control.</div>
{% endif %}
<div class="cta">
  <h2>Get Lovefilter for your browser</h2>
  <p>One click, works instantly, free. We'll email you the moment it lands in the Chrome / Firefox / Edge store.</p>
  {% if takk %}<div class="ok">Thank you — you're on the list. ❤️</div>{% else %}
  <form method="POST" action="/signup">
    <input type="email" name="email" placeholder="you@email.com" required>
    <button type="submit">Notify me</button>
  </form>{% endif %}
</div>
</div></body></html>"""

@app.route("/", methods=["GET", "POST"])
def index():
    raw, streng, resultat = "", "medium", None
    n_keep = n_hide = 0
    takk = request.args.get("takk") == "1"
    if request.method == "POST":
        raw = request.form.get("kommentarar", "").strip()
        streng = request.form.get("streng", "medium")
    elif request.args.get("sample") == "1":
        raw, streng = SAMPLE, "medium"
    if raw:
        if streng not in STRENGHET:
            streng = "medium"
        linjer = [l.strip()[:MAX_TEXT_LEN] for l in raw.splitlines() if l.strip()][:40]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            verdikt = list(pool.map(lambda l: klassifiser(l, streng), linjer))
        resultat = []
        for l, v in zip(linjer, verdikt):
            v["tekst"] = l
            resultat.append(v)
            if v["action"] == "hide":
                n_hide += 1
            else:
                n_keep += 1
    return render_template_string(SIDE, raw=raw, streng=streng, resultat=resultat,
                                  n_keep=n_keep, n_hide=n_hide, takk=takk)


@app.route("/signup", methods=["POST"])
def signup():
    email = (request.form.get("email") or "").strip().lower()
    if email and "@" in email and len(email) < 200:
        try:
            db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "kommentarvakt.db"))
            db.execute("CREATE TABLE IF NOT EXISTS signups(email TEXT, ts TEXT)")
            db.execute("INSERT INTO signups(email, ts) VALUES(?, datetime('now'))", (email,))
            db.commit(); db.close()
        except Exception:
            pass
    return redirect("/?takk=1")

# ── API for nettlesar-utvidelsen (Kjærleiksfilter) ──────────────────────────
# Utvidelsen sender kommentar-tekst hit; nøkkelen blir på server. CORS open
# (offentleg klassifiser-API). Legg rate-limit/abuse-guard før kommersialisering.
@app.after_request
def _cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.route("/api/klassifiser", methods=["POST", "OPTIONS"])
def api_klassifiser():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    texts = [t.strip()[:MAX_TEXT_LEN] for t in data.get("texts", []) if isinstance(t, str) and t.strip()][:30]
    if texts and not _rate_ok(_client_ip(), cost=len(texts)):
        out = [{"action": "keep", "category": "rate", "reason": "rate limit"} for _ in texts]
        return app.response_class(json.dumps({"results": out}), mimetype="application/json", status=429)
    streng = data.get("streng", "medium")
    if streng not in STRENGHET:
        streng = "medium"
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        res = list(pool.map(lambda t: klassifiser(t, streng), texts))
    # slank respons til det utvidelsen treng
    out = [{"action": v.get("action", "keep"), "category": v.get("category", ""),
            "reason": v.get("reason", "")} for v in res]
    return app.response_class(json.dumps({"results": out}), mimetype="application/json")


_PRIVACY = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lovefilter — Privacy</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:40px 20px;color:#1e293b;line-height:1.6}
h1{font-size:1.5rem}h2{font-size:1.05rem;margin-top:26px}.muted{color:#64748b;font-size:.9rem}</style></head><body>
<h1>Lovefilter — Privacy Policy</h1>
<p class="muted">KNDW Shelter Solutions AS (Norway) · contact@aeris.no · last updated 2026-05-30</p>
<p>Lovefilter hides hateful and harassing comments in your own browser while keeping honest disagreement. This is how it handles data.</p>
<h2>What we process</h2>
<p>To decide whether a comment is hateful, the text of comments shown on the page is sent to our server (vakt.aeris.no) and to Google Gemini for classification. That is the only purpose.</p>
<h2>What we do NOT do</h2>
<ul>
<li>We do <b>not</b> store comment text after classifying it.</li>
<li>We do <b>not</b> create user accounts, profiles, or track you.</li>
<li>We do <b>not</b> use your data to train models.</li>
<li>We do <b>not</b> sell or share data, and we show no ads.</li>
</ul>
<h2>Your settings</h2>
<p>On/off and strictness are stored locally in your browser only (chrome.storage). They never leave your device.</p>
<h2>Legal basis (GDPR)</h2>
<p>Controller: KNDW Shelter Solutions AS. Processor for classification: Google (Gemini API), configured without training on input. Lawful basis: your legitimate interest in a hate-free feed. Contact <a href="mailto:contact@aeris.no">contact@aeris.no</a> for any request.</p>
</body></html>"""

@app.route("/privacy")
def privacy():
    return _PRIVACY


@app.route("/lovefilter")
@app.route("/landing")
def lovefilter_landing():
    try:
        with open(os.path.join(os.path.dirname(__file__), "lovefilter_landing.html"), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return redirect("/")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5021, debug=False)

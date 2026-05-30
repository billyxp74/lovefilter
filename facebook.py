"""
facebook.py — Meta Graph API-integrasjon for Kommentarvakt.

Flyt:
  1. Eigaren koplar Facebook-/Instagram-sida si via Facebook Login (OAuth).
  2. Vi får eit langtidsleva Page Access Token og lagrar det.
  3. scan_page() hentar nye kommentarar på siste innlegg, klassifiserer dei
     (Kommentarvakt-AI), og skjuler hat/trakassering via Graph API (is_hidden).
  4. Alt blir logga — eigaren ser kva som blei skjult og kan overstyre.

Krev ein Facebook-app (App ID + App Secret) — sjå SETUP.md. Permissions:
  pages_show_list, pages_read_engagement, pages_manage_engagement,
  pages_read_user_content  (+ instagram_basic, instagram_manage_comments for IG).

I Development-modus verkar dette for app-admin/testarar (t.d. dine eigne sider)
UTAN App Review. For andre bedrifter krevst App Review + Business Verification.
"""
import json, time, ssl, sqlite3, urllib.parse, urllib.request, os

GRAPH = "https://graph.facebook.com/v21.0"
SCOPES = ("pages_show_list,pages_read_engagement,pages_manage_engagement,"
          "pages_read_user_content,instagram_basic,instagram_manage_comments")
DB = os.path.join(os.path.dirname(__file__), "kommentarvakt.db")
_CTX = ssl.create_default_context()


# ── Konfig (App ID/Secret — fyllast etter at FB-appen er oppretta) ───────────
def _cfg():
    path = os.path.join(os.path.dirname(__file__), "fb_config.env")
    out = {}
    try:
        for line in open(path):
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                out[k] = v
    except FileNotFoundError:
        pass
    return out

def is_configured():
    c = _cfg()
    return bool(c.get("FB_APP_ID") and c.get("FB_APP_SECRET"))


# ── DB (token-lager + moderering-logg) ───────────────────────────────────────
def init_db():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS sider(
        page_id TEXT PRIMARY KEY, namn TEXT, access_token TEXT,
        ig_id TEXT, streng TEXT DEFAULT 'medium', kopla_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS logg(
        id INTEGER PRIMARY KEY AUTOINCREMENT, page_id TEXT, comment_id TEXT,
        tekst TEXT, action TEXT, category TEXT, reason TEXT, ts TEXT)""")
    db.commit(); db.close()


# ── Graph-kall ────────────────────────────────────────────────────────────────
def _get(path, params):
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    return json.loads(urllib.request.urlopen(urllib.request.Request(url), timeout=20, context=_CTX).read())

def _post(path, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=20, context=_CTX).read())


# ── OAuth ─────────────────────────────────────────────────────────────────────
def oauth_url(redirect_uri, state="kv"):
    c = _cfg()
    q = urllib.parse.urlencode({"client_id": c["FB_APP_ID"], "redirect_uri": redirect_uri,
                                "scope": SCOPES, "response_type": "code", "state": state})
    return f"https://www.facebook.com/v21.0/dialog/oauth?{q}"

def exchange_code(code, redirect_uri):
    c = _cfg()
    short = _get("oauth/access_token", {"client_id": c["FB_APP_ID"], "redirect_uri": redirect_uri,
                                        "client_secret": c["FB_APP_SECRET"], "code": code})
    # byt til langtids-token
    long = _get("oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": c["FB_APP_ID"],
                                       "client_secret": c["FB_APP_SECRET"], "fb_exchange_token": short["access_token"]})
    return long["access_token"]

def lagre_sider(user_token):
    """Hent sidene brukaren administrerer + deira page-token, lagre dei."""
    res = _get("me/accounts", {"access_token": user_token,
                               "fields": "id,name,access_token,instagram_business_account"})
    db = sqlite3.connect(DB); n = 0
    for p in res.get("data", []):
        ig = (p.get("instagram_business_account") or {}).get("id")
        db.execute("""INSERT INTO sider(page_id,namn,access_token,ig_id,kopla_at)
                      VALUES(?,?,?,?,datetime('now'))
                      ON CONFLICT(page_id) DO UPDATE SET namn=excluded.namn,
                      access_token=excluded.access_token, ig_id=excluded.ig_id""",
                   (p["id"], p.get("name"), p["access_token"], ig))
        n += 1
    db.commit(); db.close()
    return n


# ── Skanning + moderering ─────────────────────────────────────────────────────
def hent_kommentarar(page_id, token, grense=50):
    """Nye kommentarar (ikkje alt skjult) på siste innlegg."""
    feed = _get(f"{page_id}/posts", {"access_token": token, "limit": 10, "fields": "id"})
    ut = []
    for post in feed.get("data", []):
        try:
            cs = _get(f"{post['id']}/comments", {"access_token": token, "limit": grense,
                                                 "fields": "id,message,from,is_hidden"})
            for c in cs.get("data", []):
                if not c.get("is_hidden") and c.get("message"):
                    ut.append({"id": c["id"], "message": c["message"], "post": post["id"]})
        except Exception:
            continue
    return ut

def skjul_kommentar(comment_id, token):
    return _post(comment_id, {"access_token": token, "is_hidden": "true"})

def scan_page(page_id, klassifiser_fn):
    """Hent → klassifiser → skjul hat → logg. Returnerer oppsummering.
    klassifiser_fn(tekst, streng) -> {'action','category','reason',...} (frå app.py)."""
    db = sqlite3.connect(DB)
    row = db.execute("SELECT namn,access_token,streng FROM sider WHERE page_id=?", (page_id,)).fetchone()
    if not row:
        db.close(); return {"feil": "ukjend side"}
    namn, token, streng = row
    skjult = behaldne = 0
    for c in hent_kommentarar(page_id, token):
        v = klassifiser_fn(c["message"], streng or "medium")
        if v.get("action") == "hide":
            try:
                skjul_kommentar(c["id"], token); skjult += 1
            except Exception as e:
                v["reason"] = f"skjuling feila: {e}"
        else:
            behaldne += 1
        db.execute("""INSERT INTO logg(page_id,comment_id,tekst,action,category,reason,ts)
                      VALUES(?,?,?,?,?,?,datetime('now'))""",
                   (page_id, c["id"], c["message"], v.get("action"), v.get("category"), v.get("reason")))
    db.commit(); db.close()
    return {"side": namn, "skjult": skjult, "behaldne": behaldne}

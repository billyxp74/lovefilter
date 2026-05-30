/* Kjærleiksfilter — content script for X (twitter.com / x.com)
   Les tweets/svar, klassifiser via vakt.aeris.no, blur hat/trakassering.
   Sakleg usemje og kritikk får stå. "Vis likevel" gjer alt reversibelt. */

const API = "https://vakt.aeris.no/api/klassifiser";
const MARK = "data-kf";               // prosessering-markør
let settings = { enabled: true, streng: "medium" };
let hiddenCount = 0;

// ── innstillingar ────────────────────────────────────────────────────────
chrome.storage?.sync.get(["enabled", "streng"], (s) => {
  if (typeof s.enabled === "boolean") settings.enabled = s.enabled;
  if (s.streng) settings.streng = s.streng;
});
chrome.storage?.onChanged.addListener((ch) => {
  if (ch.enabled) settings.enabled = ch.enabled.newValue;
  if (ch.streng) settings.streng = ch.streng.newValue;
});

// ── skjul / vis ──────────────────────────────────────────────────────────
function skjul(el, reason) {
  if (el.getAttribute(MARK) === "hidden") return;
  el.setAttribute(MARK, "hidden");
  const inner = el.querySelector('[data-testid="tweetText"]')?.closest("div") || el;
  inner.style.filter = "blur(6px)";
  inner.style.userSelect = "none";
  const bar = document.createElement("div");
  bar.style.cssText =
    "filter:none;font:13px system-ui;background:#fff5f5;border:1px solid #fecaca;color:#b91c1c;" +
    "border-radius:10px;padding:8px 12px;margin:6px 0;display:flex;gap:10px;align-items:center;justify-content:space-between";
  const txt = document.createElement("span");
  txt.textContent = "🛡️ Hidden by Lovefilter" + (reason ? " — " + reason : "");
  const btn = document.createElement("button");
  btn.textContent = "Show anyway";
  btn.style.cssText =
    "filter:none;background:#b91c1c;color:#fff;border:0;border-radius:7px;padding:6px 12px;font-weight:600;cursor:pointer";
  btn.onclick = (e) => { e.stopPropagation(); inner.style.filter = ""; inner.style.userSelect = ""; bar.remove(); };
  bar.append(txt, btn);
  inner.parentElement?.insertBefore(bar, inner);
  hiddenCount++;
  chrome.storage?.local.set({ hiddenCount });
}

// ── samle + klassifisere ───────────────────────────────────────────────────
async function behandle(batch) {
  if (!batch.length) return;
  try {
    const r = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: batch.map((b) => b.text), streng: settings.streng }),
    });
    const data = await r.json();
    (data.results || []).forEach((v, i) => {
      if (v.action === "hide") skjul(batch[i].el, v.reason);
    });
  } catch (e) {
    // feiler vakta → la alt stå (aldri skjul ved feil)
    batch.forEach((b) => b.el.setAttribute(MARK, "seen"));
  }
}

function skann() {
  if (!settings.enabled) return;
  const tweets = document.querySelectorAll('article[data-testid="tweet"]:not([' + MARK + "])");
  const batch = [];
  tweets.forEach((el) => {
    const t = el.querySelector('[data-testid="tweetText"]');
    const text = t ? t.innerText.trim() : "";
    if (!text) { el.setAttribute(MARK, "seen"); return; }   // media-only tweet
    el.setAttribute(MARK, "pending");
    batch.push({ el, text });
  });
  // send i bolkar på 10
  for (let i = 0; i < batch.length; i += 10) behandle(batch.slice(i, i + 10));
}

// debounce på DOM-endringar (uendeleg scroll)
let timer = null;
const obs = new MutationObserver(() => {
  clearTimeout(timer);
  timer = setTimeout(skann, 400);
});
obs.observe(document.body, { childList: true, subtree: true });
skann();

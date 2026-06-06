# Lovefilter — hide the hate, keep the debate 🏳️‍🌈

A free, transparent browser extension that uses AI to hide hate, harassment and
threats in your feed — while keeping honest disagreement and criticism visible.
It also gently **lifts the supportive replies** you'd otherwise scroll past.

**Hide the hate, keep the debate.**

> Live demo: **[vakt.aeris.no](https://vakt.aeris.no)** · Privacy: [vakt.aeris.no/privacy](https://vakt.aeris.no/privacy)

![Lovefilter](store_assets/icon-128.png)

## Why this is open source

A moderation tool asks you to trust it. So everything is auditable:

- **No silent censorship** — every hidden post shows *why*, with a one-click **“Show anyway.”**
- **Fail-open** — on any classifier or network error, comments are **kept**, never hidden.
- **Reads the line, not your opinions** — the classifier prompt (below) is tuned to keep
  factual disagreement, criticism and questions, and only hide hate, harassment, threats,
  dehumanization and incitement. You can read exactly how that line is drawn in
  [`app.py`](app.py) and change the strictness (Mild / Medium / Strict).
- **No data stored** — comment text is sent to the classifier for a verdict and not retained;
  no accounts, no tracking, no ads.

## How it works

```
X / Twitter feed ──content.js──▶  vakt.aeris.no/api/klassifiser  ──▶  Gemini Flash
       ▲                                                                       │
       └──  blur · "Show anyway" · 💚 lift support  ◀── {action, category, severity, reason} ──┘
```

- `extension/` — Manifest V3 content script for `x.com` / `twitter.com`. Scans visible
  posts, batches the text, blurs anything flagged as hate/harassment/threats, and gives
  supportive replies a quiet green accent (**Wall of Love**). Severe/criminal hits are
  marked *report-worthy* — it never auto-reports.
- `app.py` — Flask classification API. Uses Google **Gemini Flash** (fast, multilingual,
  reads any language; English reasons out). Per-worker in-memory cache; nothing persisted.
- `facebook.py` — experimental Facebook/Instagram Graph API moderation (work in progress).

## Run the backend

```bash
export GEMINI_API_KEY=your_key_here   # or put GEMINI_API_KEY=... in a .env it reads
pip install flask
python3 app.py                        # serves the demo + /api/klassifiser
```

## Load the extension (unpacked)

1. Edit `extension/content.js` if you want to point `API` at your own backend.
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick `extension/`.

The published **v0.1.0** is on the Chrome Web Store; this repo is **v0.2.0** (adds Wall of Love + severity tiers).

## Status

Works on **X (Twitter)** today; more platforms coming. Built for Pride —
because Pride is about love, and so is this.

## License

[MIT](LICENSE) © 2026 KNDW Shelter Solutions AS

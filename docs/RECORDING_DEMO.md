# Recording the demo — one walkthrough video

One ~4-minute narrated video replaces the stale GIF and carries the whole story: clean-clone
setup, all nine tools, the cross-source join, and the stated limits. It lives on YouTube, embeds
on tjromack.com, and plays inline on the GitHub README.

Nothing to install — OBS Studio, ffmpeg 8.1 and Game Bar are already on this machine.

**Why one video rather than GIFs**

| | GIF | Video |
|---|---|---|
| Narration | none | spoken, so the *why* lands |
| Length | 30–50s before the file gets heavy | 4 minutes at a fraction of the size |
| Chapters | no | yes, and they double as the site's table of contents |
| README autoplay | yes | yes, if the MP4 is uploaded to GitHub (§10) |
| Site embed | heavy | one iframe |

The only thing a GIF does better is silent autoplay in a README preview — §10 solves that with an
uploaded MP4 plus a poster image.

---

## 1. Pre-flight (5 minutes)

PowerShell, from the repo root. All of this must pass **before** Claude Desktop opens.

```powershell
# Postgres up and healthy
docker ps --filter name=clinical_trials_db --format "{{.Names}} {{.Status}}"
# → clinical_trials_db  Up ... (healthy)    — if missing: docker compose up -d

# Corpus present and every server wired
foreach ($s in 'clinical','openfda_label','openfda_event','openfda_enforcement','openfda_drugsfda','pubmed') {
  $env:MCP_SUITE_SOURCE = $s
  "{0,-22}{1}" -f $s, (uv run python -m core.server --selftest | Select-Object -Last 1)
}
# → six "OK — ready for Claude Desktop" lines
```

> The loop above is PowerShell. The bash equivalent (`for s in … do … done`) only works in Git Bash
> or WSL — in PowerShell it fails with *"Missing statement body in do loop"*.

`.env` needs `ANTHROPIC_API_KEY` (summarizing tools) and `VOYAGE_API_KEY` (meaning-based ranking).
Without them search prints "lexical-only" on screen and the summarize tools error.

Then **fully restart Claude Desktop** — tray icon → Quit, not just closing the window — and check
**Settings → Developer** lists six running servers: `clinical-trials`, `fda-drug-labels`, `pubmed`,
`fda-adverse-events`, `fda-recalls`, `fda-approvals`.

## 2. Stage the desktop

- **Do not disturb on** — Settings → System → Notifications. One toast ruins a take.
- Two windows, both at **1920×1080-friendly sizes**: Windows Terminal (PowerShell, in the repo) and
  Claude Desktop. You will switch between them once.
- Terminal: increase the font (Ctrl + `+`) until text is readable at 1080p — roughly 16–18pt.
- Claude Desktop: new chat, sidebar collapsed. Chat titles and your email are personal data.
- Close anything else that could appear in a window switch.

## 3. Rehearse once — the step people skip

In a throwaway chat, run every prompt in §5 once:

1. The **first call to each tool shows a permission dialog** — answer *Allow always* so it cannot
   appear mid-take.
2. It warms caches, so the recorded run is the fast one.

Then open a **fresh chat** for the real take.

## 4. OBS setup (one-time, ~3 minutes)

1. **Settings → Video:** Base and Output resolution `1920×1080`, FPS `30`.
2. **Settings → Output → Recording:** Format `mkv` (survives a crash; remuxed to mp4 in §7),
   Encoder `x264`, Rate Control `CRF`, CRF `20`, Preset `veryfast`.
3. **Settings → Audio:** your mic as Mic/Aux. Desktop audio can stay off — nothing here makes sound.
4. **Scene → Sources → + Display Capture** (simplest, since you switch windows). Add **Audio Input
   Capture** for the mic if it isn't already in the mixer.
5. **Settings → Hotkeys:** bind Start/Stop Recording to something reachable, e.g. `Ctrl+Shift+F9`.
6. Say a test sentence and watch the mixer meter move before the real take.

---

## 5. The script

Total ~4:00. Record it in one pass; §7 trims the ends. Narration is written to be read aloud at a
normal pace — short sentences, no filler.

### 0:00 — What this is *(terminal on screen, repo visible)*

> "This is mcp-suite. Six MCP servers that put clinical trials, FDA drug data and PubMed literature
> inside Claude as tools it can call. Everything you see is public data, queried locally."

### 0:15 — Setup from a clean clone *(terminal)*

Type, and let each finish:

```powershell
docker compose up -d
```

> "One command brings up Postgres with a three-hundred document demo corpus already loaded. No
> ingest, no API keys."

```powershell
$env:MCP_SUITE_SOURCE = "clinical"; uv run python -m core.server --selftest
```

> "The selftest lists the tools this server exposes and what's actually in the corpus. That's the
> whole setup."

### 0:55 — Six servers *(switch to Claude Desktop → Settings → Developer)*

> "Six servers are connected: trials, four FDA datasets, and PubMed. Nine tools between them."

### 1:10 — Corpus status *(new chat)*

```text
Show me the corpus status
```

> "Every source reports its size, its newest record and when it last refreshed. If the data is
> stale, it says so rather than hiding it."

### 1:35 — Search by meaning

```text
Find trials of immunotherapy after surgery or ablation for liver cancer
```

> "This is hybrid retrieval — pgvector similarity fused with Postgres full-text. It matches the
> concept, not the keywords. Every result carries its registry ID and a link back to the source."

### 2:05 — Eligibility in plain language

```text
Summarize the eligibility criteria for NCT03867084 for a patient
```

> "Claude only sees the criteria text retrieved from the corpus, and it's instructed not to invent
> criteria that aren't there. Alpha-fetoprotein and Child-Pugh class come back as plain English."

### 2:45 — The cross-source join

```text
What FDA approval, label, adverse-event and recall context exists for the drugs in NCT03867084?
```

> "This one is pure SQL — no model involved. It takes the trial's drugs and joins them to FDA
> approvals, labels, adverse-event reports and recalls. Placebo is skipped, and it says so. This is
> the query that only exists because the suite shares one table."

### 3:20 — Cited literature

```text
Summarize the evidence on pembrolizumab in hepatocellular carcinoma for a clinician
```

> "Every claim cites a numbered PMID from the retrieved set. Follow the number, read the paper."

### 3:50 — Limits *(scroll to the disclaimer in the response, or the README's Limits section)*

> "Retrieval is measured, not asserted: hit-at-three is eighty percent on twenty labelled
> questions, and the misses are published with the fix. It's research tooling — not clinical
> decision support, and it holds no patient data."

---

## 6. Record

Hotkey to start, wait two seconds before speaking, run §5, wait two seconds after the last word,
hotkey to stop. Output lands in `%USERPROFILE%\Videos` as `.mkv`.

Fluffed a line? Pause, say it again, and keep rolling — §7 cuts.

## 7. Export for the web

Verified on ffmpeg 8.1. From the repo, with `<clip>` as the recording.

```powershell
$REC = "$env:USERPROFILE\Videos\<clip>.mkv"

# Compress to a web-ready MP4 (yuv420p + faststart = plays everywhere, starts instantly)
ffmpeg -y -i $REC -c:v libx264 -crf 21 -preset slow -pix_fmt yuv420p -vf "scale=1920:-2" `
  -c:a aac -b:a 128k -movflags +faststart docs/demo-walkthrough.mp4
```

Trim the dead ends (start 3.5s in, keep 4 minutes):

```powershell
ffmpeg -y -ss 3.5 -t 240 -i $REC -c:v libx264 -crf 21 -preset slow -pix_fmt yuv420p `
  -c:a aac -b:a 128k -movflags +faststart docs/demo-walkthrough.mp4
```

Check it, and grab a thumbnail from a good frame:

```powershell
ffprobe -v error -show_entries format=duration,size -of default=nw=1 docs/demo-walkthrough.mp4
ffmpeg -y -ss 165 -i docs/demo-walkthrough.mp4 -frames:v 1 -q:v 2 docs/demo-poster.jpg
```

Size targets: **under 25 MB** if you self-host on Cloudflare Pages (hard per-file limit),
**under 10 MB** for a comfortable GitHub README upload. A 4-minute screen recording at CRF 21
usually lands well under both; if not, raise CRF to 24 or scale to `1280:-2`.

## 8. Publish to YouTube

- **Visibility:** Public if you want it indexed and shareable; Unlisted works for embedding and
  keeps it off your channel page. Embedding works either way.
- **Title:** `mcp-suite — clinical trials, FDA and PubMed as MCP tools in Claude`
- **Description** (timestamps starting at `0:00` become chapters automatically):

```text
Six MCP servers that put ClinicalTrials.gov, four FDA datasets and PubMed inside Claude as
callable tools. Hybrid pgvector + full-text retrieval, cross-source SQL joins, and cited
summaries — over public data, queried locally.

Code: https://github.com/tjromack/mcp-suite
Retrieval scores and error analysis: https://github.com/tjromack/mcp-suite/blob/main/docs/EVAL_RETRIEVAL.md

0:00 What this is
0:15 Setup from a clean clone
0:55 Six servers, nine tools
1:10 Corpus status and freshness
1:35 Search by meaning
2:05 Eligibility in plain language
2:45 Cross-source join: trial to FDA record
3:20 Cited literature
3:50 Limits

Research and informational use only. Not clinical decision support. No patient data.
```

- Upload `docs/demo-poster.jpg` as the custom thumbnail.

## 9. Embed on tjromack.com

Privacy-enhanced domain, lazy-loaded, correct aspect ratio:

```html
<div style="position:relative;aspect-ratio:16/9;">
  <iframe
    src="https://www.youtube-nocookie.com/embed/VIDEO_ID"
    title="mcp-suite walkthrough — trials, FDA and PubMed as MCP tools"
    loading="lazy"
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
    allowfullscreen
    style="position:absolute;inset:0;width:100%;height:100%;border:0;border-radius:8px;"
  ></iframe>
</div>
```

Then point the project card's try-it field at it — the schema already has a `video` kind:

```ts
tryIt: { label: "Watch the 4-min walkthrough", href: "https://youtu.be/VIDEO_ID", kind: "video" }
```

Self-hosting instead of YouTube: drop `docs/demo-walkthrough.mp4` into the site's `public/`, then
`<video src="/demo-walkthrough.mp4" poster="/demo-poster.jpg" controls preload="none" playsinline>`.
Keep it under Cloudflare Pages' 25 MiB per-file limit.

## 10. README

GitHub markdown ignores `<iframe>`, but it *does* render an uploaded MP4 as an inline player.

1. Open a new issue on the repo (don't submit it), drag `docs/demo-walkthrough.mp4` into the
   comment box, and wait for the upload to produce a
   `https://github.com/user-attachments/assets/…` URL. Copy it, then close the tab.
2. In the `## Demo` section, replace the GIF and its caveat with:

```markdown
https://github.com/user-attachments/assets/YOUR-ASSET-ID

*Four minutes: clean-clone setup, hybrid search, plain-language eligibility, the cross-source
FDA join, and cited literature. [Watch on YouTube](https://youtu.be/VIDEO_ID).*
```

3. Delete the old `> **Caveat:**` block and, once the video is live, `docs/demo.gif` itself.

A poster image linking out also works if you skip the upload:

```markdown
[![mcp-suite walkthrough](docs/demo-poster.jpg)](https://youtu.be/VIDEO_ID)
```

## 11. Ship it

```powershell
uv run python -m pytest tests/ -q      # keep main green
git add docs/demo-walkthrough.mp4 docs/demo-poster.jpg README.md
git rm docs/demo.gif                   # once the video is live
git commit -m "docs: replace demo GIF with a narrated walkthrough video"
git push
```

Then open the repo on GitHub and confirm the player renders, and the site page and check the embed.

## 12. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Missing statement body in do loop` | bash syntax pasted into PowerShell | Use the `foreach` loop in §1 |
| Toast: "Couldn't start … Connection closed" | Postgres wasn't running when Desktop launched | `docker compose up -d`, then ask again — the server reconnects without a restart |
| A tool answers "Database unavailable" | Same, mid-session | Start Postgres; no restart needed |
| Search output says "lexical-only" | No `VOYAGE_API_KEY` in `.env` | Add it, restart Desktop, re-record |
| Permission dialog mid-take | Rehearsal skipped | Stop, answer *Allow always*, fresh chat |
| Claude picks the wrong server | Six servers expose same-named tools | Name the source in the prompt ("in the FDA recalls data …") |
| Server missing from Settings → Developer | Config not reloaded | Quit fully from the tray, not the window |
| OBS records a black screen | Display Capture on a hybrid-GPU laptop | Run OBS as administrator, or switch the source to Window Capture |
| MP4 too big for Cloudflare Pages | CRF too low / 1080p | `-crf 24`, or `scale=1280:-2` |

---

*Tool timings on this machine, for pacing the script: `semantic_search` 1.5–3s · `find_similar`
~0.7s · `get_details` 0.2–1.8s · `drug_context_for_trial` <0.1s · `summarize_eligibility` ~10s ·
`summarize_safety_profile` ~11s · `summarize_evidence` ~15s. The three summarizing tools are the
only real pauses — keep narrating over them.*

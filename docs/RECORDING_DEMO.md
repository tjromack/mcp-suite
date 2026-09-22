# Recording the demo GIFs — runbook

Seven short GIFs, each showing one idea, together covering all nine tools across the six servers.
Short clips beat one long video here: they drop straight into a README section, a site card and a
case study at the exact point being made, and they need no narration.

Budget ~45 minutes for all seven, ~15 for the three marked **core**.

## 0. The shot list

| # | File | Shows | Tools captured | Target |
|---|---|---|---|---|
| 1 | `01-setup.gif` | `docker compose up` → `--selftest` in the terminal | — (proves the try-it path) | ~20s |
| 2 | `02-search.gif` **core** | Search by meaning, then "trials like this one" | `semantic_search`, `find_similar` | ~20s |
| 3 | `03-eligibility.gif` **core** | Full registry record, then plain-language criteria | `get_details`, `summarize_eligibility` | ~25s |
| 4 | `04-cross-source.gif` **core** | Trial's drugs joined to FDA approvals/labels/FAERS/recalls | `drug_context_for_trial` | ~20s |
| 5 | `05-safety.gif` | Balanced drug safety profile from three FDA datasets | `summarize_safety_profile` | ~25s |
| 6 | `06-evidence.gif` | Literature for a trial, then a cited synthesis | `evidence_for_trial`, `summarize_evidence` | ~25s |
| 7 | `07-corpus-status.gif` | Per-source size, freshness, 7-day usage | `corpus_status` | ~12s |

Nine tools, seven files. If time is short, record 2, 3 and 4 — those carry the README.

---

## 1. Install and configure ScreenToGif (one-time, ~4 minutes)

```powershell
winget install NickeManarin.ScreenToGif
```

Open it and choose **Recorder**, then set:

- **Size:** type `1280 × 800` into the width/height boxes at the bottom of the recorder frame. A
  fixed size across all seven keeps them visually consistent in the README.
- **FPS:** `10`. Higher only inflates the file; nothing here animates fast.
- **Options (gear icon) → Recorder:** uncheck *Capture the cursor* — a hovering pointer is noise
  while someone is reading tool output.

After each recording the Editor opens. Do three things before saving:

1. **Edit → Remove Duplicates** (threshold default) — kills the frames where nothing changed.
2. **Image → Crop** if the frame caught a window edge.
3. **File → Save As → Gif**, Encoder **FFmpeg**, quality high, *Loop* enabled.

Save straight into `docs/media/` using the filenames from §0.

## 2. Pre-flight (PowerShell, from the repo root)

```powershell
docker ps --filter name=clinical_trials_db --format "{{.Names}} {{.Status}}"
# → clinical_trials_db  Up ... (healthy)    — if missing: docker compose up -d

foreach ($s in 'clinical','openfda_label','openfda_event','openfda_enforcement','openfda_drugsfda','pubmed') {
  $env:MCP_SUITE_SOURCE = $s
  "{0,-22}{1}" -f $s, (uv run python -m core.server --selftest | Select-Object -Last 1)
}
# → six "OK - ready for Claude Desktop" lines
```

> This loop is PowerShell. The bash form (`for s in … do … done`) fails here with
> *"Missing statement body in do loop"*. Paste it as one block, not line by line.

`.env` needs `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`, or search prints "lexical-only" on screen and
the summarizing tools error out.

Then **fully restart Claude Desktop** (tray → Quit, not the window) and confirm **Settings →
Developer** shows six running servers.

## 3. Stage the window

- **Do not disturb on** — Settings → System → Notifications.
- New chat, sidebar collapsed: chat titles and your email are personal data on a public README.
- Terminal font up to ~16pt for `01-setup.gif` (Ctrl + `+`).
- Position the ScreenToGif frame over the conversation area only — not the whole app window. Tighter
  framing means readable text at README width.

## 4. Rehearse once

Run every prompt below in a throwaway chat first. The **first call to each tool shows a permission
dialog** — answer *Allow always*, or it will appear mid-take. It also warms caches so the recorded
run is the fast one. Then start a **fresh chat**.

---

## 5. The seven takes

Prompts are verbatim. Start recording just before hitting enter; stop as soon as the answer finishes
rendering. Don't scroll while text streams.

### 1 — `01-setup.gif` *(terminal)*

```powershell
docker compose up -d
```
```powershell
$env:MCP_SUITE_SOURCE = "clinical"; uv run python -m core.server --selftest
```

Shows the container starting and the selftest printing tools, corpus counts and freshness. This is
the "it actually runs" proof for the README's try-it section.

### 2 — `02-search.gif` **core**

```text
Find trials of immunotherapy after surgery or ablation for liver cancer
```
```text
Show me trials similar to NCT03867084
```

*~2s each. Ranked hits with NCT ids, similarity scores, registry links.*

### 3 — `03-eligibility.gif` **core**

```text
Get the full registry record for NCT03867084
```
```text
Summarize the eligibility criteria for NCT03867084 for a patient
```

*~10s for the summary. Let the first few bullets render — AFP and Child-Pugh in plain English are
the payoff — then stop.*

### 4 — `04-cross-source.gif` **core**

```text
What FDA approval, label, adverse-event and recall context exists for the drugs in NCT03867084?
```

*Under a second. Keytruda's BLAs, the label, 2025 FAERS reports, a Class II recall, and "Skipped
(non-drug comparators): Placebo". No model wrote this — it's a SQL join.*

### 5 — `05-safety.gif`

```text
Give me the FDA safety profile for pembrolizumab for a clinician
```

*~11s. Capture through the "Adverse-Event Signals" section so the reports-are-not-causation framing
and the appended FDA disclaimer are both visible.*

### 6 — `06-evidence.gif`

```text
What published evidence relates to NCT03867084?
```
```text
Summarize the evidence on pembrolizumab in hepatocellular carcinoma for a clinician
```

*~15s for the synthesis. Make sure at least one `[n]` citation is on screen at the end.*

### 7 — `07-corpus-status.gif`

```text
Show me the corpus status
```

*Instant. Six sources with counts, newest record, last refresh, and 7-day tool usage.*

---

## 6. Size check, and the fix if they're heavy

```powershell
Get-ChildItem docs/media/*.gif | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,2)}}
```

Aim for **≤3 MB each** (the old single GIF was 8.6 MB, which is slow to load). If one is over,
re-encode it — verified on ffmpeg 8.1:

```powershell
ffmpeg -y -i docs/media/04-cross-source.gif `
  -vf "fps=10,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" `
  -loop 0 docs/media/04-cross-source.gif
```

Still heavy? In order: `fps=8`, `scale=1000:-1`, `max_colors=64`, then trim the take.

A still for the site card or social preview, if one is wanted:

```powershell
ffmpeg -y -sseof -2 -i docs/media/04-cross-source.gif -frames:v 1 -q:v 2 docs/media/poster.jpg
```

## 7. Hand-off

Save everything into `docs/media/` with the exact filenames from §0, then say so — the wiring is
mechanical from there: size checks, alt text, README sections, the case-study page and the site
snippets all follow the placement map below. Odd names are fine as long as it's obvious which tool
each one shows; they'll be renamed to the convention.

## 8. Where each GIF goes

### README

| GIF | Section |
|---|---|
| `01-setup` | **Try it in five minutes**, right after the `docker compose up -d` block |
| `02-search`, `03-eligibility` | **Demo** — the two hero clips, replacing `docs/demo.gif` |
| `04-cross-source` | **Demo**, under a "Cross-source in one question" subhead |
| `05-safety`, `06-evidence` | **What It Is**, beside the server table rows they demonstrate |
| `07-corpus-status` | **How it's verified**, next to the freshness claim |

Markdown pattern — alt text is what a screen reader and a failed image load show, so it describes
the *content*, not the file:

```markdown
![Claude calling semantic_search over the local corpus and returning ranked liver-cancer immunotherapy trials with NCT ids and similarity scores](docs/media/02-search.gif)
```

Then delete the stale `docs/demo.gif` and the `> **Caveat:**` block under `## Demo`.

### Website (tjromack.com)

- **Project card:** `tryIt: { label: "Watch it run", href: "<case-study URL>#demo", kind: "demo" }`
- **Case study page**, mapped to the template's sections:
  - *The situation* → `02-search.gif`
  - *The design* → `04-cross-source.gif` (the join is the architectural claim)
  - *How it's verified* → `07-corpus-status.gif`, beside the retrieval numbers
  - *What broke* → no GIF; that section is prose
- Host them from the site's `public/media/` rather than hotlinking GitHub, and give every `<img>` a
  `loading="lazy"` and a `width`/`height` so the page doesn't reflow:

```html
<img src="/media/04-cross-source.gif" alt="A clinical trial's drugs joined to FDA approval, label, adverse-event and recall records, with placebo arms skipped" width="1280" height="800" loading="lazy" />
```

### Case study document

Same three GIFs as the site page. The remaining four stay in the README — a case study with seven
animations reads as a highlight reel rather than an argument.

## 9. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Missing statement body in do loop` | bash syntax in PowerShell | Use the `foreach` loop in §2, pasted as one block |
| Terminal text shows `ù` or `º` | Console codepage mangling em dashes | Fixed — selftest output is ASCII now; `git pull` if you see it |
| Toast: "Couldn't start … Connection closed" | Postgres wasn't running when Desktop launched | `docker compose up -d`, then ask again — the server reconnects, no restart |
| A tool answers "Database unavailable" | Same, mid-session | Start Postgres |
| Search says "lexical-only" | No `VOYAGE_API_KEY` in `.env` | Add it, restart Desktop, re-record |
| Permission dialog mid-take | Rehearsal skipped | Stop, *Allow always*, fresh chat |
| Claude picks the wrong server | Six servers share tool names | Name the source in the prompt ("in the FDA recalls data …") |
| GIF looks washed out | Palette too small | Raise `max_colors` to 192 in the ffmpeg command |

---

*Tool timings on this machine, for pacing takes: `semantic_search` 1.5–3s · `find_similar` ~0.7s ·
`get_details` 0.2–1.8s · `drug_context_for_trial` <0.1s · `corpus_status` <0.1s ·
`summarize_eligibility` ~10s · `summarize_safety_profile` ~11s · `summarize_evidence` ~15s.*

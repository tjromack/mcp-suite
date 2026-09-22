# Recording the demo — one take, two deliverables

Record **one continuous ~5-minute walkthrough**. That single file becomes:

1. a **YouTube video** — embedded on tjromack.com and linked from the project card, and
2. **seven short clips** cut out of it — the GIFs/MP4s that drop into README and case-study
   sections at the exact point each one makes.

No stop-start between takes, no re-staging, and the clips stay visually consistent because they
come from the same recording.

---

## 1. Pre-flight

```
docker ps --filter name=clinical_trials_db --format "{{.Names}} {{.Status}}"
uv run python -m core.server --selftest --source clinical
```

Both work in cmd, PowerShell and bash. Expect `Up … (healthy)` and a corpus table ending in
`OK - ready for Claude Desktop`.

`.env` needs `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`, or search prints "lexical-only" on screen and
the summarizing tools error.

Then **fully restart Claude Desktop** (tray → Quit) and confirm **Settings → Developer** lists six
running servers.

**Stage:** Do Not Disturb on · new chat, sidebar collapsed · terminal ~110 columns so the tools line
doesn't wrap · rehearse every prompt once in a throwaway chat so the *Allow always* permission
dialogs are already answered.

## 2. Recording

OBS (Display Capture, 1920×1080, 30fps) is the right tool here because the take switches between
terminal and Claude Desktop. Mic on if you're narrating.

**Leave ~2 seconds of silence between sections.** That dead air is where the clips get cut, and it
makes the section boundaries easy to find.

Narration is optional — the prompts and output carry the story on their own, and YouTube chapters
label the sections either way. If you do narrate, the lines below are written to be read aloud.

---

## 3. The run order

Every prompt verbatim. Nine tools, ~5:15 total. Timings are approximate — the actual cut points get
found from the recording.

### A · ~0:00 — What this is *(terminal on screen)*

> "This is mcp-suite. Six MCP servers that put clinical trials, FDA drug data and PubMed literature
> inside Claude as tools it can call. All public data, queried locally."

### B · ~0:15 — Setup *(terminal)* → clip `01-setup`

```
docker compose up -d
```
```
uv run python -m core.server --selftest --source clinical
```

> "One command brings up Postgres with a demo corpus already loaded — no ingest, no API keys. The
> selftest prints the tools this server exposes and what's actually in the corpus."

### C · ~0:55 — Six servers *(Claude Desktop → Settings → Developer)*

> "Six servers connected: trials, four FDA datasets, and PubMed. Nine tools between them."

### D · ~1:10 — Corpus status *(new chat)* → clip `07-corpus-status`

```text
Show me the corpus status
```

> "Every source reports its size, its newest record, and when it last refreshed. Stale data says so
> rather than hiding it."

### E · ~1:30 — Search by meaning → clip `02-search` (with F)

```text
Find trials of immunotherapy after surgery or ablation for liver cancer
```

> "Hybrid retrieval — pgvector similarity fused with Postgres full-text. It matches the concept, not
> the keywords, and every hit links back to the registry."

### F · ~1:55 — Nearest neighbours

```text
Show me trials similar to NCT03867084
```

> "Same idea from the other direction: nearest neighbours of one trial's stored embedding."

### G · ~2:15 — The live record → clip `03-eligibility` (with H)

```text
Get the full registry record for NCT03867084
```

> "This one leaves the local corpus and fetches the current record from ClinicalTrials.gov."

### H · ~2:35 — Eligibility in plain language *(~10s to run)*

```text
Summarize the eligibility criteria for NCT03867084 for a patient
```

> "Claude only sees the criteria retrieved from the corpus, and it's instructed not to invent
> criteria that aren't there. Alpha-fetoprotein and Child-Pugh come back as plain English."

### I · ~3:10 — The cross-source join → clip `04-cross-source`

```text
What FDA approval, label, adverse-event and recall context exists for the drugs in NCT03867084?
```

> "No model wrote this — it's a SQL join. The trial's drugs against FDA approvals, labels,
> adverse-event reports and recalls. Placebo is skipped, and it says so. This query only exists
> because the suite shares one table."

### J · ~3:35 — Drug safety profile *(~11s to run)* → clip `05-safety`

```text
Give me the FDA safety profile for pembrolizumab for a clinician
```

> "Three FDA datasets in one answer, with label warnings kept separate from unverified reports, and
> the disclaimer appended by code rather than left to the model."

### K · ~4:05 — Literature for a trial → clip `06-evidence` (with L)

```text
What published evidence relates to NCT03867084?
```

### L · ~4:25 — Cited synthesis *(~15s to run)*

```text
Summarize the evidence on pembrolizumab in hepatocellular carcinoma for a clinician
```

> "Every claim cites a numbered PMID from the retrieved set. Follow the number, read the paper."

### M · ~5:00 — Limits *(scroll to the disclaimer, or the README's Limits section)*

> "Retrieval is measured, not asserted — hit-at-three is eighty percent on twenty labelled
> questions, with the misses published. It's research tooling: not clinical decision support, and it
> holds no patient data."

---

## 4. Hand-off

Save the recording anywhere and say where it is. From one file everything else is mechanical:

- Section boundaries get found by sampling frames from the recording — no need to log timestamps
  while you record, though rough ones speed it up.
- Each clip is cut with `ffmpeg -ss … -to …`, then converted to GIF or a small MP4.
- Sizes checked, alt text written, README and case-study sections wired, site snippets produced.

What comes back: the clips in `docs/media/`, a YouTube description with chapter timestamps, the
embed snippet for the site, and the files to copy into the site repo.

## 5. Where it all goes

| Surface | What it gets |
|---|---|
| **YouTube** | the full take, chaptered by the sections above |
| **README** | `01-setup` in the try-it section · `02-search` + `03-eligibility` as the hero · `04-cross-source` under its own subhead · `05-safety` and `06-evidence` beside the server table · `07-corpus-status` next to the freshness claim · full video linked at the top of Demo |
| **Case study** | `02-search` (the situation) · `04-cross-source` (the design) · `07-corpus-status` (how it's verified) · the video embedded once |
| **Project card** | `tryIt: { label: "Watch the walkthrough", href: "<youtube url>", kind: "video" }` |

GitHub markdown ignores `<iframe>`, so the README gets an uploaded MP4 (drag it into a new issue,
don't submit, copy the `user-attachments` URL) or a poster image linking to YouTube. The short
clips carry the individual sections either way.

## 6. Reference commands

```
# Cut a clip (exact, re-encoded)
ffmpeg -y -ss 95 -to 132 -i take.mp4 -c:v libx264 -crf 21 -preset veryfast -pix_fmt yuv420p -c:a aac -movflags +faststart docs/media/02-search.mp4

# Same range as a GIF
ffmpeg -y -ss 95 -to 132 -i take.mp4 -vf "fps=10,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 docs/media/02-search.gif

# Whole take, compressed for the web
ffmpeg -y -i take.mp4 -c:v libx264 -crf 21 -preset slow -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart docs/demo-walkthrough.mp4

# Thumbnail
ffmpeg -y -ss 200 -i take.mp4 -frames:v 1 -q:v 2 docs/media/poster.jpg
```

Targets: clips ≤3 MB as GIF (or ≤2 MB as MP4), full video under 25 MB if self-hosted on Cloudflare
Pages, under 10 MB for a GitHub README upload.

## 7. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `$env:...` errors in the terminal | PowerShell syntax in cmd.exe | Use `--source clinical`; it needs no env var |
| `Missing statement body in do loop` | bash loop pasted into PowerShell | Run the single-source command once per source |
| Toast: "Couldn't start … Connection closed" | Postgres wasn't running when Desktop launched | `docker compose up -d`, then ask again — no restart needed |
| A tool answers "Database unavailable" | Same, mid-session | Start Postgres |
| Search says "lexical-only" | No `VOYAGE_API_KEY` | Add it to `.env`, restart Desktop |
| Permission dialog mid-take | Rehearsal skipped | Stop, *Allow always*, fresh chat |
| Claude picks the wrong server | Six servers share tool names | Name the source ("in the FDA recalls data …") |
| OBS records black | Display Capture on hybrid graphics | Run OBS as administrator, or use Window Capture |

---

*Tool timings, for pacing: `semantic_search` 1.5–3s · `find_similar` ~0.7s · `get_details` 0.2–1.8s ·
`drug_context_for_trial` <0.1s · `corpus_status` <0.1s · `summarize_eligibility` ~10s ·
`summarize_safety_profile` ~11s · `summarize_evidence` ~15s. The three summarizing tools are the
only real pauses — narrate over them, or let them run.*

---

## Appendix — the 2026-09-22 take

`mcp-suite-demo.mkv`, 10:38, 1080p60, silent. Clips in `docs/media/` were cut from it with the
timings below (source seconds), cropped to `1920x1040` to drop the taskbar, sped up so each clip
reads in about 15 seconds. Re-cut any of them without re-recording:

| Clip | MP4 window | GIF window (payoff) | Speed |
|---|---|---|---|
| `01-corpus-status` | 22–52 | 28–52 | 1.0 / 1.8 |
| `02-search` | 112–150 | 124–150 | 1.5 / 1.9 |
| `03-find-similar` | 196–226 | 200–224 | 1.3 / 1.8 |
| `04-eligibility` | 294–330 | 300–328 | 1.3 / 2.0 |
| `05-cross-source` | 328–402 | 376–402 | 2.2 / 1.9 |
| `06-safety` | 412–458 | 430–458 | 1.8 / 2.0 |
| `07-evidence` | 468–508 | 482–508 | 1.6 / 1.9 |
| `08-cited-synthesis` | 554–626 | 598–626 | 2.2 / 2.0 |

**Not captured:** `get_details` (the live registry fetch). Eight of the nine tools appear. A 20-second
take — *"Get the full registry record for NCT03867084"* — closes the gap whenever convenient.

**If a future take is narrated**, keep the audio: this one is silent (mean volume −47 dB), which is
fine for clips but thin for a standalone video.

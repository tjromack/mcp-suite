# Recording the demo GIFs — runbook

Everything needed to replace `docs/demo.gif` (which predates the Phase-A tool rename) and,
optionally, add two short clips for the cross-source tools. Nothing to install: Windows Game Bar
records, ffmpeg 8.1 converts — both already on this machine.

**What you end up with**

| File | Shows | Required? |
|---|---|---|
| `docs/demo.gif` | `semantic_search` → `summarize_eligibility` | **Yes** — replaces the stale clip |
| `docs/demo-cross-source.gif` | `drug_context_for_trial` (trial → FDA join) | Optional, highest-value extra |
| `docs/demo-evidence.gif` | `summarize_evidence` (cited literature) | Optional |

Budget ~30 minutes for all three, ~15 for the required one.

---

## 1. Pre-flight (5 minutes)

Run from the repo. Everything here should pass *before* Claude Desktop opens.

```bash
# Postgres up and healthy
docker ps --filter name=clinical_trials_db --format "{{.Names}} {{.Status}}"
# → clinical_trials_db  Up ... (healthy)    — if missing: docker compose up -d

# Corpus present and every server wired
for s in clinical openfda_label openfda_event openfda_enforcement openfda_drugsfda pubmed; do
  MCP_SUITE_SOURCE=$s uv run python -m core.server --selftest | tail -1
done
# → six "OK — ready for Claude Desktop" lines
```

Keys: `.env` needs `ANTHROPIC_API_KEY` (the summarizing tools) and `VOYAGE_API_KEY` (meaning-based
ranking). Without them the clips still record, but search prints "lexical-only" on screen and the
summarize tools error — not what belongs in the hero GIF.

Then **fully restart Claude Desktop**: tray icon → Quit (closing the window is not enough), reopen,
and check **Settings → Developer** lists six servers, all running: `clinical-trials`,
`fda-drug-labels`, `pubmed`, `fda-adverse-events`, `fda-recalls`, `fda-approvals`.

---

## 2. Set the stage

- **Do not disturb on** — Settings → System → Notifications → Do not disturb. One toast ruins a take.
- **New chat**, sidebar collapsed if possible: chat titles are personal data on a public README.
- Scan the window for your email / account name before rolling.
- Resize the Claude window to roughly **1400×900**. Larger only makes a heavier GIF; smaller makes
  tool output unreadable.
- Either theme works; dark reads better against GitHub's default.

## 3. Rehearsal pass — the step people skip

In a throwaway chat, run each prompt below **once**. Two reasons:

1. The **first call to each tool shows a permission dialog.** Choose *Allow always* so it cannot
   appear mid-take.
2. It warms caches, so the recorded run is the fast one.

Then open a **fresh chat** for the real take.

---

## 4. Take 1 — `docs/demo.gif` (required, target 35–50s)

Focus the Claude window and press **Win + Alt + R** to start recording, again to stop. Output lands
in `%USERPROFILE%\Videos\Captures` as `.mp4`. (If Game Bar says gaming features aren't available,
`winget install NickeManarin.ScreenToGif` and record with that.)

Type these verbatim, waiting for each answer:

```text
Find trials of immunotherapy after surgery or ablation for liver cancer
```

*~2s. Expect a ranked list with NCT ids, similarity scores and registry links.*

```text
Summarize the eligibility criteria for NCT03867084 for a patient
```

*~10s. Expect plain-language inclusion/exclusion bullets — AFP and Child-Pugh explained in English.*

Stop recording once the summary finishes rendering. Don't narrate, and don't scroll while text streams.

## 5. Take 2 — `docs/demo-cross-source.gif` (optional, ~25s)

The differentiator: one question crossing four FDA datasets, with no AI writing involved.

```text
What FDA approval, label, adverse-event and recall context exists for the drugs in NCT03867084?
```

*Under a second. Expect Keytruda's BLAs, the label, 2025 FAERS reports, a Class II recall — and
"Skipped (non-drug comparators): Placebo".*

## 6. Take 3 — `docs/demo-evidence.gif` (optional, ~35s)

```text
Summarize the evidence on pembrolizumab in hepatocellular carcinoma for a clinician
```

*~15s. Expect a cited summary where every claim carries a [n] mapping to a PMID.*

---

## 7. Convert to GIF

From the repo, with `<clip>` as the recorded file name. Verified on ffmpeg 8.1.

```bash
CAP="$USERPROFILE/Videos/Captures"

ffmpeg -y -i "$CAP/<clip>.mp4" \
  -vf "fps=10,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 docs/demo.gif
```

Two adjustments you will probably want:

```bash
# Trim — start 2.5s in, keep 40s (drops the click that started the recording)
ffmpeg -y -ss 2.5 -t 40 -i "$CAP/<clip>.mp4" \
  -vf "fps=10,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 docs/demo.gif

# Speed up dead air 2x (the ~10s wait while Claude writes the summary)
ffmpeg -y -i "$CAP/<clip>.mp4" \
  -vf "setpts=PTS/2,fps=10,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 docs/demo.gif
```

Check the result — **aim under 5 MB** (the old clip was 8.6 MB, slow to load on a README):

```bash
ls -lh docs/demo.gif
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,nb_frames -of default=nw=1 docs/demo.gif
```

Still too big? In order: `fps=8`, then `scale=1000:-1`, then `max_colors=64`, then trim harder.

---

## 8. Update the README

1. **Delete the caveat block** under `## Demo` — the paragraph starting
   `> **Caveat:** this clip predates the Phase-A refactor`. It exists only because the GIF was stale.
2. Leave the image and its caption: both are already accurate for the new clip.
3. If you recorded the optional takes, add them under the caption:

```markdown
**Cross-source in one question** — a trial's drugs joined to FDA approvals, labels, adverse events and recalls, with placebo arms skipped:

![drug_context_for_trial joining NCT03867084's interventions to FDA approval, label, FAERS and recall records](docs/demo-cross-source.gif)

**Cited evidence** — every claim maps to a retrieved PMID:

![summarize_evidence producing a cited synthesis of pembrolizumab literature in hepatocellular carcinoma](docs/demo-evidence.gif)
```

Alt text matters: it is what a screen reader and a failed image load show.

## 9. Ship it

```bash
uv run python -m pytest tests/ -q     # unrelated to the GIF, but keep main green
git add docs/demo.gif docs/demo-cross-source.gif docs/demo-evidence.gif README.md
git commit -m "docs: re-record demo GIFs on current tool names"
git push
```

Then open the repo on GitHub and confirm the images render and loop.

## 10. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Toast: "Couldn't start … Connection closed" | Postgres wasn't running when Desktop launched | `docker compose up -d`, then ask again — the server reconnects without a restart (fixed in `47aca10`) |
| A tool answers "Database unavailable" | Same thing, mid-session | Start Postgres; no restart needed |
| Search output says "lexical-only" | No `VOYAGE_API_KEY` in `.env` | Add it, restart Desktop, re-record |
| Permission dialog appears mid-take | Rehearsal skipped | Stop, answer *Allow always*, start a fresh chat |
| Claude picks the wrong server | Six servers expose same-named tools | Name the source in the prompt ("in the FDA recalls data …") |
| A server is missing from Settings → Developer | Config not reloaded | Quit fully from the tray, not the window |
| Game Bar won't record | Gaming features disabled | `winget install NickeManarin.ScreenToGif` |

---

*Tool timings measured on this machine, for planning takes: `semantic_search` 1.5–3s ·
`find_similar` ~0.7s · `get_details` 0.2–1.8s · `drug_context_for_trial` <0.1s ·
`summarize_eligibility` ~10s · `summarize_safety_profile` ~11s · `summarize_evidence` ~15s.*

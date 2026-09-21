# Bundled demo corpus

`seed/02-demo-corpus.sql.gz` is a 300-document slice of the full suite corpus so a
fresh clone is queryable immediately:

| Source | Documents |
|---|---|
| `clinical` (ClinicalTrials.gov) | 120 |
| `pubmed` (abstracts) | 50 |
| `openfda_event` (FAERS reports) | 40 |
| `openfda_drugsfda` (approvals) | 35 |
| `openfda_enforcement` (recalls) | 30 |
| `openfda_label` (SPL labels) | 25 |

## How it loads

`docker-compose.yml` mounts `core/store/schema.sql` and this file into the Postgres
image's `/docker-entrypoint-initdb.d`. The entrypoint runs them **only when the data
directory is empty**, so `docker compose up -d` seeds a new volume and leaves an
existing one untouched. To force a reload:

```bash
docker compose down -v && docker compose up -d   # destroys local data
```

## What's in it

Embeddings are included, so `find_similar` and hybrid `semantic_search` work offline.
Records are chosen, not sampled at random: the documents behind the README's example
queries are pinned (trial `NCT03867084` and its comparators, the Keytruda label and
approvals, a pembrolizumab recall), and the rest are top full-text matches for a
handful of themes per source, so searches return something coherent.

It is a **demo slice, not a mirror** — most upstream records are absent, and the
freshness dates are whenever the author last refreshed. Use `corpus_status` to see
what any given database actually holds.

## Regenerating

Against a full local corpus:

```bash
uv run python scripts/export_demo_corpus.py
```

Selection (pinned ids, themes, per-source budgets) lives at the top of that script.

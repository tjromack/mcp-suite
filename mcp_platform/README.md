# mcp_platform/ — gateway / billing / deploy (placeholder)

> **Naming note:** the plan calls this `platform/`, but a top-level
> `platform/` package shadows Python's stdlib `platform` module (which
> pytest itself imports during collection), so it's renamed to
> `mcp_platform/`. Function and scope are unchanged from the plan.

Not part of Phase A. Reserved for the cross-cutting "suite" layer once two or
more servers are live and the strategy doc's metering / Pro/Suite tiers are
worth implementing.

Likely future contents:
- An auth + per-tenant key gateway in front of all servers.
- Tool-call metering (Moesif / Dedalus / a marketplace meter) instrumented
  at the gateway so pricing decisions are usage-driven.
- Shared deploy manifests (Dockerfiles per server, Compose / a thin
  Kubernetes layer).

See `dev-dashboard/docs/mcp-suite/03-pricing-and-positioning.md` for the
strategic rationale; nothing concrete lands here until a paying customer or
clear usage signal exists (per the plan's gate in Phase C).

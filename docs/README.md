# shadowraven — design & RE dependencies

This mod's design and the reverse-engineering it depends on are documented in the
**AC1_RE** repo (pinned here as the `ac1_re/` submodule), so the design stays next to
the symbols it references.

## Where the design lives

- **Design + RE backlog:** `ac1_re/docs/MOD_DESIGN.md` *(draft — to be committed to
  AC1_RE)*. Covers the perception keystone, the dismount/sentry/jacking model,
  destructibility tiering, modular sensors, and the prioritized RE backlog.
- **De-risk prompt (Goals 0 / A / B):** the three feasibility questions to answer in
  AC1_RE before mod authoring begins.

## The de-risk gate (summary)

Mission/asset authoring in `mods/shadowraven/` starts once these are confirmed in AC1_RE:

| Goal | Question | Why it gates the mod |
|------|----------|----------------------|
| **0** | Can enemy detection be made LOS/FOV/range-gated? | Stealth is the mod's identity — keystone. |
| **A** | Is "deal area damage on a flag" data-only or new code? | Airstrike + mech-breach beats. |
| **B** | Can player control move to an on-foot human entity? | Decides if on-foot is a reskin or research. |

Once Goal 0 is confirmed feasible, the `[systems]` flags in
[`../mods/shadowraven/mod.toml`](../mods/shadowraven/mod.toml) move from
`blocked-on-re` to `in-progress`.

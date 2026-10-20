# shadowraven

The mod itself. See [`mod.toml`](mod.toml) for the manifest and system status.

## Folders

- **`missions/`** — authored mission logic as chunk-4 bytecode, assembled with
  `ac1_re/tools/mission/mission_script.py` (round-trippable). This is where the
  alert → reinforcements → airstrike-timer → objective flow lives.
- **`assets/`** — replacement textures (TIM), geometry (PA), and audio. **Recipes and
  original art only — never extracted game bytes.**
- **`patch/`** — C / MIPS patches that bake into the in-mission overlay (FDAT 202 @
  `0x80050000`) and repack with checksum fix-up. The keystone **perception gate**,
  **sentry-mode** think-handler, and the **scripted-strike** primitive land here.

## Before authoring

The mod's core systems are gated on the AC1_RE de-risk (Goals 0 / A / B — see
[`../../docs/`](../../docs)). Confirm Goal 0 (perception) is feasible before investing
in mission/asset content, since stealth is the mod's whole identity.

# AC1mod — Stealth-Infiltration Mod for Armored Core (SLUS-01323)

> **Codename: `shadowraven`.** A stealth/infiltration overhaul for the PlayStation
> game *Armored Core* (1997, North American `SLUS-01323` v1.1): perception-gated
> enemies, dismount-and-infiltrate on foot, modular sensors, sentry-mode mechs, and
> alert-driven reinforcements/airstrikes.

> **This repository previously hosted the AC1mod viewer/CLI tool.** That tooling now
> lives in the **[AC1_RE](https://github.com/byroweb/ac1_re)** repo (under `ac1mod/`),
> alongside the reverse-engineering notes it's interdependent with. The standalone
> viewer history is preserved in this repo's `main` at commit `616bcd2` and in AC1_RE.
> This repo is now **the mod**.

## No game data

Ships **only** original mod source — mission scripts, binary patches, and asset
*recipes*. **No game executable, assets, or disc data** are included or distributed.
You supply your own legally obtained copy of `SLUS-01323`; the build operates on data
*you* extract from it, on your machine.

## How it's built

The mod is produced as a **patch** applied to your own disc image, using the AC1_RE
toolchain (extraction, the mission-script assembler, `.T` repack, reinject). AC1_RE is
pinned here as a git submodule so builds are reproducible against a known engine RE.

```sh
git clone --recurse-submodules https://github.com/byroweb/AC1mod.git
# or, after a plain clone:
git submodule update --init --recursive
```

Then (once the build is implemented — see Status):

```sh
python build.py --bin /path/to/your/SLUS-01323.bin --mod mods/shadowraven
```

## Layout

```
mods/shadowraven/      # the mod
  mod.toml             #   metadata: version, target game, engine pin, systems
  missions/            #   authored chunk-4 mission scripts (via ac1_re mission_script.py)
  assets/              #   replacement TIM / PA geometry / audio
  patch/               #   C / MIPS patches for the in-mission overlay (FDAT 202)
build.py               # extract -> patch -> repack -> reinject pipeline (stub)
ac1_re/                # AC1_RE toolchain + RE notes, pinned submodule
docs/                  # design + the RE backlog this mod depends on
```

## Status

**Pre-implementation.** The mod's feasibility hinges on a short reverse-engineering
de-risk tracked in AC1_RE — in priority order:

0. **Perception gate** (make enemy detection respect line of sight) — *the keystone*.
1. **Scripted area damage** (airstrike / mech-breach defenses).
2. **Player-control reassignment** (on-foot dismount).

See [`docs/`](docs/) for the design and the full backlog. Authoring of missions/assets
begins once Goal 0 is confirmed feasible.

## Legal

Unofficial, non-commercial fan modification for interoperability, preservation, and
education. Not affiliated with or endorsed by FromSoftware or Agetec. *Armored Core*
and all related assets are the property of their respective owners. Original mod source
is released under the [MIT License](LICENSE); that license covers this repository's own
work only and grants no rights in the underlying game.

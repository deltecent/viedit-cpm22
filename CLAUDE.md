# VIEDIT — altairsim build (driven over MCP)

The "VIEDIT" vi-style CP/M editor, built with M80 + L80 on the **`altairsim`**
Altair 8800 / S-100 simulator, driven **natively over MCP** (JSON-RPC on stdio):
`mcpdrive.py` is the driver, `.toml` files are the machine definitions, and the
disk carries altairsim's hostbridge `R.COM`/`W.COM`/`HDIR.COM` for file exchange.

Use the `altairsim` binary on **PATH** (`which altairsim`), not any copy in the
altairsim source tree. The altairsim sources at `~/src/altairsim` are reference
only.

## HARD RULE: drive altairsim over MCP, never a pty/script

Always drive the simulator through **MCP** — the local `mcpdrive.py`, or
`altairsim <machine> --mcp` JSON-RPC directly. **Do NOT** use `expect`/pty, the
`altairsim -s <script>` (piped-`RUN`) path, or `~/src/altairsim/tools/install-
hostbridge-utils.sh`. A bare monitor `RUN` **blocks on stdin under a pipe** and
hangs (this cost real time once — the install script hung >10 min; the same
install over MCP took ~1 s). This is spelled out in
`~/src/altairsim/docs/DRIVING-WITH-AI.md` — treat that doc as authoritative.
Before any step that spawns a sim or may run more than a few seconds, state the
approach first so it can be corrected at the first wrong move.

## Build and test

```bash
python3 build_vi.py          # build VIEDIT.COM (M80/L80)
python3 build_vi.py KEYTST   # key-decoder test stub
MAX_LIVE=20 python3 smoke_vi.py   # full-screen smoke test (see _MAX_LIVE below)
```

`build_vi.py` produces `VIEDIT.COM` (21120 bytes) and `VIEDIT.SYM` here. The
build is reproducible — two builds differ only in the embedded build-timestamp
(`VIBLD.INC`). `smoke_vi.py` boots VIEDIT on test files, renders the VT100
screen, drives command/insert-mode keys, and checks the screen + save
round-trips. Only the Python standard library is needed (no pexpect).

## How it works

- Sources live flat in this directory (there is no `src/`): the `VI*.MAC`
  modules, `VI.INC`, `VIBLD.INC`, and `VIEDIT.SUB` (CRLF line endings).
- `CPM22-8MB-56K-VIEDIT.DSK` is a **tracked, self-contained** CP/M system disk
  that carries M80/L80, altairsim's hostbridge `R.COM`/`W.COM`/`HDIR.COM`, and
  the current sources. A fresh clone can `altairsim` (bare, see below) →
  `SUBMIT VIEDIT` to compile entirely inside CP/M.
- `build_vi.py` builds **directly on that disk** (no per-build working copy):
  it `R`s the current sources onto it and pulls `VIEDIT.COM`/`.SYM`/`.PRN` back
  off with `W`, so the committed image always holds the current source. If the
  image is ever corrupted, revert it from a previous commit.
- File exchange uses altairsim's **hostbridge** card (`hb0`, port 0xB0, rooted
  at the server's working directory = this dir): `R host cpm` (host→guest),
  `W cpm host [B|T]` (guest→host). The disk image is never edited file-by-file.
- `mcpdrive.py` spawns `altairsim <machine> --mcp` and speaks JSON-RPC, exposing
  `boot`/`cmd`/`rfile`/`wfile`/`send`/`enable_dsr`/`run_until_quiet`/`mem`. Boot
  is `run {from:0xFF00}`; the prompt is `A0>`. `run` returns `stopped:"idle"`
  when the guest parks polling the console, so a settled full-screen redraw is
  detected directly.

## Machines

- `viedit.toml` — used by the **scripts**: a delta on altairsim's built-in
  `default` machine (88-DCDD `dsk0`, hostbridge `hb0`, 56K RAM, DBL PROM at
  FF00). It mounts **no** disk; `mcpdrive` MOUNTs the working image at runtime
  (build: this same disk; smoke: a per-editor copy).
- `altairsim.toml` — the file `altairsim` loads with **no machine argument**, for
  a human at the keyboard: it mounts `CPM22-8MB-56K-VIEDIT.DSK` and boots, so
  `cd` here and run `altairsim` to land at `A0>`.

Under `--mcp` the console is an in-memory terminal: `send` stages a whole key
sequence and one `run` feeds it verbatim, so multi-byte keys and the editor's
`ESC[6n` terminal-size probe (answered `ESC[rows;colsR`) arrive intact — **no
throttle and no WRU remap** (the editor's `^E` reaches the guest).

## Documentation

`README.md` (overview) and `VIEDIT.md` (the end-user manual) describe the editor
itself, not the build tooling. Each `.pdf` is the rendered form of its `.md` and they are
**committed as synced pairs** — regenerate the PDF whenever you edit the Markdown:

```bash
pandoc VIEDIT.md -f gfm -s --embed-resources -c vi.css -o VIEDIT.html
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=VIEDIT.pdf "file://$PWD/VIEDIT.html"
rm VIEDIT.html
```

## Smoke-test notes

`smoke_vi.py` drives `altairsim` over MCP. What that implies:

- **One `altairsim --mcp` process per editor.** A concurrency cap
  (`_MAX_LIVE`, override with `MAX_LIVE=N`) evicts the oldest editor when
  exceeded. The test body's deepest reach-back is **17**, so a cap of **18+**
  gives a fully clean run; the default is **4** to spare the host (long
  reach-back cases then hit an evicted editor and report spurious failures).
- Each concurrent editor needs its **own** disk image (copied from
  `CPM22-8MB-56K-VIEDIT.DSK` into `_smoke_work/`): several sims are alive at
  once and each writes to its disk, so they cannot share one file.
- `diskfile()` brings the editor back to `A0>` (the file was `:w`-saved) and
  reads it out with **W**.
- Settle detection is exact: `run_until_quiet` pumps `run` slices and stops once
  `SETTLE_CONFIRMS` slices in a row draw nothing while the guest is idle — no
  wall-clock quiet window.

## Files

| Path | What |
|------|------|
| `README.md` / `README.pdf` | overview: what VIEDIT is, running it, limitations, build |
| `VIEDIT.md` / `VIEDIT.pdf` | the full end-user manual |
| `vi.css` | print stylesheet for the PDF render |
| `mcpdrive.py` | the MCP driver for altairsim (Python stdlib only) |
| `build_vi.py` | the build driver (over MCP, full speed); targets VIEDIT / KEYTST / SCRTST |
| `smoke_vi.py` | the full-screen smoke test (per-editor `--mcp` sims) |
| `vt100.py` | minimal VT100 screen emulator used by the smoke test |
| `viedit.toml` | scripts' machine (delta on `default`; disk MOUNTed at runtime) |
| `altairsim.toml` | interactive machine (`altairsim` with no args; mounts the VIEDIT disk) |
| `VI*.MAC`, `VI.INC`, `VIBLD.INC` | VIEDIT source modules (`VIBLD.INC` = build stamp) |
| `VIEDIT.SUB` | `SUBMIT VIEDIT` build script (also rides onto the disk for in-CP/M builds) |
| `CPM22-8MB-56K-VIEDIT.DSK` | tracked CP/M disk: M80/L80 + hostbridge R/W/HDIR + current sources |

The `*.REL`, `*.PRN`, and `VIEDIT.COM`/`.SYM` (and KEYTST/SCRTST) outputs plus the
smoke test's `_smoke_work/` scratch are regenerated each run.

## Status

- **Build:** reproducible (VIEDIT.COM = 21120 bytes; KEYTST also builds).
  `SCRTST` is a stale Phase-2 stub: its `VISCREEN` references `MKDEL`/`MKINS`,
  which now exist only in the full VIEDIT link, so it fails to link.
- **Smoke test:** 357 passed / 0 failed at `MAX_LIVE=20`.

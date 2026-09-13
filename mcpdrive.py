"""mcpdrive -- drive the ``altairsim`` simulator through its MCP interface.

Spawns ``altairsim <machine> --mcp`` and talks to it over **JSON-RPC 2.0 on
stdio** (the protocol documented in altairsim's ``docs/manual/mcp.md`` /
``DRIVING-WITH-AI.md``).  ``build_vi.py`` and ``smoke_vi.py`` drive the guest
through this class and hand it a ``.toml`` machine file.

Why MCP:

  * one ``tools/call`` in, one structured result out -- no prompt races, no
    interleaved echo, no WRU escape character to remap.  The editor's ^E reaches
    the guest untouched because under ``--mcp`` the console is an in-memory
    terminal the driver reads and writes with ``send`` / ``run`` / ``recv``.
  * ``run`` returns on its own the moment the guest idles polling the console
    (``stopped == "idle"``), so a settled full-screen redraw is detected
    directly instead of waiting out a wall-clock quiet window.
  * files move with the **R** / **W** verbs through altairsim's ``hostbridge``
    card (port 0xB0, rooted at the server's working directory).  The disk must
    carry altairsim's ``R.COM`` / ``W.COM`` / ``HDIR.COM`` (see
    ``CPM22-8MB-56K-VIEDIT.DSK``).

Requires only the Python standard library and the ``altairsim`` binary on
``PATH`` (or ``$ALTAIRSIM`` / ``sim=``).
"""

import json
import os
import re
import shutil
import subprocess
import time

# altairsim's DBL boot PROM lives at 0xFF00; booting a disk machine is RUN FF00.
BOOT_PC = 0xFF00

# The CP/M ready prompt for these disks is "A0>" (drive A, user 0).  Kept as a
# regex so callers can pass it straight to ``expect``; it also matches other
# drive/user prompts (B>, C15>, ...).
DEFAULT_PROMPT = r"[A-P][0-9]*>"


def default_sim():
    """Resolve the altairsim binary: ``$ALTAIRSIM``, then ``altairsim`` on
    ``PATH``, else the bare name (so the error message names it)."""
    return os.environ.get("ALTAIRSIM") or shutil.which("altairsim") or "altairsim"


def crlf(text):
    """Normalize to CR/LF line endings -- what CP/M tools (M80, ASM, ED) expect.
    Host editors write LF-only; feeding that to ASM/M80 misbehaves.  Accepts str
    or bytes; returns the same type."""
    if isinstance(text, (bytes, bytearray)):
        return bytes(text).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


class SimError(RuntimeError):
    """Raised when the simulator does something unexpected (RPC error, timeout)."""


class AltairSim:
    """A running ``altairsim`` MCP server under this driver's control.

    Typical use::

        with AltairSim("viedit.toml", cwd=HERE, disk="CPM22-8MB-56K-VIEDIT.DSK") as sim:
            sim.boot()                       # RUN FF00, wait for A0>
            sim.rfile("hello.mac")           # host -> CP/M via R (hostbridge)
            print(sim.cmd("M80 HELLO,HELLO=HELLO"))
            sim.wfile("HELLO.PRN", "T")      # CP/M -> host via W
    """

    def __init__(
        self,
        machine,
        *,
        sim=None,
        cwd=None,
        disk=None,
        drive="dsk0:drive0",
        prompt=DEFAULT_PROMPT,
        timeout=60,
        logfile=None,
        boot_pc=BOOT_PC,
        slice_ms=3000,
        **_ignored,          # swallow kwargs that don't apply under --mcp (e.g. wru)
    ):
        """Spawn ``altairsim <machine> --mcp``.

        machine  -- built-in name or ``.toml`` machine file (altairsim's sole
                    positional arg).  Relative to *cwd*.
        sim      -- path to the altairsim binary (default: ``$ALTAIRSIM`` or the
                    one found on ``PATH``; see ``default_sim``).
        cwd      -- working directory the server runs in; the hostbridge sandbox
                    (R/W) and any relative machine/disk paths resolve here, so
                    point it at your project dir.  Defaults to os.getcwd().
        disk     -- optional disk image to MOUNT on *drive* after spawn (before
                    boot).  Lets one machine file serve many per-run images.
        prompt   -- regex for the CP/M ready prompt (see DEFAULT_PROMPT).
        timeout  -- default seconds to wait in expect/boot/cmd calls.
        logfile  -- optional writable text stream; every byte the guest prints
                    is teed to it (the smoke test renders it as a VT100 screen).
        slice_ms -- per-``run`` wall-clock budget (ms).  Bounds how long a single
                    advance waits; a long CPU-bound build just takes more slices.
        """
        sim = sim or default_sim()
        if not os.path.isabs(sim) and os.sep not in sim:
            sim = shutil.which(sim) or sim
        if not os.access(sim, os.X_OK):
            raise SimError(
                f"altairsim binary not found or not executable: {sim!r}. "
                "Put it on PATH, set ALTAIRSIM=/path/to/altairsim, or pass sim=...")

        self.cwd = cwd or os.getcwd()
        self.prompt = prompt
        self.timeout = timeout
        self.logfile = logfile
        self.boot_pc = boot_pc
        self.slice_ms = slice_ms
        self._id = 0
        self._dsr = None          # (rows, cols) to answer DSR queries, or None
        self.last_output = ""     # console text from the most recent call

        self.proc = subprocess.Popen(
            [sim, machine, "--mcp"], cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcpdrive", "version": "1.0"},
        })
        self._rpc("notifications/initialized", notify=True)

        if disk is not None:
            # Mount the working image on the controller the default machine
            # already fits (dsk0), then boot from the PROM below.
            self.monitor(f'MOUNT {drive} "{disk}"')

    # ---------------------------------------------------------------- JSON-RPC

    def _rpc(self, method, params=None, notify=False):
        if self.proc is None or self.proc.poll() is not None:
            raise SimError("altairsim process is not running")
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._id
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise SimError("altairsim closed its input (process gone?)")
        if notify:
            return None
        # Read lines until the response with our id arrives; skip any
        # server-initiated notifications (no matching id).
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise SimError("altairsim exited before answering "
                               f"{method!r}")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise SimError(f"{method} error: {resp['error']}")
                return resp.get("result", {})

    def _tool(self, name, args=None):
        """Call one MCP tool; return its ``structuredContent`` (or ``result``)."""
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        return res.get("structuredContent", res)

    def monitor(self, command):
        """Run one monitor command and return its text (the escape hatch)."""
        res = self._tool("monitor", {"command": command})
        if isinstance(res, dict):
            return res.get("output", res.get("text", ""))
        return str(res)

    # ------------------------------------------------------------------- guest

    def _tee(self, text):
        if text:
            if self.logfile is not None:
                self.logfile.write(text)
            self.last_output += text

    def _run(self, input=None, from_pc=None, until=None, timeout_ms=None):
        args = {"timeout_ms": timeout_ms or self.slice_ms}
        if input is not None:
            args["input"] = input
        if from_pc is not None:
            args["from"] = from_pc
        if until is not None:
            args["until"] = until
        r = self._tool("run", args)
        self._tee(r.get("output", ""))
        return r

    def boot(self, prompt=None, timeout=None):
        """Boot the machine from the PROM (RUN FF00) and wait for the ready
        prompt.  Returns the boot banner text; leaves the guest at the console."""
        pat = prompt or self.prompt
        self.last_output = ""
        deadline = time.time() + (timeout or self.timeout)
        r = self._run(from_pc=self.boot_pc)
        while True:
            if re.search(pat, self.last_output):
                return self.last_output
            if time.time() > deadline:
                raise SimError("timeout waiting for boot prompt "
                               f"{pat!r}\n--- recent ---\n{self.last_output[-1500:]}")
            r = self._run()

    def send(self, text):
        """Type raw text at the guest console (no automatic CR).  Staged for the
        next ``run``/``expect``/``run_until_quiet`` -- altairsim's ``send`` does
        not advance the guest."""
        self._tool("send", {"text": text})

    def recv(self):
        """Drain and return whatever the guest has printed since the last read,
        without running it."""
        r = self._tool("recv")
        out = r.get("output", "") if isinstance(r, dict) else str(r)
        self._tee(out)
        return out

    def expect(self, pattern=None, timeout=None):
        """Advance the guest until *pattern* (regex) appears in its output.

        Returns the accumulated output up to the match; raises SimError on
        timeout.  Whatever was staged with ``send`` is fed as the guest runs."""
        pat = pattern or self.prompt
        self.last_output = ""
        deadline = time.time() + (timeout or self.timeout)
        while True:
            self._run()
            if re.search(pat, self.last_output):
                return self.last_output
            if time.time() > deadline:
                raise SimError(f"timeout waiting for {pat!r}\n"
                               f"--- recent ---\n{self.last_output[-1500:]}")

    def send_expect(self, text, pattern=None, timeout=None):
        """Type *text*, then advance until *pattern* (default: prompt)."""
        self.last_output = ""
        return self._expect_after(text, pattern, timeout)

    def _expect_after(self, input_text, pattern=None, timeout=None):
        pat = pattern or self.prompt
        self.last_output = ""
        deadline = time.time() + (timeout or self.timeout)
        r = self._run(input=input_text)
        while True:
            if re.search(pat, self.last_output):
                return self.last_output
            if time.time() > deadline:
                raise SimError(f"timeout waiting for {pat!r}\n"
                               f"--- recent ---\n{self.last_output[-1500:]}")
            r = self._run()

    def cmd(self, line, prompt=None, timeout=None):
        """Type a CP/M command line + CR, wait for the ready prompt, return the
        output the command produced."""
        return self._expect_after(line + "\r", prompt, timeout)

    # ------------------------------------------------------------- full-screen

    def enable_dsr(self, rows, cols):
        """Answer the guest's ESC[6n terminal-size probe as a (rows x cols)
        VT100.  Full-screen programs home the cursor to ESC[999;999H then ask
        where it landed (ESC[6n); ``run_until_quiet`` replies ESC[rows;colsR."""
        self._dsr = (rows, cols)

    def _answer_dsr(self, data):
        """Reply ESC[rows;colsR to each ESC[6n seen in *data*."""
        if self._dsr is None:
            return False
        n = data.count("\x1b[6n")
        for _ in range(n):
            rows, cols = self._dsr
            self.send("\x1b[%d;%dR" % (rows, cols))
        return n > 0

    #: consecutive drew-nothing slices required before the screen is called
    #: settled.  A full-screen redraw is not atomic -- the editor polls the
    #: keyboard between phases (e.g. after ESC it idles, then erases the
    #: "-- INSERT --" status line), so the FIRST idle is premature.  Requiring a
    #: few quiet slices in a row lets those deferred draws land.  Override with
    #: SETTLE_CONFIRMS in the environment.
    SETTLE_CONFIRMS = int(os.environ.get("SETTLE_CONFIRMS", "4"))

    def run_until_quiet(self, quiet=0.3, timeout=30):
        """Advance the guest until the screen settles.

        A full-screen program never returns to A0>, so there is no prompt to
        match.  Instead we pump ``run`` slices: each returns what the guest drew
        and stops when it idles polling the keyboard.  An ESC[6n probe is
        answered mid-stream.  The screen is *settled* once SETTLE_CONFIRMS
        slices in a row draw nothing (a single idle slice can fall between two
        phases of one redraw).  Returns the new output.

        *quiet* is accepted but ignored -- idle detection is exact, not a
        wall-clock window.  *timeout* bounds the whole wait."""
        self.last_output = ""
        deadline = time.time() + timeout
        idle_run = 0
        while time.time() < deadline:
            before = len(self.last_output)
            r = self._run()
            drew = len(self.last_output) - before
            if self._dsr is not None and self._answer_dsr(r.get("output", "")):
                idle_run = 0
                continue                       # answered a probe; let it redraw
            if drew == 0 and r.get("stopped") in ("idle", "timeout", "halt",
                                                  "breakpoint"):
                idle_run += 1
                if idle_run >= self.SETTLE_CONFIRMS:
                    break                      # quiet for long enough -> settled
            else:
                idle_run = 0                   # drew something; keep watching
        return self.last_output

    # ----------------------------------------------------------- file transfer

    def put(self, name, text, convert=True):
        """Write a host file into the server's cwd, ready to ``rfile`` onto CP/M.
        CRLF-normalized by default (``convert=False`` writes raw bytes)."""
        data = crlf(text) if convert else text
        mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
        path = os.path.join(self.cwd, name)
        with open(path, mode) as f:
            f.write(data)
        return path

    def rfile(self, spec, lower=False, timeout=None):
        """READ a host file into CP/M via the R utility (host -> guest).
        *spec* is whatever R accepts (``hello.mac``, ``src/*.mac``, ...).
        ``lower=True`` keeps lower-case names.  Raises SimError if R reports it
        could not open the file."""
        line = "R " + spec + (" L" if lower else "")
        out = self.cmd(line, timeout=timeout)
        low = out.lower()
        if "annot" in out or "rror" in out or "not found" in low:
            raise SimError(f"R failed for {spec!r}:\n{out}")
        return out

    def wfile(self, spec, mode=None, timeout=None):
        """WRITE a CP/M file out to the host via the W utility (guest -> host).
        *mode* is ``"B"`` (binary) or ``"T"`` (text); omit to let W default
        (``.COM/.REL`` imply binary).  Raises SimError on failure."""
        line = "W " + spec + (" " + mode if mode else "")
        out = self.cmd(line, timeout=timeout)
        low = out.lower()
        if "annot" in out or "not found" in low or "rror" in out:
            raise SimError(f"W failed for {spec!r}:\n{out}")
        return out

    # ----------------------------------------------------------- introspection

    def mem(self, addr, n=1):
        """Read *n* bytes of guest memory starting at *addr*; returns a
        bytearray.  A non-invasive peek (``mem_dump``) -- it does not stop the
        guest."""
        r = self._tool("mem_dump", {"lo": addr, "hi": addr + n - 1})
        by = r.get("bytes", []) if isinstance(r, dict) else []
        return bytearray((by + [0] * n)[:n])

    def regs(self):
        """Return the 8080 registers now, as a flag/register dict."""
        r = self._tool("regs")
        reg = r.get("registers", {}) if isinstance(r, dict) else {}
        return {
            "C": reg.get("CY", 0), "Z": reg.get("Z", 0), "M": reg.get("S", 0),
            "E": 0, "I": reg.get("IE", 0),
            "A": reg.get("A", 0), "B": reg.get("BC", 0), "D": reg.get("DE", 0),
            "H": reg.get("HL", 0),
            "SP": reg.get("SP", r.get("pc", 0) if isinstance(r, dict) else 0),
            "PC": reg.get("PC", r.get("pc", 0) if isinstance(r, dict) else 0),
        }

    # ---------------------------------------------------------------- lifecycle

    def quit(self):
        """Quit the simulator cleanly (best effort), then terminate it."""
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.monitor("QUIT")
        except Exception:
            pass
        self.close()

    def close(self):
        """Force-terminate the simulator process (idempotent)."""
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
            for s in (self.proc.stdin, self.proc.stdout):
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.quit()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

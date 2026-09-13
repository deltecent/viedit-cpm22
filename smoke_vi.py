#!/usr/bin/env python3
"""Smoke test for VIEDIT.COM on the altairsim simulator (MCP driver).

Boots VIEDIT on a small file, renders the VT100 screen, drives command-mode
keys, and checks the screen + save round-trips -- driven through the simulator
over MCP.

Build first:  python3 build_vi.py
Then run:     python3 smoke_vi.py

Two driver capabilities make full-screen driving work (see mcpdrive.py):
  * enable_dsr() answers the editor's ESC[6n terminal-size probe
  * run_until_quiet() pumps `run` slices until the guest idles polling the
    keyboard (the screen has settled)
Under --mcp the console is an in-memory terminal: `send` stages a whole key
sequence and one `run` feeds it verbatim, so multi-byte keys and the DSR reply
arrive intact with no throttle and no WRU remap (the editor's ^E reaches it).
"""
import os
import sys
import io
import re
import shutil
import atexit
import itertools

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcpdrive import AltairSim
from vt100 import VT100

MACHINE = "viedit.toml"                             # altairsim machine (delta on `default`)
TEMPLATE = os.path.join(HERE, "CPM22-8MB-56K-VIEDIT.DSK")

PASS = [0]; FAIL = [0]
def check(label, cond):
    if cond: PASS[0] += 1
    else: FAIL[0] += 1; print(f'  FAIL {label}')


# Symbol table (NAME -> address) parsed from VIEDIT.SYM so tests can read
# editor globals (e.g. NROWS/NCOLS) straight out of guest memory.  The .SYM is a
# whitespace-separated stream of "AAAA NAME" pairs.
SYM = {}
_toks = open(os.path.join(HERE, "VIEDIT.SYM")).read().replace('\t', ' ').split()
for _i in range(0, len(_toks) - 1, 2):
    try:
        SYM[_toks[_i + 1]] = int(_toks[_i], 16)
    except ValueError:
        pass


def _quiet_for(idle):
    """Map a call site's step-count `idle` hint onto a settle window (seconds).
    Vestigial: run_until_quiet now detects idle exactly and ignores the window,
    so this only survives to keep the call sites' `idle=` hints meaningful."""
    return min(5.0, max(0.3, idle / 6000.0))


# Each Editor runs its own altairsim process on its own disk image, so several
# editors can be alive at once -- the test body freely interleaves them (it keeps
# an editor in a named local and reuses it after creating others).  Each
# simulator is a heavyweight OS process, so we cap how many run concurrently:
# new Editors are appended to _LIVE and, once the cap is exceeded, the OLDEST is
# quit and its scratch disk removed.
#
# NOTE ON THE CAP: the body's deepest reach-back -- an editor created, then
# reused N creations later while N others are made in between -- is 17.  A cap
# strictly above that (e.g. 18-24) guarantees no editor is evicted before its
# last use (verified: 322 passed / 3 failed at 24 -- the 3 are pre-existing).
# _MAX_LIVE is set to 4 here to keep the host from being starved by ~20 live
# simulators; at that setting the long-reach-back cases hit an evicted (quit)
# editor and report spurious failures.  Raise it toward ~18-24 for a clean full
# run; lower it to spare the machine.  WORK holds the per-instance scratch disks.
WORK = os.path.join(HERE, "_smoke_work")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)
_ids = itertools.count()
_LIVE = []
# Override with MAX_LIVE=N in the environment.  The body's deepest reach-back is
# 17, so a cap of 18+ guarantees a clean run; the low default spares the host.
_MAX_LIVE = int(os.environ.get("MAX_LIVE", "4"))


@atexit.register
def _cleanup_all():
    for ed in list(_LIVE):
        ed.close()
    shutil.rmtree(WORK, ignore_errors=True)


class Editor:
    """Boot a fresh disk, launch VIEDIT on a test file, drive it.

    Each Editor owns an independent simulator (its own disk image), so the test
    can keep several alive at once.  The simulator is quit and its scratch files
    removed when the Editor is closed or garbage-collected.
    """

    def __init__(self, content, args='', cfg=None, extra=None, fname='TEST.TXT',
                 term=(24, 80), idle=3000):
        # term=(rows, cols): emulate a VT100 that answers the ESC[6n DSR query
        # with that size (the editor auto-detects when rows/cols are unset).
        # term=None: a terminal that never replies (the editor must time out).
        self.s = None
        n = next(_ids)
        # Each concurrent editor gets its OWN disk image: several altairsim
        # processes are alive at once (see _MAX_LIVE) and each writes to its
        # disk (saves, VIB/VIA.$$$ scratch), so they cannot share one file.
        # Copied from the tracked CP/M disk, which carries the hostbridge R/W
        # utilities the harness needs.  The disk is mounted by mcpdrive at
        # runtime, relative to cwd=HERE (the simulator's working directory).
        self.disk = os.path.join(WORK, f"disk{n}.DSK")
        shutil.copy(TEMPLATE, self.disk)
        self.cap = io.StringIO()
        self.quiet = _quiet_for(idle)
        self.s = AltairSim(MACHINE, cwd=HERE, disk=os.path.relpath(self.disk, HERE),
                           logfile=self.cap, timeout=30)
        _LIVE.append(self)
        while len(_LIVE) > _MAX_LIVE:
            _LIVE.pop(0).close()
        try:
            self.s.boot()
            self.s.rfile("VIEDIT.COM")               # binary R
            # Stage the test file(s) onto the CP/M disk: write to the host cwd,
            # then R them onto the guest disk (host -> guest).
            if content is not None:
                self.s.put("TEST.TXT", content, convert=False)
                self.s.rfile("TEST.TXT")
            if cfg is not None:
                self.s.put("VIEDIT.CFG", cfg, convert=False)
                self.s.rfile("VIEDIT.CFG")
            for name, data in (extra or {}).items():
                self.s.put(name, data, convert=False)
                self.s.rfile(name)
            if term is not None:
                self.s.enable_dsr(term[0], term[1])
            self.cap.seek(0); self.cap.truncate(0)   # screen = VIEDIT only
            # fname=None boots VIEDIT with no filename (an unnamed, empty buffer).
            cmd = 'VIEDIT' + (' ' + fname if fname else '') + args
            self.s.send(cmd + '\r')
            self.s.run_until_quiet(quiet=self.quiet, timeout=40)
        except BaseException:
            self.close()
            raise

    def close(self):
        """Quit the simulator and remove its scratch disk (idempotent)."""
        try:
            _LIVE.remove(self)
        except ValueError:
            pass
        s, self.s = self.s, None
        if s is not None:
            try:
                s.quit()
            except Exception:
                pass
        p = getattr(self, "disk", None)
        if p:
            try:
                os.remove(p)
            except OSError:
                pass

    def __del__(self):
        self.close()

    def geom(self):
        """(NROWS, NCOLS) as the running editor computed them.  Reads guest
        memory, which escapes to sim> and stops the editor -- only call on an
        Editor that is about to be discarded."""
        return (self.s.mem(SYM['NROWS'])[0], self.s.mem(SYM['NCOLS'])[0])

    def key(self, text, idle=2000, steps=None):
        self.s.send(text)
        self.s.run_until_quiet(quiet=_quiet_for(idle), timeout=40)

    def mark(self):
        return len(self.cap.getvalue())

    def since(self, m):
        return self.cap.getvalue()[m:]

    def screen(self, rows=24, cols=80):
        v = VT100(rows, cols)
        v.feed(self.cap.getvalue())
        return v

    def _ensure_ccp(self):
        """Bring the guest back to the CCP A0> prompt so the W utility can run.

        The W utility runs at the CP/M console, so the editor must be back at
        A0> before a file can be read out.  The test always :w-saves before
        reading and never edits an Editor again after diskfile(), so
        force-quitting here is safe.  A bare CR reprints
        the prompt if we are already at A0> (the editor had quit via :q/:wq/^X);
        otherwise the editor is still up and gets a force-quit (file already
        saved by the preceding :w)."""
        self.s.send("\r")
        try:
            self.s.expect(self.s.prompt, timeout=3)
            return                     # already at A0>
        except Exception:
            pass
        self.s.send("\x1b\x1b:q!\r")   # ESC out of any mode, then force-quit
        self.s.expect(self.s.prompt, timeout=20)

    def diskfile(self, name, ext):
        """W the file out to the host and return its raw bytes (the caller
        strips the CP/M ^Z EOF padding)."""
        self._ensure_ccp()
        self.s.wfile(f"{name}.{ext}", "T")
        with open(os.path.join(HERE, f"{name}.{ext}"), "rb") as f:
            return f.read()

    def listed(self):
        """Set of (NAME, EXT) on the CP/M disk via DIR (brings the guest to the
        A0> prompt first if the editor is somehow still up)."""
        self._ensure_ccp()
        out = self.s.cmd("DIR")
        names = set()
        for m in re.finditer(r"([A-Z0-9$]{1,8})\s+([A-Z0-9$]{1,3})", out):
            names.add((m.group(1), m.group(2)))
        return names


def main():
    content = (b'first line\r\nsecond line\r\nthird line\r\n'
               b'fourth line\r\nfifth line\r\n')
    e = Editor(content)
    v = e.screen()
    r = v.render()
    check('renders first line', 'first line' in r)
    check('renders fifth line', 'fifth line' in r)
    check('cursor at home (1,1)', (v.row, v.col) == (0, 0))

    # navigate: j j l l  -> row 3, col 3 (0-based row 2, col 2)
    e.key('jjll')
    v = e.screen()
    check('after jjll cursor row 3', v.row == 2)
    check('after jjll cursor col 3', v.col == 2)

    # x deletes char under cursor on line 3 ("third" -> "thrd")
    e.key('x')
    v = e.screen()
    check('x deleted a char', 'thrd line' in v.render())

    # help overlay: K (and :h) paint a command summary; any key restores the
    # editor; the startup status line advertises the K=HELP hint.
    eh = Editor(b'alpha\r\nbeta\r\ngamma\r\n')
    check('startup status hints "Press K for help"',
          'Press K for help' in eh.screen().render().split('\n')[23])
    eh.key('K')
    rh = eh.screen().render()
    check('K opens help (Command Summary)', 'Command Summary' in rh)
    check('help lists MOVE section', 'MOVE' in rh)
    check('help lists EXIT section', 'EXIT' in rh)
    eh.key(' ')                          # any key dismisses
    rh = eh.screen().render()
    check('help dismissed: editor text back', 'alpha' in rh)
    check('help dismissed: summary gone', 'Command Summary' not in rh)
    eh.key(':h\r')                       # ex :h also opens help
    check(':h opens help', 'Command Summary' in eh.screen().render())
    eh.key(' ')                          # dismiss before continuing

    # insert-mode line wrap: typing past column 80 horizontally scrolls the
    # edited line; it must NOT displace the line below (old corruption bug).
    ew = Editor(b'short one\r\nshort two\r\n')
    ew.key('A')             # append at end of line 1
    ew.key('x' * 90)        # grow well past col 80 -> AUTOSCR kicks in
    ew.key('\x1b')
    vw = ew.screen()
    rows = vw.render().split('\n')
    check('wrap: cursor stays on edited line (row 0)', vw.row == 0)
    check('wrap: edited line scrolled (row 0 is x-run)', set(rows[0].strip()) == {'x'})
    check('wrap: line 2 not displaced to row 2', 'short two' not in rows[2])
    check('wrap: no overflow x spilled onto row 1', 'x' not in rows[1])

    # x feeds the unnamed register: x then p re-inserts the deleted char.
    # On "first line": x deletes 'f' (cursor -> 'i'); p puts 'f' after -> "ifrst line"
    ex = Editor(content)
    ex.key('xp')
    check('x then p re-inserts deleted char', 'ifrst line' in ex.screen().render())

    # open a line below and type, then ESC
    e.key('o')
    e.key('NEW ROW')
    e.key('\x1b')
    v = e.screen()
    check('o inserted new line', 'NEW ROW' in v.render())

    # save and quit, then verify the file on disk
    e.key(':w\r')
    e.key(':q\r', idle=3000)
    out = e.diskfile('TEST', 'TXT')
    z = out.find(b'\x1a')
    if z >= 0: out = out[:z]
    check('saved file has edit (thrd)', b'thrd line' in out)
    check('saved file has new row', b'NEW ROW' in out)
    check('saved file keeps line 1', out.startswith(b'first line\r\n'))

    # --- :w filename / :r filename (write-to / read-from a named file) ---
    def _lines(ed):
        return [r.rstrip() for r in ed.screen().render().split('\n')]
    e = Editor(b'hello\r\nworld\r\n')
    e.key(':w OUT.TXT\r')
    check(':w filename reports vi-style status',
          '"OUT.TXT" 2L, 14B written' in e.screen().render())
    check(':w filename wrote the named file',
          bytes(e.diskfile('OUT', 'TXT')).rstrip(b'\x1a') == b'hello\r\nworld\r\n')
    check(':w filename leaves the current file untouched',
          bytes(e.diskfile('TEST', 'TXT')).rstrip(b'\x1a') == b'hello\r\nworld\r\n')
    e = Editor(b'one\r\ntwo\r\nthree\r\n', extra={'INS.TXT': b'AAA\r\nBBB\r\n'})
    e.key(':r INS.TXT\r')               # cursor on line 1 -> read in below it
    rl = _lines(e)
    check(':r inserts below the current line',
          rl[:4] == ['one', 'AAA', 'BBB', 'two'])
    e.key(':w\r')
    check(':r round-trips through a save',
          bytes(e.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
          == b'one\r\nAAA\r\nBBB\r\ntwo\r\nthree\r\n')
    e = Editor(b'x\r\n')
    e.key(':r NOPE.TXT\r')
    check(':r missing file -> "File not found"', 'File not found' in e.screen().render())
    e = Editor(b'x\r\n')
    e.key(':r\r')
    check(':r with no name -> "No file name"', 'No file name' in e.screen().render())

    # --- :r into an UNNAMED buffer must not leave it 'modified' (so :q quits) ---
    # vi started with no file; reading a file in is not an edit, so :q must not
    # warn "No write since last change".
    e = Editor(b'', fname=None, extra={'DUMP.ASM': b'aaa\r\nbbb\r\n'})
    e.key(':r DUMP.ASM\r')
    # empty buffer is one blank line; :r inserts the file below it
    check(':r into unnamed buffer reads the file', _lines(e)[:3] == ['', 'aaa', 'bbb'])
    m = e.mark(); e.key(':q\r', idle=3000); qout = e.since(m)
    check(':q after :r on unnamed buffer quits (no "No write" warning)',
          'No write since last change' not in qout and 'A0>' in qout)
    # protective case 1: :r into a NAMED buffer still marks modified -> :q warns
    e = Editor(b'one\r\ntwo\r\n', extra={'INS.TXT': b'AAA\r\n'})
    e.key(':r INS.TXT\r')
    e.key(':q\r', idle=3000)
    check(':r into a named buffer still warns on :q',
          'No write since last change' in e.screen().render())
    # protective case 2: an unnamed buffer with real edits before :r still warns
    e = Editor(b'', fname=None, extra={'DUMP.ASM': b'aaa\r\n'})
    e.key('i'); e.key('typed'); e.key('\x1b')   # a real edit -> modified
    e.key(':r DUMP.ASM\r')
    e.key(':q\r', idle=3000)
    check('unnamed buffer with edits still warns on :q after :r',
          'No write since last change' in e.screen().render())

    # --- save shows a vi-style 'name NL, NB written' line and clears 'modified' ---
    e = Editor(b'one\r\ntwo\r\nthree\r\n')
    e.key('o'); e.key('hi'); e.key('\x1b')   # modify -> 4 lines
    m = e.mark(); e.key(':w\r'); wout = e.since(m)
    check(':w shows the vi-style written line',
          '"TEST.TXT" 4L,' in e.screen().render() and 'written' in e.screen().render())
    check(':w emits "writing" before "written"', 'writing' in wout)
    # the modified flag is now clear, so a plain :e (no !) is allowed
    e = Editor(b'aaa\r\nbbb\r\n', extra={'OTHER.TXT': b'1\r\n2\r\n3\r\n'})
    e.key(':e OTHER.TXT\r')
    check(':e loads the new file', _lines(e)[:3] == ['1', '2', '3'])
    check(':e shows the new name + counts', '"OTHER.TXT" 3L,' in e.screen().render())
    # :e refuses to abandon unsaved changes; :e! forces it
    e = Editor(b'keep\r\n', extra={'OTHER.TXT': b'NEW\r\n'})
    e.key('o'); e.key('X'); e.key('\x1b')    # modify
    e.key(':e OTHER.TXT\r')
    check(':e refuses with unsaved changes',
          'No write since last change' in e.screen().render()
          and 'keep' in '\n'.join(_lines(e)))
    e.key(':e! OTHER.TXT\r')
    check(':e! forces the load', _lines(e)[0] == 'NEW')
    # :e on a file too large to hold resident is refused, current buffer intact
    big = ('\r\n'.join('line %04d filler text to add some bulk' % i
                       for i in range(900)) + '\r\n').encode()
    e = Editor(b'keep me\r\nsecond line\r\n', extra={'BIG.TXT': big})
    e.key(':e BIG.TXT\r')
    check(':e too-large is refused', 'File too large' in e.screen().render())
    check(':e too-large keeps the current buffer', _lines(e)[0] == 'keep me')

    # --- :e with no filename: ^G status if a file is loaded, else "No file name"
    e = Editor(b'first line\r\nsecond\r\n')
    e.key(':e\r')
    check(':e (no name) shows ^G status for a loaded file',
          '"TEST.TXT" line 1 of 2 col 1' in e.screen().render())
    e = Editor(b'', fname=None)
    e.key(':e\r')
    check(':e (no name) shows "No file name" when unnamed',
          'No file name' in e.screen().render())

    # --- normal >> / << (count-aware) : line indent ---
    def _file(ed):
        return bytes(ed.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
    e = Editor(b'one\r\ntwo\r\n')
    e.key('>>'); e.key(':w\r')
    check('>> inserts a leading tab', _file(e) == b'\tone\r\ntwo\r\n')
    # _file() force-quits the editor (diskfile -> _ensure_ccp), so we cannot keep
    # editing this same `e`.  Verify the >> then << round-trip in its own editor.
    e = Editor(b'one\r\ntwo\r\n')
    e.key('>>'); e.key('<<'); e.key(':w\r')
    check('<< removes the leading tab', _file(e) == b'one\r\ntwo\r\n')
    e = Editor(b'a\r\nb\r\nc\r\n')
    e.key('2>>'); e.key(':w\r')
    check('2>> shifts two lines', _file(e) == b'\ta\r\n\tb\r\nc\r\n')
    e = Editor(b'        x\r\n')          # 8 leading spaces
    e.key('<<'); e.key(':w\r')
    check('<< removes up to 8 leading spaces', _file(e) == b'x\r\n')

    # --- shifts are undoable (whole-region snapshot) ---
    e = Editor(b'a\r\nb\r\nc\r\n')
    e.key('3>>'); e.key('u'); e.key(':w\r')
    check('3>> + u restores all lines', _file(e) == b'a\r\nb\r\nc\r\n')

    # --- command coverage for the four reported bugs (fresh editor) ---
    e = Editor(content)            # line 1 = "first line" (cols 0..9)
    # Bug 3: $ lands ON the last char (col 9), not one past
    e.key('$')
    v = e.screen()
    check('$ on last char (col 9)', v.col == 9)
    # Bug 1: w advances to the next word ("first" -> "line" at col 6)
    e.key('0')
    e.key('w')
    v = e.screen()
    check('w advances to next word (col 6)', v.col == 6)
    # Bug 2: ^G shows file + line status on the status row
    e.key('0')
    e.key('\x07')
    v = e.screen()
    check('^G shows file/line status', '"TEST.TXT" line 1' in v.render())
    check('^G omits [readonly] when writable', '[readonly]' not in v.render())
    # ^G shows [readonly] for a /R file
    e2 = Editor(content, args=' /R'); e2.key('\x07')
    check('^G shows [readonly] with /R',
          '"TEST.TXT" [readonly] line 1' in e2.screen().render())

    # ^G is vi-style: "NAME" [readonly] line N of M col C (GBNLNS total; the
    # count cache invalidates on edits via SETMOD clearing NLNVAL).
    def gstat(ed):
        sv = ed.screen()
        return ''.join(sv.screen[sv.rows - 1]).rstrip()
    eg = Editor(content)            # 5 lines
    eg.key('j'); eg.key('\x07')
    check('^G: line N of M col C', 'line 2 of 5 col 1' in gstat(eg))
    eg.key('o'); eg.key('z'); eg.key('\x1b'); eg.key('\x07')
    check('^G total recounts after add (6)', 'of 6 col' in gstat(eg))
    eg.key('dd'); eg.key('\x07')
    check('^G total recounts after delete (5)', 'of 5 col' in gstat(eg))
    # unterminated last line is counted; empty file is one line
    en = Editor(b'aaa\r\nbbb\r\nccc'); en.key('G'); en.key('\x07')
    check('^G counts no-final-newline line', 'line 3 of 3 col' in gstat(en))
    ee = Editor(b''); ee.key('\x07')
    check('^G of empty file is 1 of 1', 'line 1 of 1 col 1' in gstat(ee))
    # :stat diagnostics on the status row (RAM-only set: build stamp + buffer)
    e.key('gg')
    e.key(':stat\r')
    r = e.screen().render()
    check(':stat shows build timestamp', 'built ' in r)
    check(':stat shows buf= byte count', 'buf=' in r)
    check(':stat shows free= memory', 'free=' in r)
    check(':stat shows lines= total', 'lines=' in r)
    # Bug 5: TABs expand to tab stops in rendering + cursor column math
    et = Editor(b'a\tb\tcc\r\nsecond line\r\n')
    vt = et.screen()
    check('tab renders to next stop (col 8)',
          vt.render().splitlines()[0].startswith('a       b       cc'))
    check('tab cursor at home col 0', vt.col == 0)
    et.key('l')                       # onto the tab: renders at the tab's last cell
    check('cursor on tab renders at the tab end (col 7)', et.screen().col == 7)
    et.key('l')                       # onto 'b': sits at the tab stop
    check('cursor past tab at col 8', et.screen().col == 8)

    # newbugs: insert/overwrite incremental redraw (VT100 insert/overwrite-char)
    ei = Editor(b'hello world\r\nsecond\r\n')
    ei.key('0ll'); ei.key('i')        # mid-line insert at col 2
    m = ei.mark(); ei.key('X')
    out = ei.since(m)
    check('insert uses ESC[@ (no full-line erase)', '\x1b[1@' in out and '\x1b[K' not in out)
    check('insert result correct', ei.screen().render().splitlines()[0].startswith('heXllo world'))
    eo = Editor(b'abcde\r\n'); eo.key('0R')   # overwrite mode
    m = eo.mark(); eo.key('X')
    check('overwrite emits no ESC[@', '\x1b[1@' not in eo.since(m))
    check('overwrite result correct', eo.screen().render().splitlines()[0].startswith('Xbcde'))
    eb = Editor(b'ab\tcd\r\n'); eb.key('0i')   # insert before a TAB -> must fall back
    m = eb.mark(); eb.key('X')
    check('insert before tab falls back (no ESC[@)', '\x1b[1@' not in eb.since(m))
    check('insert before tab result correct',
          eb.screen().render().splitlines()[0].startswith('Xab     cd'))

    # Bug 5: j/k track the DISPLAY column across tab-indented lines
    ej = Editor(b'abcdefghijk\r\n\tX\r\nZZZZZZZZZZ\r\n')
    ej.key('0')
    for _ in range(8):
        ej.key('l')                   # display col 8 on line 1 ('i')
    check('j/k setup col 8', ej.screen().col == 8)
    ej.key('j')                       # tab line: col 8 is X (just past the tab)
    check('j onto tab line lands on X at col 8', ej.screen().col == 8)
    ej.key('j')                       # plain line: col 8
    check('j onto plain line keeps col 8', ej.screen().col == 8)
    ej.key('k')                       # back up to tab line, col 8 again
    check('k restores col 8 over a tab line', ej.screen().col == 8)
    # a goal column inside a tab lands ON the tab; the cursor renders at the
    # tab's LAST cell (vi: a cursor on a tab sits at the end of the tab).
    ek = Editor(b'abcdefghijk\r\n\tX\r\n')
    ek.key('0')
    for _ in range(3):
        ek.key('l')                   # display col 3
    ek.key('j')
    check('j with goal inside a tab renders at the tab end (col 7)',
          ek.screen().col == 7)

    # cursor ON a tab renders at the tab's last cell (the space before the first
    # visible char).  G/nG still go to the first non-blank (Vim default).
    et = Editor(b'short\r\n\tORG\t100H\r\n')
    et.key('j')                       # goal col 0 -> onto the tab (offset 0)
    check('j onto a tab-first line sits at the tab end (col 7)',
          et.screen().col == 7)
    et.key('0')
    check('0 on a tab-first line sits at the tab end (col 7)',
          et.screen().col == 7)
    et.key('2G')
    check('2G lands on the first non-blank past the tab (col 8)',
          et.screen().col == 8)

    # New bug: cursor during an insert that lands BETWEEN two tabs.
    # `\tWORD1\tWORD2`, cw on WORD1 -> `\t\tWORD2` with the insert point between
    # the two tabs.  In command mode a cursor ON a tab renders at the tab's last
    # cell (col 15), but during the insert it must mark where the next char will
    # land -- the tab's FIRST cell (col 8, where the W was).  As chars are typed
    # the cursor follows them in.
    ec = Editor(b'\tWORD1\tWORD2\r\n')
    ec.key('w')                       # onto WORD1 (display col 8)
    check('w lands on WORD1 at col 8', ec.screen().col == 8)
    ec.key('cw')                      # delete WORD1, enter insert between tabs
    check('cw insert point sits at the tab start (col 8), not tab end',
          ec.screen().col == 8)
    ec.key('X')                       # first inserted char
    check('typed char advances the cursor to col 9', ec.screen().col == 9)
    ec.key('Y')
    check('cursor keeps following inserted chars (col 10)',
          ec.screen().col == 10)
    ec.key('\x1b')                    # ESC back to command mode
    ec.key(':w\r')
    check('cw-between-tabs result is correct',
          _file(ec) == b'\tXY\tWORD2\r\n')

    # $ is sticky: j/k ride each line's end until a horizontal motion resets it
    es = Editor(b'0123456789\r\nshort\r\nlongerline!\r\n')
    es.key('$')
    check('$ lands on the last char (col 9)', es.screen().col == 9)
    es.key('j')
    check('$ then j rides to end of a short line (col 4)', es.screen().col == 4)
    es.key('j')
    check('$ then j rides to end of a longer line (col 10)', es.screen().col == 10)
    es.key('k')
    check('$ stickiness holds on k too (col 4)', es.screen().col == 4)
    es = Editor(b'0123456789\r\nshort\r\nlongerline!\r\n')
    es.key('$'); es.key('0'); es.key('l')   # a horizontal motion clears stickiness
    es.key('j')
    check('a horizontal motion ends $ stickiness (col 1)', es.screen().col == 1)

    # newbugs: vim ^F/^B full-page scroll (cursor to top / bottom, NEDIT-2 step)
    ef = Editor(b''.join(b'line %03d\r\n' % i for i in range(100)))
    ef.key('jjj')                     # cursor mid-screen (row 3)
    ef.key('\x06')                    # ^F: cursor to TOP, window scrolls forward
    vf = ef.screen(); rows = vf.render().splitlines()
    check('^F puts cursor on the top row', vf.row == 0)
    check('^F scrolled forward (not still at line 000)', rows[0] != 'line 000')
    top_after_f = rows[0].strip()
    ef.key('\x02')                    # ^B: cursor to BOTTOM, window scrolls back
    vf = ef.screen(); rows = vf.render().splitlines()
    check('^B puts cursor on the bottom edit row', vf.row == 22)
    check('^B scrolled back above the ^F top', rows[0].strip() < top_after_f)
    # two-line overlap: ^F then ^B returns to the original top
    check('^F/^B overlap restores top to line 000', rows[0].strip() == 'line 000')

    # regression: ^B on a file with >256 lines.  The page-up math does a 16-bit
    # subtract of (NEDIT-2) from the absolute top line; a high byte > 0 (top line
    # >= 256) used to underflow and clamp the new top to 0, so ^B jumped to the
    # top of the file instead of paging back one screen (repro: DUMP.PRN + G + ^B).
    eb = Editor(b''.join(b'line %03d\r\n' % i for i in range(400)))
    eb.key('G')                       # cursor on last line; top line >= 256
    vb = eb.screen(); rows = vb.render().splitlines()
    check('G top line is >=256 (exercises high byte)', rows[0].strip() == 'line 377')
    eb.key('\x02')                    # ^B: must page back, NOT jump to top
    vb = eb.screen(); rows = vb.render().splitlines()
    check('^B on >256-line file does not jump to top', rows[0].strip() != 'line 000')
    check('^B on >256-line file pages back one screen', rows[0].strip() == 'line 356')
    check('^B (>256 lines) puts cursor on the bottom edit row', vb.row == 22)

    # matching ^F regression on a file with >256 lines.  ^F adds (NEDIT-2) to the
    # absolute top line; with the window already past line 256 both operands have
    # a non-zero high byte, so this guards the page-down arithmetic the same way.
    ef2 = Editor(b''.join(b'line %03d\r\n' % i for i in range(400)))
    ef2.key('290G')                   # window top lands past line 256
    vf2 = ef2.screen(); rows = vf2.render().splitlines()
    check('290G top line is >=256 (exercises high byte)', rows[0].strip() == 'line 267')
    ef2.key('\x06')                   # ^F: page forward one screen (NEDIT-2 step)
    vf2 = ef2.screen(); rows = vf2.render().splitlines()
    check('^F on >256-line file pages forward one screen', rows[0].strip() == 'line 288')
    check('^F (>256 lines) puts cursor on the top edit row', vf2.row == 0)

    # Bug 4: R overwrites in place ("first" -> "XYZst")
    e.key('0')
    e.key('R')
    v = e.screen()
    check('R shows REPLACE indicator', 'REPLACE' in v.render())
    e.key('XYZ')
    e.key('\x1b')
    v = e.screen()
    check('R overwrote chars in place', 'XYZst line' in v.render())

    # --- sub-batch 1: b e ~ J r ---
    e = Editor(content)            # "first line" / "second line" / ...
    e.key('e')
    v = e.screen()
    check('e -> end of "first" (col 4)', v.col == 4)
    e.key('0'); e.key('w')
    v = e.screen()
    check('w -> "line" (col 6)', v.col == 6)
    e.key('b')
    v = e.screen()
    check('b -> back to "first" (col 0)', v.col == 0)
    e.key('~')
    v = e.screen()
    check('~ toggled f->F', 'First line' in v.render())
    check('~ advanced to col 1', v.col == 1)
    e.key('0'); e.key('r'); e.key('Q')
    v = e.screen()
    check('r replaced f->Q', 'Qirst line' in v.render())
    e.key('0'); e.key('J')
    v = e.screen()
    check('J joined lines 1+2', 'Qirst line second line' in v.render())

    # J edge cases (verify saved bytes -- the screen hides trailing spaces).
    def _jbytes(cont, keys):
        ed = Editor(cont)
        for k in keys: ed.key(k)
        ed.key(':w\r')
        return bytes(ed.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
    check('J on last line (trailing NL) is a no-op',
          _jbytes(b'one\r\ntwo\r\nthree\r\n', ['G', 'J']) == b'one\r\ntwo\r\nthree\r\n')
    check('J on last line (no final NL) is a no-op',
          _jbytes(b'aaa\r\nbbb', ['G', 'J']) == b'aaa\r\nbbb')
    check('J with empty line below deletes the blank (no space)',
          _jbytes(b'foo\r\n\r\nbar\r\n', ['J']) == b'foo\r\nbar\r\n')
    ej = Editor(b'foo\r\n\r\nbar\r\n'); ej.key('J')   # cursor must land ON 'o'
    check('J empty-below leaves cursor on last char', ej.screen().col == 2)
    # J must not let a line exceed MAXCOLS (255): reject with a bell + message.
    el = Editor(b'X' * 127 + b'\r\n' + b'Y' * 127 + b'\r\n')   # 127+127+1 = 255 OK
    el.key('J')
    el.key(':w\r')
    check('J to exactly 255 is allowed',
          len(bytes(el.diskfile('TEST', 'TXT')).rstrip(b'\x1a').split(b'\r\n')[0]) == 255)
    eo = Editor(b'X' * 127 + b'\r\n' + b'Y' * 128 + b'\r\n')   # 127+128+1 = 256 -> reject
    mo = eo.mark(); eo.key('J'); oo = eo.since(mo)
    check('J past 255 rings bell + rejects', '\x07' in oo and 'too long' in eo.screen().render().lower())
    eo.key(':w\r')
    check('J past 255 leaves the line unchanged',
          len(bytes(eo.diskfile('TEST', 'TXT')).rstrip(b'\x1a').split(b'\r\n')[0]) == 127)
    check('J with content below joins with a space',
          _jbytes(b'foo\r\nbar\r\n', ['J']) == b'foo bar\r\n')

    # --- sub-batch 2: yy / dd / D / p / P (yank & put) ---
    def rows(ed): return ed.screen().render().split('\n')

    e = Editor(content)            # first / second / third / fourth / fifth
    e.key('yy'); e.key('p')
    r = rows(e)
    check('yy+p duplicates line', r[0] == 'first line' and r[1] == 'first line')

    e = Editor(content)
    e.key('dd')
    r = rows(e)
    check('dd removes line 1', r[0] == 'second line')
    e.key('p')
    r = rows(e)
    check('p puts deleted line below', r[1] == 'first line')

    e = Editor(content)
    e.key('yy'); e.key('j'); e.key('P')
    r = rows(e)
    check('P puts line above', r[1] == 'first line')

    e = Editor(content)
    e.key('D')
    r = rows(e)
    check('D clears line 1, keeps rest', r[0] == '' and r[1] == 'second line')

    # --- sub-batch 3: C / cc / S / s (change family) ---
    e = Editor(content)
    e.key('0'); e.key('l'); e.key('l')      # cursor at col 2 ('r')
    e.key('C'); e.key('XX'); e.key('\x1b')
    r = rows(e)
    check('C changes to EOL', r[0] == 'fiXX')

    e = Editor(content)
    e.key('S'); e.key('NEW'); e.key('\x1b')
    r = rows(e)
    check('S changes whole line', r[0] == 'NEW' and r[1] == 'second line')

    e = Editor(content)
    e.key('cc'); e.key('ABC'); e.key('\x1b')
    r = rows(e)
    check('cc changes whole line', r[0] == 'ABC')

    e = Editor(content)
    e.key('s'); e.key('Z'); e.key('\x1b')
    r = rows(e)
    check('s substitutes one char', r[0] == 'Zirst line')

    # --- sub-batch 4: count prefix (nG and repeats) ---
    e = Editor(content)            # 5 lines: first..fifth
    e.key('5G')
    v = e.screen()
    check('5G -> line 5', rows(e)[4] == 'fifth line' and v.row == 4)

    e = Editor(content)
    e.key('G')
    v = e.screen()
    check('G (no count) -> last line', rows(e)[4] == 'fifth line' and v.row == 4)

    # nG / gg / G land on the first non-blank of the target line (vi behavior)
    ei = Editor(b'top\r\n      indented line\r\nlast\r\n')
    ei.key('2G'); vi_ = ei.screen()
    check('2G lands on first non-blank (col 6)', vi_.row == 1 and vi_.col == 6)
    ei.key('gg'); vg = ei.screen()
    check('gg lands on first non-blank of line 1 (col 0)', vg.row == 0 and vg.col == 0)
    ei.key('G'); vGl = ei.screen()
    check('G lands on first non-blank of last line (col 0)', vGl.row == 2 and vGl.col == 0)

    # nG to a line already on screen must NOT full-redraw (just move the cursor);
    # an off-screen target still scrolls + repaints.  (GBGOTOL must preserve TOPLN
    # across its internal GBMVTOP, which zeroes it.)
    tg = ('\r\n'.join('line %03d' % i for i in range(60)) + '\r\n').encode()
    eg = Editor(tg)
    mg = eg.mark(); eg.key('10G'); og = eg.since(mg)
    check('10G from line 1 (on screen): no full redraw', '\x1b[H' not in og)
    check('10G on-screen lands correctly', eg.screen().row == 9)
    mg = eg.mark(); eg.key('G'); og = eg.since(mg)
    check('G to last line (off screen): full redraw', '\x1b[H' in og)
    mg = eg.mark(); eg.key('5G'); og = eg.since(mg)
    check('5G back up (off screen): full redraw', '\x1b[H' in og)
    check('5G off-screen shows target at top', eg.screen().render().split('\n')[0].strip() == 'line 004')

    # --- ^ : first non-blank of the current line ---
    ec = Editor(b'top\r\n      indented\r\n')
    ec.key('j'); ec.key('$'); ec.key('^'); vc_ = ec.screen()
    check('^ -> first non-blank (col 6)', vc_.row == 1 and vc_.col == 6)
    ecb = Editor(b'      hi there\r\n')
    ecb.key('$'); ecb.key('d^'); ecb.key(':wq\r', idle=3000)
    check('d^ deletes to first non-blank', ecb.diskfile('TEST', 'TXT').split(b'\r\n')[0] == b'      e')

    # --- H / M / L : screen-relative motions (land on first non-blank) ---
    htext = ('\r\n'.join('    L%03d' % i for i in range(40)) + '\r\n').encode()
    eh = Editor(htext)
    eh.key('G')                                  # scroll so a full window shows
    eh.key('H'); vH = eh.screen()
    check('H -> top visible row (col 4)', vH.row == 0 and vH.col == 4)
    eh.key('2H'); v2H = eh.screen()
    check('2H -> second visible row', v2H.row == 1)
    eh.key('M'); vM = eh.screen()
    check('M -> middle of screen', vM.row == 11)
    eh.key('L'); vL = eh.screen()
    check('L -> bottom edit row (22)', vL.row == 22)
    eh.key('3L'); v3L = eh.screen()
    check('3L -> 3rd from bottom', v3L.row == 20)
    # short file: L is the last line, an over-count L clamps to the top line
    es = Editor(b'    a\r\n    b\r\n    c\r\n')
    es.key('L');  check('short L -> last line (row 2)', es.screen().row == 2)
    es.key('9L'); check('short 9L clamps to top (row 0)', es.screen().row == 0)

    # --- * / # : search for the word under the cursor ---
    wtext = b'alpha foo bravo\r\ncharlie foo delta\r\nfoo echo\r\n'
    ew = Editor(wtext)
    ew.key('w')                                  # cursor onto 'foo' (line 0)
    ew.key('*'); vS = ew.screen()
    check('* -> next occurrence of word (row 1)', vS.row == 1 and vS.col == 8)
    ew.key('*'); vS2 = ew.screen()
    check('* again -> wraps to row 2', vS2.row == 2 and vS2.col == 0)
    eh2 = Editor(wtext)
    eh2.key('G')                                 # last line, 'foo' at col 0
    eh2.key('#'); vHsh = eh2.screen()
    check('# -> previous occurrence (row 1)', vHsh.row == 1 and vHsh.col == 8)
    # cursor not on a word: * skips forward to the next word
    ew3 = Editor(b'   foo bar\r\nfoo again\r\n')
    ew3.key('*'); vS3 = ew3.screen()
    check('* from blank skips to next word match', vS3.row == 1)

    e = Editor(content)
    e.key('3j')
    v = e.screen()
    check('3j -> row 3', v.row == 3)

    e = Editor(content)
    e.key('3l')
    v = e.screen()
    check('3l -> col 3', v.col == 3)

    e = Editor(content)
    e.key('3x')
    check('3x deletes 3 chars', rows(e)[0] == 'st line')

    # --- batch 5: f F t T ; , (find char on line) ---  "first line"
    e = Editor(content)            # f i r s t _ l i n e  (cols 0..9)
    e.key('f'); e.key('i')
    check('f i -> first i (col 1)', e.screen().col == 1)
    e.key(';')
    check('; -> next i (col 7)', e.screen().col == 7)
    e.key('F'); e.key('f')
    check('F f -> back to f (col 0)', e.screen().col == 0)
    e.key('t'); e.key('n')
    check('t n -> before n (col 7)', e.screen().col == 7)
    e.key('T'); e.key('r')
    check('T r -> after r (col 3)', e.screen().col == 3)
    e.key('f'); e.key('Z')         # no Z on the line -> no move
    check('f Z (not found) stays', e.screen().col == 3)

    # --- batch 6: operators d/c/y + motion ---
    e = Editor(content); e.key('dw')
    check('dw deletes word+space', rows(e)[0] == 'line')
    e = Editor(content); e.key('d$')
    check('d$ deletes to EOL', rows(e)[0] == '')
    e = Editor(content); e.key('dd')
    check('dd (operator path) deletes line', rows(e)[0] == 'second line')
    e = Editor(content); e.key('de')
    check('de deletes word (inclusive)', rows(e)[0] == ' line')
    e = Editor(content); e.key('dl')
    check('dl deletes one char', rows(e)[0] == 'irst line')
    e = Editor(content); e.key('c$'); e.key('AB'); e.key('\x1b')
    check('c$ changes to EOL', rows(e)[0] == 'AB')
    e = Editor(content); e.key('yw'); e.key('P')
    check('yw + P duplicates word', rows(e)[0] == 'first first line')

    # --- batch 7: search / ? n N ---
    e = Editor(content)            # first/second/third/fourth/fifth line
    e.key('/third\r')
    v = e.screen()
    check('/third -> line 3 col 0', v.row == 2 and v.col == 0)

    e = Editor(content)
    e.key('/line\r')               # "line" on line 1 at col 6
    v = e.screen()
    check('/line -> line 1 col 6', v.row == 0 and v.col == 6)
    e.key('n')                     # next "line" -> line 2 col 7
    v = e.screen()
    check('n -> line 2 col 7', v.row == 1 and v.col == 7)

    e = Editor(content)
    e.key('G')                     # last line
    e.key('?first\r')              # backward to "first" on line 1
    v = e.screen()
    check('?first -> line 1 col 0', v.row == 0 and v.col == 0)

    e = Editor(content)
    e.key('/zzz\r')
    v = e.screen()
    check('search miss reports + stays', 'Pattern not found' in v.render()
          and v.row == 0 and v.col == 0)

    # --- batch 7c: search redraws as cheaply as it can ---
    # On-page match: just a cursor move.  Off-page within a screen's reach: the
    # view scrolls (ESC[<n>M / [<n>L), no full repaint.  A jump further than a
    # screen has no overlap to keep, so it full-repaints.  (Full redraw == ESC[H,
    # emitted only by SCRDRAW's first row.)
    def numbered(n, marks):
        rows = ['row %02d%s' % (i, (' ' + marks[i]) if i in marks else '')
                for i in range(1, n + 1)]
        return ('\r\n'.join(rows) + '\r\n').encode()

    e = Editor(numbered(40, {3: 'alpha', 38: 'omega'}))
    m = e.mark(); e.key('/alpha\r')        # line 3: on the visible page
    v = e.screen()
    check('search on-page: cursor moves, no full redraw',
          v.row == 2 and v.col == 7 and '\x1b[H' not in e.since(m))

    m = e.mark(); e.key('/omega\r')        # line 38: off-page but within a screen
    s = e.since(m)
    check('search off-page within a screen: scrolls, no full repaint',
          'omega' in e.screen().render() and '\x1b[H' not in s
          and '\x1b[M' in s)

    e = Editor(numbered(60, {3: 'alpha', 55: 'omega'}))
    m = e.mark(); e.key('/alpha\r')
    m = e.mark(); e.key('/omega\r')        # line 55: > one screen away -> repaint
    check('search far off-page: full repaint',
          'omega' in e.screen().render() and '\x1b[H' in e.since(m))

    # --- batch 7b: an oversize file auto-activates virtual mode (no /W) -------
    # A file too big to hold resident used to be refused; it now loads a partial
    # window automatically and pages the rest to scratch, using the full TPA as
    # the window (the /W<n> knob only forces a smaller test window).
    big = ('\r\n'.join('row %05d ' % i + '.' * 30 for i in range(900)) + '\r\n').encode()
    eb = Editor(big)
    check('oversize file auto-loads (not refused)',
          'too large' not in eb.screen().render().lower())
    check('oversize file renders its head', _lines(eb)[0].startswith('row 00000'))
    check('oversize file activates virtual mode', eb.s.mem(SYM['VMODE'], 1)[0] == 1)
    _bb = eb.s.mem(SYM['BYTBEL'], 3)
    check('oversize file pages the tail below the window',
          (_bb[0] | (_bb[1] << 8) | (_bb[2] << 16)) > 0)
    eb.close()
    # and it saves byte-exact: a virtual :wq reconstructs the whole document from
    # the resident window ++ the unread original tail (full-TPA window, not /W).
    eb = Editor(big)
    try:
        eb.key(':wq\r', idle=5000)
        check('oversize file round-trips byte-exact (auto virtual :wq)',
              bytes(eb.diskfile('TEST', 'TXT')).rstrip(b'\x1a') == big)
    finally:
        eb.close()

    # --- batch 7c: virtual-mode paging stress (auto full-TPA window) ---------
    # Sweep the cursor over the WHOLE document and back, then far-jump-edit at
    # scattered depths.  This drives every paging primitive many times over on a
    # real oversize file: FILLFWD + SPILLTOP descending, REWIND + SPILLBOT
    # ascending, GBINSRT gap-full spill on the mid-document insert.  A RAM-only
    # oracle is impossible (the file does not fit RAM), so the oracle is the same
    # edit modelled in Python.
    #
    # (a) full down-then-up sweep with no edits must preserve the file byte-exact:
    #     if any primitive dropped or duplicated a record while the window slid
    #     across the entire document twice, the save would not match.
    eb = Editor(big)
    try:
        eb.key(':900\r', idle=8000)      # to the last line: page fully forward
        eb.key(':1\r',   idle=8000)      # back to the top:  page fully back
        eb.key(':wq\r',  idle=8000)
        check('oversize full down+up sweep preserves bytes (no edit)',
              bytes(eb.diskfile('TEST', 'TXT')).rstrip(b'\x1a') == big)
    finally:
        eb.close()

    # (b) far-jump edits at scattered depths, save-compared to a Python model of
    #     the same three edits (delete deep line, insert near top, delete middle).
    dl = [('row %05d ' % i).encode() + b'.' * 30 for i in range(900)]
    del dl[849]                                  # :850 dd
    dl.insert(20, b'INSERTED NEAR THE TOP')      # :20  o<text>ESC  (opens below 20)
    del dl[429]                                  # :430 dd  (in the post-insert doc)
    expected = b'\r\n'.join(dl) + b'\r\n'
    eb = Editor(big)
    try:
        eb.key(':850\r', idle=8000)              # deep forward paging
        eb.key('dd',     idle=4000)
        eb.key(':20\r',  idle=8000)              # far back: reverse paging
        eb.key('oINSERTED NEAR THE TOP\x1b', idle=4000)
        eb.key(':430\r', idle=8000)              # forward again to the middle
        eb.key('dd',     idle=4000)
        eb.key(':wq\r',  idle=8000)
        check('oversize far-jump edits round-trip byte-exact',
              bytes(eb.diskfile('TEST', 'TXT')).rstrip(b'\x1a') == expected)
    finally:
        eb.close()

    # --- batch 8: configurable geometry (/Ln /Cn /R, VIEDIT.CFG) ---
    # /R : read-only -> :w refuses
    e = Editor(content, args=' /R')
    e.key(':w\r')
    check('/R makes file read only', 'read only' in e.screen().render().lower())

    # /L40 : 40 rows.  NEDIT=39 -> edit rows fill '~' down to row 39, status on
    # row 40 (a default 24-row screen could not show those).
    e = Editor(content, args=' /L40')
    r = e.screen(rows=40).render().split('\n')
    check('/L40 sets 40 rows (tilde at row 39)', len(r) >= 39 and r[38] == '~')

    # VIEDIT.CFG : lines=40 has the same effect with no switch
    e = Editor(content, cfg=b'lines=40\r\ncolumns=80\r\n')
    r = e.screen(rows=40).render().split('\n')
    check('VIEDIT.CFG lines=40 honored', len(r) >= 39 and r[38] == '~')

    # /C132 : 132 columns -> a >80-col line is not clipped at col 80
    wide = (b'A' * 120 + b'\r\nsecond\r\n')
    e = Editor(wide, args=' /C132')
    row0 = e.screen(rows=24, cols=132).render().split('\n')[0]
    check('/C132 shows wide line past col 80', len(row0) >= 120)

    # --- batch: virtual buffering, forced-tiny window (/W<n>) ---
    # /W<n> caps the gap buffer to <n> bytes so paging engages on small files.
    # Phase 1: verify the mode gate + partial load; the RAM-only path (no /W, or
    # a file that fits the window) is untouched.  mem() stops the editor, so each
    # reader runs just before close().
    def _u16(ed, a):
        b = ed.s.mem(a, 2); return b[0] | (b[1] << 8)
    def _u24(ed, a):
        b = ed.s.mem(a, 3); return b[0] | (b[1] << 8) | (b[2] << 16)
    BDVM = SYM['BDESC0']
    vlines = [('%04d hello world' % i).encode() for i in range(200)]
    vcontent = b'\r\n'.join(vlines) + b'\r\n'          # ~3600 bytes

    e = Editor(vcontent, args=' /W2048')
    check('/W: partial-load head renders like RAM-only', rows(e)[0] == '0000 hello world')
    _vmode = e.s.mem(SYM['VMODE'], 1)[0]
    _bsize = _u16(e, BDVM + 2)                          # BD_BSIZE
    _txend = _u16(e, BDVM + 8)                          # BD_TXEND (bytes resident)
    _above = _u24(e, SYM['BYTABO'])
    _below = _u24(e, SYM['BYTBEL'])
    check('/W enables virtual mode', _vmode == 1)
    check('/W caps the window (BD_BSIZE == 2048)', _bsize == 2048)
    check('/W loads only a partial window', 0 < _txend <= 2048 and _txend < len(vcontent))
    check('/W accounts the unread tail below, nothing above yet',
          _below > 0 and _above == 0)
    e.close()

    # a file that fits inside the /W window stays fully resident (RAM-only)
    e = Editor(b'one\r\ntwo\r\n', args=' /W2048')
    check('/W: a file that fits stays RAM-only', e.s.mem(SYM['VMODE'], 1)[0] == 0)
    e.close()

    # without /W, virtual mode never engages (same 3600-byte file loads whole)
    e = Editor(vcontent)
    _vm = e.s.mem(SYM['VMODE'], 1)[0]
    _tx = _u16(e, BDVM + 8)
    check('no /W: RAM-only, whole file resident',
          _vm == 0 and _tx == len(vcontent))
    e.close()

    # --- Phase 2 acceptance: paging matches RAM-only rendering across motions ---
    # SCRDRAW paints purely from BD_CSROW + the resident buffer, so if the paging
    # hooks (GBMVDN/GBMVUP/GBMVRT/GBMVLT/GBINSRT + VMFILL) keep the correct slice
    # resident as the window slides, a /W2048 (virtual) editor must render the same
    # edit area AND land the cursor on the same (row,col) as a no-/W (RAM-only)
    # editor fed the SAME keys.  The status row (index 23) is excluded: it shows a
    # window-relative line number in virtual mode (absolute ^G is phase 3), so it
    # legitimately differs.  These editors are never saved (virtual save is phase 4).
    def _pair(keys, idle=2500):
        """Feed identical keys to a virtual and a RAM-only editor; return
        (virtual_rows, virtual_cursor, ram_rows, ram_cursor)."""
        ev = Editor(vcontent, args=' /W2048')
        er = Editor(vcontent)
        try:
            for k in keys:
                ev.key(k, idle=idle); er.key(k, idle=idle)
            vv, vr = ev.screen(), er.screen()
            return (vv.render().split('\n'), (vv.row, vv.col),
                    vr.render().split('\n'), (vr.row, vr.col))
        finally:
            ev.close(); er.close()

    # j well past the screen bottom, still inside the initial resident window.
    gv, cv, gr, cr = _pair(['j' * 30])
    check('/W vs RAM: 30x j edit area matches', gv[:23] == gr[:23])
    check('/W vs RAM: 30x j cursor matches', cv == cr)

    # deep descent: forces FILLFWD of below-content and SPILLTOP of the top edge.
    # Read BYTABOVE off the virtual editor (mem() stops it) to prove text actually
    # spilled above the window rather than the whole file happening to stay resident.
    ev = Editor(vcontent, args=' /W2048')
    er = Editor(vcontent)
    ev.key('j' * 150, idle=3500); er.key('j' * 150, idle=3500)
    vs = ev.screen(); rs = er.screen()
    check('/W vs RAM: 150x j (paged) edit area matches',
          vs.render().split('\n')[:23] == rs.render().split('\n')[:23])
    check('/W vs RAM: 150x j cursor matches', (vs.row, vs.col) == (rs.row, rs.col))
    check('/W: deep j spilled text above the window',
          _u24(ev, SYM['BYTABO']) > 0)             # mem() stops ev -- read last
    er.close(); ev.close()

    # round trip: descend deep, then k all the way back up (REWIND pages above
    # content back in).  Must return to the exact top-of-file view.
    gv, cv, gr, cr = _pair(['j' * 150, 'k' * 150], idle=3500)
    check('/W vs RAM: j*150 then k*150 edit area matches', gv[:23] == gr[:23])
    check('/W vs RAM: j*150 then k*150 cursor matches', cv == cr)
    check('/W: k back to top restores line 0000 at the top row',
          gv[0] == '0000 hello world' and cv == (0, 0))

    # ^F page down x4 (each cursor->top, window +NEDIT-2), then ^B page up x4.
    gv, cv, gr, cr = _pair(['\x06' * 4], idle=3000)
    check('/W vs RAM: 4x ^F edit area matches', gv[:23] == gr[:23])
    check('/W vs RAM: 4x ^F cursor matches', cv == cr)
    gv, cv, gr, cr = _pair(['\x06' * 4, '\x02' * 4], idle=3000)
    check('/W vs RAM: 4x ^F then 4x ^B edit area matches', gv[:23] == gr[:23])
    check('/W vs RAM: 4x ^F then 4x ^B cursor matches', cv == cr)

    # insert deep in the document (o opens a line below; may force GBINSRT SPILLTOP
    # as the gap fills).  The buffer edit is identical in both, so the rendered
    # screens must still match.  Not saved (virtual save is phase 4).
    gv, cv, gr, cr = _pair(['j' * 80, 'oPHASE2 INSERT LINE\x1b'], idle=3000)
    check('/W vs RAM: deep o-insert edit area matches', gv[:23] == gr[:23])
    check('/W vs RAM: deep o-insert cursor matches', cv == cr)
    check('/W: inserted text is on screen', any('PHASE2 INSERT LINE' in r for r in gv))

    # --- Phase 3 acceptance: ^G absolute line accounting vs a RAM-only reference ---
    # ^G reports the ABSOLUTE line (CURLN = LINABOVE + LFs-in-R1) and an approximate
    # total (">=N", CY set) while text still lies below.  The absolute line must
    # equal a RAM-only editor's ^G line at the same logical position, no matter how
    # much has paged above/below.  Parse the status row (index 23) after ^G (\x07).
    def _gstat(ed):
        ed.key('\x07')
        st = ed.screen().render().split('\n')[23]
        ln = re.search(r'line (\d+)', st)
        cl = re.search(r'col (\d+)', st)
        tot = re.search(r'of (>=)?(\d+)', st)
        return (int(ln.group(1)) if ln else None,
                int(cl.group(1)) if cl else None,
                ((tot.group(1) or '') + tot.group(2)) if tot else '')
    def _gpair(keys, idle=3500):
        ev = Editor(vcontent, args=' /W2048'); er = Editor(vcontent)
        try:
            for k in keys:
                ev.key(k, idle=idle); er.key(k, idle=idle)
            return _gstat(ev), _gstat(er)
        finally:
            ev.close(); er.close()

    gv, gr = _gpair(['j' * 30])
    check('/W vs RAM: ^G line after 30x j matches (=31)', gv[0] == gr[0] == 31)
    check('/W vs RAM: ^G col after 30x j matches', gv[1] == gr[1])

    # 150x j pages text above the window; the ABSOLUTE line must still be exact.
    gv, gr = _gpair(['j' * 150])
    check('/W vs RAM: ^G absolute line after 150x j (paged) matches (=151)',
          gv[0] == gr[0] == 151)

    # descend deep, climb partway back: absolute line tracks through both directions.
    gv, gr = _gpair(['j' * 150, 'k' * 77])
    check('/W vs RAM: ^G line after j*150 then k*77 matches (=74)',
          gv[0] == gr[0] == 74)

    # near the top, text still lies below: virtual total is a lower bound (">=N"),
    # RAM-only is the exact 200.  The lower bound must not exceed the true total.
    gv, gr = _gpair(['j' * 5])
    check('/W near top: ^G total is approximate (>=)', gv[2].startswith('>='))
    check('RAM near top: ^G total is exact 200', gr[2] == '200')
    check('/W approximate total is a valid lower bound', int(gv[2][2:]) <= 200)

    # scroll to the last line: everything below drains into the window, so the
    # total firms up to the exact count and equals the RAM-only reference.
    gv, gr = _gpair(['j' * 199])
    check('/W at last line: ^G line 200 matches RAM', gv[0] == gr[0] == 200)
    check('/W at last line: total firms up to exact 200 (no >=)', gv[2] == '200')
    check('RAM at last line: total 200', gr[2] == '200')

    # --- Phase 4 acceptance: virtual-mode save (rename-based, terminal) ---
    # A virtual :wq must reconstruct the WHOLE document byte-exact from the four
    # regions -- name.$$$ (ABOVE) ++ window ++ name.$$B (back-scroll, LIFO) ++ the
    # unread original tail -- and promote $$$ to the real file (with /B keeping the
    # old file as name.BAK, else deleting it).  Oracle: the saved bytes equal the
    # original (no edits) or a RAM-only editor's save (identical edits).  A virtual
    # save consumes the scratch, so it is terminal (save then quit).
    def _vsave(keys):
        ed = Editor(vcontent, args=' /W2048')
        try:
            for k in keys:
                ed.key(k, idle=3000)
            ed.key(':wq\r', idle=4000)
            return bytes(ed.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
        finally:
            ed.close()

    # (a) immediate save: window ++ tail (no ABOVE/$$B yet) == the whole document
    check('/W save: immediate :wq round-trips byte-exact', _vsave([]) == vcontent)
    # (b) deep descent spilled text ABOVE (name.$$$): $$$ ++ window ++ tail
    check('/W save: after j*150 (text spilled above) round-trips byte-exact',
          _vsave(['j' * 150]) == vcontent)
    # (c) descend then climb: exercises all four regions incl. $$B back-scroll
    check('/W save: after j*150,k*40 (ABOVE+window+$$B+tail) round-trips byte-exact',
          _vsave(['j' * 150, 'k' * 40]) == vcontent)

    # (d) edit deep, then save: the virtual save must match a RAM-only editor fed
    # the same keys (the real oracle for an edited document).
    ev = Editor(vcontent, args=' /W2048'); er = Editor(vcontent)
    try:
        for k in ['j' * 120, 'oPHASE4 SAVE LINE\x1b']:
            ev.key(k, idle=3000); er.key(k, idle=3000)
        ev.key(':wq\r', idle=4000); er.key(':wq\r', idle=4000)
        vfile = bytes(ev.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
        rfile = bytes(er.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
    finally:
        ev.close(); er.close()
    check('/W save: edited document matches RAM-only oracle byte-exact', vfile == rfile)
    check('/W save: the edit is present in the saved file', b'PHASE4 SAVE LINE' in vfile)

    # (e) /B keeps the old file as name.BAK (original content); real file = new edit
    eb = Editor(vcontent, args=' /W2048 /B')
    try:
        eb.key('j' * 20, idle=3000)
        eb.key('oBAK TEST LINE\x1b', idle=3000)
        eb.key(':wq\r', idle=4000)
        names = eb.listed()
        bak = bytes(eb.diskfile('TEST', 'BAK')).rstrip(b'\x1a')
        txt = bytes(eb.diskfile('TEST', 'TXT')).rstrip(b'\x1a')
    finally:
        eb.close()
    check('/B save: name.BAK exists', ('TEST', 'BAK') in names)
    check('/B save: name.BAK holds the ORIGINAL document', bak == vcontent)
    check('/B save: the real file holds the new edit', b'BAK TEST LINE' in txt)

    # (f) without /B: no name.BAK; scratch ($$$/$$B) gone after the quit
    en = Editor(vcontent, args=' /W2048')
    try:
        en.key('j' * 20, idle=3000)
        en.key(':wq\r', idle=4000)
        names = en.listed()
    finally:
        en.close()
    check('no /B: no name.BAK left behind', ('TEST', 'BAK') not in names)
    check('virtual save: name.$$$ scratch gone after quit', ('TEST', '$$$') not in names)
    check('virtual save: name.$$B scratch gone after quit', ('TEST', '$$B') not in names)

    # --- Phase 5 acceptance: goto (:N) and search (/ ? n N) across the window ---
    # In virtual mode the resident window is only ~BSIZE around the cursor, so a
    # goto or search to a far target must WALK the cursor through the file, paging
    # (FILLFWD/REWIND) as it moves, and still land at the exact document position a
    # RAM-only editor reaches.  Oracle: _gstat (absolute ^G line/col) of a /W2048
    # editor must equal a RAM-only editor fed the same keys.  (:N goto is newly
    # implemented in BOTH modes -- it previously errored -- so this also covers RAM.)

    # :N goto to a line far below the initial window (forces FILLFWD paging down).
    gv, gr = _gpair([':150\r'], idle=6000)
    check('/W vs RAM: :150 goto lands on the same line', gv[0] == gr[0] == 150)
    check('/W vs RAM: :150 goto lands on the same col', gv[1] == gr[1])

    # goto deep, then goto back up (forces REWIND paging above content back in).
    gv, gr = _gpair([':190\r', ':5\r'], idle=6000)
    check('/W vs RAM: :190 then :5 lands on line 5', gv[0] == gr[0] == 5)

    # forward search to a far, unique prefix below the window (paged forward).
    gv, gr = _gpair(['/0180\r'], idle=8000)
    check('/W vs RAM: /0180 (paged forward) lands on line 181', gv[0] == gr[0] == 181)
    check('/W vs RAM: /0180 col matches', gv[1] == gr[1])

    # n on a UNIQUE match wraps the whole document and returns to the same line
    # (exercises a full forward sweep + wrap across every window boundary).
    gv, gr = _gpair(['/0180\r', 'n'], idle=8000)
    check('/W vs RAM: n on a unique match wraps back to line 181',
          gv[0] == gr[0] == 181)

    # backward search from the last line up to a far-up target (paged backward).
    gv, gr = _gpair(['G', '?0010\r'], idle=8000)
    check('/W vs RAM: ?0010 from bottom (paged backward) lands on line 11',
          gv[0] == gr[0] == 11)

    # N reverses direction: after a forward /, N searches backward and (unique
    # match) wraps back to the same line.
    gv, gr = _gpair(['/0180\r', 'N'], idle=8000)
    check('/W vs RAM: N after / wraps back to line 181', gv[0] == gr[0] == 181)

    # a search miss must sweep the whole document, find nothing, and restore the
    # cursor to where it started (VS_MISS: back to the saved origin line + col).
    em = Editor(vcontent, args=' /W2048')
    try:
        em.key(':100\r', idle=6000)
        before = _gstat(em)                    # line 100
        em.key('/zzzz\r', idle=8000)
        msg = em.screen().render()             # capture before _gstat overwrites row 23
        after = _gstat(em)
    finally:
        em.close()
    check('/W search miss reports "Pattern not found"', 'Pattern not found' in msg)
    check('/W search miss restores the cursor to the origin line',
          after[0] == before[0] == 100)
    check('/W search miss restores the cursor to the origin col', after[1] == before[1])

    # --- Phase 6 acceptance: undo safe-no-op after a page; marks relocate ---
    # Undo and marks are limited to the resident window.  Any page op (SPILLTOP/
    # SPILLBOT/FILLFWD/REWIND) calls URESET, so `u` becomes a safe no-op once the
    # window has slid; marks are logical window offsets that the page primitives
    # relocate (-=128 on a top spill, +=128 on a rewind) or drop (a mark that
    # pages off an edge is unset).  RAM-only never runs the primitives, so undo
    # and marks there stay byte-for-byte as the earlier batches already checked.

    # undo WITHIN the window still works in virtual mode (no page op fired).
    e = Editor(vcontent, args=' /W2048')
    try:
        e.key('x')                              # delete '0' at row 0 col 0
        r_after_x = e.screen().render().split('\n')[0]
        e.key('u')                              # undo -> restored
        r_after_u = e.screen().render().split('\n')[0]
    finally:
        e.close()
    check('/W: in-window edit changes the line', r_after_x == '000 hello world')
    check('/W: in-window undo restores the line', r_after_u == '0000 hello world')

    # undo is a safe NO-OP after a page op.  Two virtual editors both edit at the
    # top then page down and back; one also presses `u`.  If the page made `u` a
    # no-op, the edit survives in BOTH and the renders are identical.
    def _v6(keys):
        ed = Editor(vcontent, args=' /W2048')
        try:
            for k in keys:
                ed.key(k, idle=3500)
            return ed.screen().render().split('\n')[:23]
        finally:
            ed.close()
    noundo = _v6(['x', 'j' * 150, 'k' * 150])          # edit, page down+up
    withu  = _v6(['x', 'j' * 150, 'k' * 150, 'u'])     # ... then u (should no-op)
    check('/W: u after a page is a no-op (renders identical)', noundo == withu)
    check('/W: the pre-page edit survives (u did not undo it)',
          noundo[0] == '000 hello world')

    # a mark that pages off the top is unset: set at line 1, then scroll past it.
    e = Editor(vcontent, args=' /W2048')
    try:
        e.key('ma')                             # mark a at line 1 (top)
        e.key('j' * 150, idle=3500)             # page down; line 1 evicted -> gone
        e.key('`a')                             # jump to mark a
        msg = e.screen().render()
    finally:
        e.close()
    check('/W: a mark paged off the top is unset', 'Mark not set' in msg)

    # a mark that STAYS resident relocates correctly across paging: set it deep,
    # scroll further down (each SPILLTOP slides it -=128), then jump back to it.
    e = Editor(vcontent, args=' /W2048')
    try:
        e.key('j' * 150, idle=3500)             # to line 151
        e.key('ma')                             # mark a at line 151
        e.key('j' * 20, idle=3500)              # page further; top spills, mark tracks
        e.key('`a', idle=3500)                  # jump back to mark a
        ln = _gstat(e)[0]
    finally:
        e.close()
    check('/W: a resident mark relocates correctly across paging (line 151)',
          ln == 151)

    # --- bugs.txt: word motions w/b/e vs W/B/E (punctuation boundaries) ---
    cp = b'foo.bar baz\r\nsecond line\r\n'   # f0 o1 o2 .3 b4 a5 r6 _7 b8 a9 z10
    e = Editor(cp); e.key('w')
    check('w stops at punctuation (col 3)', e.screen().col == 3)
    e = Editor(cp); e.key('W')
    check('W skips to next WORD (col 8)', e.screen().col == 8)
    e = Editor(cp); e.key('e')
    check('e ends word before punct (col 2)', e.screen().col == 2)
    e = Editor(cp); e.key('E')
    check('E ends WORD ignoring punct (col 6)', e.screen().col == 6)
    e = Editor(cp); e.key('$'); e.key('b')
    check('b goes back to "baz" start (col 8)', e.screen().col == 8)
    e = Editor(cp); e.key('W'); e.key('B')   # to "baz", then back over "foo.bar"
    check('B goes back over the whole WORD (col 0)', e.screen().col == 0)
    # dw uses word (punctuation) boundaries: deletes "foo", leaves ".bar baz"
    e = Editor(cp); e.key('dw')
    check('dw stops at punctuation', rows(e)[0] == '.bar baz')

    # cw on a non-blank acts like ce: it does NOT eat the trailing whitespace
    # (vi special case), unlike dw which does.
    cs = b'foo   bar\r\nsecond\r\n'
    e = Editor(cs); e.key('dw')
    check('dw eats trailing blanks', rows(e)[0] == 'bar')
    e = Editor(cs); e.key('cw'); e.key('X'); e.key('\x1b')
    check('cw stops at end of word (ce)', rows(e)[0] == 'X   bar')
    # cw uses the VT100 delete fast path (ESC[P), not a full-line repaint.
    ecw = Editor(b'the quick brown fox\r\nx\r\n')
    mcw = ecw.mark(); ecw.key('cw'); ocw = ecw.since(mcw)
    check('cw emits ESC[NP delete fast path', '\x1b[3P' in ocw)
    check('cw does not re-send the whole line', b'quick brown fox' not in ocw.encode())
    # tab after the cursor -> must fall back to a full line redraw (re-sends the
    # line content; the ESC[P fast path would not)
    etab = Editor(b'the\tquick\r\nx\r\n')
    mtab = etab.mark(); etab.key('cw'); otab = etab.since(mtab)
    check('cw with a tab ahead falls back to full redraw',
          '\x1b[3P' not in otab and 'quick' in otab)
    # cw/dw on WHITESPACE deletes the blank run and (cw) enters insert -- it used
    # to no-op because OPH_C2E's GBPKRT clobbered the motion key in C.
    ews = Editor(b'   foo bar\r\nx\r\n'); ews.key('cw'); ews.key('Z'); ews.key('\x1b')
    check('cw on leading whitespace deletes ws + inserts', rows(ews)[0] == 'Zfoo bar')
    ews2 = Editor(b'   foo bar\r\nx\r\n')
    mws = ews2.mark(); ews2.key('cw'); ows = ews2.since(mws)
    check('cw on whitespace does not full-screen redraw', '\x1b[H' not in ows)
    # dw now uses the same single-line redraw as cw: in-line -> ESC[NP fast path,
    # no full-screen repaint; a range that crosses a line break still full-redraws.
    edw = Editor(b'the quick brown fox\r\nx\r\n')
    mdw = edw.mark(); edw.key('dw'); odw = edw.since(mdw)
    check('dw in-line emits ESC[NP delete fast path', '\x1b[4P' in odw)
    check('dw in-line does not full-screen redraw', '\x1b[H' not in odw)
    check('dw in-line content correct', rows(edw)[0] == 'quick brown fox')
    edt = Editor(b'the\tquick\r\nx\r\n')
    mdt = edt.mark(); edt.key('dw'); odt = edt.since(mdt)
    check('dw with a tab ahead stays single-line (no full redraw)', '\x1b[H' not in odt)
    check('dw with a tab ahead content correct', rows(edt)[0] == 'quick')
    edx = Editor(b'foo \r\nbar baz\r\n'); edx.key('$')   # on the trailing space
    mdx = edx.mark(); edx.key('dw'); odx = edx.since(mdx)
    check('dw across a line break full-redraws', '\x1b[H' in odx)
    check('dw across a line break merges lines', rows(edx)[0] == 'foobar baz')

    # --- bugs.txt: h must not cross to the previous line ---
    e = Editor(content); e.key('j')      # line 2, col 0
    e.key('h')
    v = e.screen()
    check('h at col 0 stays on the line', v.row == 1 and v.col == 0)

    # --- bugs.txt: u with nothing to undo shows a message ---
    e = Editor(content); e.key('u')
    check('u with nothing to undo warns', 'Nothing to undo' in e.screen().render())

    # --- bugs.txt: page at the edge must not force a full redraw ---
    e = Editor(content)
    m = e.mark(); e.key('\x02')           # ^B on the first line: no-op
    check('^B at top: no full redraw', '~' not in e.since(m))
    e = Editor(content); e.key('G')       # last line
    m = e.mark(); e.key('\x06')           # ^F on the last line: no-op
    check('^F at bottom: no full redraw', '~' not in e.since(m))

    # --- bugs.txt: :q warn / ZZ / ':' BS ---
    # Bug 1: :q on a changed file warns and does not quit
    e = Editor(content); e.key('x'); e.key(':q\r')
    r = e.screen().render()
    check(':q on changed file warns', 'No write' in r)
    e.key('x')                       # still in the editor (command mode)
    check(':q did not quit', rows(e)[0] == 'rst line')

    # Bug 2: ZZ writes the file and quits
    e = Editor(content); e.key('x')  # "irst line"
    e.key('ZZ', idle=3000)
    out = e.diskfile('TEST', 'TXT')
    z = out.find(b'\x1a')
    if z >= 0: out = out[:z]
    check('ZZ saved the edit', out == content[1:])   # x dropped the leading 'f'

    # Bug 3: ':' then BS cancels ex mode (back to command mode), no hang/crash
    e = Editor(content); e.key(':'); e.key('\x08')
    e.key('x')                       # command-mode edit proves we returned
    check(': then BS returns to command mode', rows(e)[0] == 'irst line')

    # Backspacing erased chars + one more must still cancel (count survives the
    # echo).  Was a hang: OUTCHR clobbered the count/pointer registers in EX_BS.
    e = Editor(content); e.key(':'); e.key('abcdef'); e.key('\x08' * 7)
    e.key('x')
    check(':abcdef + 7 BS cancels (no hang)', rows(e)[0] == 'irst line')

    # ex prompt caps input at the limit and rings the bell
    e = Editor(content); e.key(':'); e.key('a' * 40)
    m = e.mark()
    e.key('b')                       # 41st char: over the limit
    check('ex prompt bells when full', '\x07' in e.since(m))
    e.key('\x1b')

    # --- batch 13: repeat (.) ---
    e = Editor(content); e.key('x'); e.key('.')
    check('. repeats x', rows(e)[0] == 'rst line')
    e = Editor(content); e.key('dd'); e.key('.')
    check('. repeats dd', rows(e)[0] == 'third line')
    e = Editor(content); e.key('dw'); e.key('.')
    # dw -> "line"; repeated dw is on the last word, so it eats the newline too
    check('. repeats dw', rows(e)[0] == 'second line')
    e = Editor(content); e.key('o'); e.key('X'); e.key('\x1b'); e.key('.')
    r = rows(e)
    check('. repeats o (two new lines)', r[1] == 'X' and r[2] == 'X')
    e = Editor(content); e.key('x'); e.key('.'); e.key('.')
    check('. . repeats again', rows(e)[0] == 'st line')   # deletes f, i, r
    # `.` redraws only the changed line when the repeat stays on one line;
    # a line-count change (e.g. repeating dd) still forces a full repaint.
    e = Editor(content); e.key('x')
    m = e.mark(); e.key('.')
    check('. of x: single-line redraw, no full repaint', '\x1b[H' not in e.since(m))
    e = Editor(content); e.key('r'); e.key('Z')
    m = e.mark(); e.key('.')
    check('. of r: single-line redraw, no full repaint', '\x1b[H' not in e.since(m))
    e = Editor(content); e.key('dd')
    m = e.mark(); e.key('.')
    check('. of dd: full repaint (line count changed)', '\x1b[H' in e.since(m))

    # --- batch 11: single-level undo (u) ---
    e = Editor(content); e.key('x'); e.key('u')
    check('u after x restores char', rows(e)[0] == 'first line')
    e = Editor(content); e.key('dd'); e.key('u')
    check('u after dd restores line', rows(e)[0] == 'first line'
          and rows(e)[1] == 'second line')
    e = Editor(content); e.key('dw'); e.key('u')
    check('u after dw restores word', rows(e)[0] == 'first line')
    e = Editor(content); e.key('D'); e.key('u')
    check('u after D restores EOL', rows(e)[0] == 'first line')
    e = Editor(content); e.key('i'); e.key('XX'); e.key('\x1b'); e.key('u')
    check('u after insert removes typed', rows(e)[0] == 'first line')
    e = Editor(content); e.key('o'); e.key('new'); e.key('\x1b'); e.key('u')
    check('u after o removes opened line', rows(e)[0] == 'first line'
          and rows(e)[1] == 'second line')
    e = Editor(content); e.key('yy'); e.key('p'); e.key('u')
    check('u after p removes the put', rows(e)[0] == 'first line'
          and rows(e)[1] == 'second line')
    # u redraws only the changed line when the undone edit stayed on one line;
    # an undo that restores/removes a whole line still forces a full repaint.
    e = Editor(content); e.key('x')
    m = e.mark(); e.key('u')
    check('u of x: single-line redraw, no full repaint', '\x1b[H' not in e.since(m))
    e = Editor(content); e.key('i'); e.key('XX'); e.key('\x1b')
    m = e.mark(); e.key('u')
    check('u of insert: single-line redraw, no full repaint', '\x1b[H' not in e.since(m))
    e = Editor(content); e.key('dd')
    m = e.mark(); e.key('u')
    check('u of dd: full repaint (line restored)', '\x1b[H' in e.since(m))

    # --- batch 10: count on operators ---
    e = Editor(content); e.key('2dd')
    check('2dd deletes two lines', rows(e)[0] == 'third line')
    e = Editor(content); e.key('2x')
    check('2x deletes two chars', rows(e)[0] == 'rst line')

    # --- batch 9: % bracket match ---  "a(bc[d]e)f"
    bc = b'a(bc[d]e)f\r\nsecond\r\n'   # ( col1 ) col8 ; [ col4 ] col6
    e = Editor(bc)
    e.key('l')            # col 1 = '('
    e.key('%')
    check('% ( -> ) col 8', e.screen().col == 8)
    e.key('%')
    check('% ) -> ( col 1', e.screen().col == 1)
    e = Editor(bc)
    for _ in range(4): e.key('l')   # col 4 = '['
    e.key('%')
    check('% [ -> ] col 6', e.screen().col == 6)
    e = Editor(bc)
    e.key('%')            # col 0 = 'a', not a bracket -> no move
    check('% not-on-bracket stays', e.screen().col == 0)

    # --- bugs.txt regressions ---
    # Bug 1: $ then j must land on the last CHAR of the (shorter) next line,
    # never on the line break.  line1 long, line2 = "x" (1 char).
    c2 = b'first line\r\nx\r\nthird line\r\n'
    e = Editor(c2)
    e.key('$')          # line 1, last char (col 9)
    e.key('j')          # down to line 2 ("x")
    v = e.screen()
    check('$,j lands on last char of short next line', v.row == 1 and v.col == 0)

    # Bug 2: leaving insert/replace mode with ESC must not force a full redraw
    # (a full SCRDRAW paints '~' rows past EOF for this short file).
    e = Editor(content)
    e.key('i'); e.key('Z')
    m = e.mark()
    e.key('\x1b')
    check('ESC from insert: no full redraw', '~' not in e.since(m))
    e.key('R'); e.key('Q')
    m = e.mark()
    e.key('\x1b')
    check('ESC from replace: no full redraw', '~' not in e.since(m))

    # --- windowed (large) file: navigate across slides, save unchanged ---
    big = ('\r\n'.join('row %05d ' % i + 'x' * 40 for i in range(300))
           + '\r\n').encode()
    e = Editor(big)
    v = e.screen()
    check('big: top shows row 00000', 'row 00000' in v.render())
    e.key('G', idle=3000, steps=1_500_000_000)   # to last line (forces slides)
    v = e.screen()
    check('big: G reaches last line', 'row 00299' in v.render())
    e.key('gg', idle=3000, steps=1_500_000_000)   # back to top
    v = e.screen()
    check('big: gg back to top', 'row 00000' in v.render())
    # :w on a windowed file: FISAVE rewinds, DOSAVE must restore the position.
    e.key('G', idle=3000, steps=1_500_000_000)   # go to last line again
    e.key(':w\r', idle=3000, steps=1_500_000_000)
    v = e.screen()
    check('big: cursor stays at last line after :w', 'row 00299' in v.render())
    check('big: top of file not shown after :w', 'row 00000' not in v.render())
    e.key(':q\r', idle=3000)
    out = e.diskfile('TEST', 'TXT')
    z = out.find(b'\x1a')
    if z >= 0: out = out[:z]
    check('big: save round-trips unchanged', out == big)

    # quit cleans up the scratch files (VIB.$$$ / VIA.$$$)
    e = Editor(content)
    e.key(':q\r', idle=3000)
    names = e.listed()
    check('quit deletes VIB.$$$', ('VIB', '$$$') not in names)

    # --- exit resets the scroll region to the full screen (ESC[r), so a
    #     terminal taller than the editor's rows is not left clamped ---
    e = Editor(b''.join(b'line %02d\r\n' % i for i in range(40)))
    e.key('G')                      # scroll: restore must be ESC[r, never ESC[1;24r
    cap = e.cap.getvalue()
    check('scroll restores full region (ESC[r)', '\x1b[r' in cap)
    check('scroll never clamps to ESC[1;24r', '\x1b[1;24r' not in cap)
    m = e.mark(); e.key(':q!\r')
    q = e.since(m)
    check('quit emits ESC[r (unclamp on exit)', '\x1b[r' in q)
    check('quit saves+restores cursor around the reset (ESC7..ESC8)',
          '\x1b7' in q and '\x1b8' in q and q.index('\x1b7') < q.index('\x1b[r') < q.index('\x1b8'))
    check('quit deletes VIA.$$$', ('VIA', '$$$') not in names)

    # --- VT100 single-line fast paths (bugs.txt 3 & 4) ---
    # A file taller than the screen so the viewport actually scrolls.
    tall = ('\r\n'.join('line %03d' % i for i in range(60)) + '\r\n').encode()

    # j at the bottom row scrolls one line via a Delete-Line (DL = ESC[M) issued
    # at the region TOP -- the region-aware mirror of RI (ESC M) used by k.  DL
    # is honored within the DECSTBM region by serial terminals that scroll the
    # whole physical screen on a bare LF or IND at the bottom margin (which ate
    # the status block on real iron).
    e = Editor(tall)
    e.key('30G')                       # somewhere with a full screen below us
    while e.screen().row < 22:
        e.key('j')
    m = e.mark(); e.key('j')
    d = e.since(m)
    check('j-scroll uses DL (ESC[M)', '\x1b[M' in d)
    check('j-scroll no full redraw', '\x1b[H' not in d)

    # k at the top row scrolls one line via Reverse Index (RI = ESC M, terminal
    # scroll-down at the region top), not a redraw.
    e = Editor(tall)
    e.key('40G')
    while e.screen().row > 0:
        e.key('k')
    m = e.mark(); e.key('k')
    d = e.since(m)
    check('k-scroll uses RI (ESC M)', '\x1bM' in d)
    check('k-scroll no full redraw', '\x1b[H' not in d)

    # Multi-line viewport moves ROLL the terminal one line at a time, using the
    # terminal's own scroll (DL = ESC[M / RI = ESC M, both at the region top),
    # each step
    # followed by the one new edge line -- never a full repaint, and the result
    # must match a forced full redraw.
    def edit_rows(ed):
        return ed.screen().render().split('\n')[:23]   # rows 1..23 (not status)

    # ^D / ^U are vim half-page scrolls: the WINDOW always scrolls, even when the
    # cursor could have stayed on screen, carrying the cursor at its screen row.
    e = Editor(tall)                   # cursor at the very top (row 0)
    row0 = e.screen().row
    m = e.mark(); e.key('\x04')        # ^D from the top still scrolls the window
    d = e.since(m)
    fast = edit_rows(e)
    check('^D from top rolls the window (repeated DL)',
          d.count('\x1b[M') >= 2)
    check('^D keeps the cursor screen row', e.screen().row == row0)
    check('^D no full repaint', '\x1b[H' not in d)
    e.key('\x0c')                      # ^L: force a full redraw = ground truth
    check('^D scroll matches full redraw', edit_rows(e) == fast)

    e = Editor(tall)
    e.key('30G')                       # cursor on the bottom row, view scrolled
    m = e.mark(); e.key('\x15')        # ^U scrolls even though the cursor is visible
    d = e.since(m)
    fast = edit_rows(e)
    check('^U rolls the window (repeated RI)',
          d.count('\x1bM') >= 2)
    check('^U no full repaint', '\x1b[H' not in d)
    e.key('\x0c')
    check('^U scroll matches full redraw', edit_rows(e) == fast)

    # ^F / ^B full-page moves also roll one line at a time (NEDIT-2 lines, keeping
    # 2 lines of overlap) instead of repainting, and match a forced full redraw.
    e = Editor(tall)
    before = edit_rows(e)
    m = e.mark(); e.key('\x06')        # ^F: page forward
    d = e.since(m)
    fast = edit_rows(e)
    check('^F rolls forward (repeated DL)',
          d.count('\x1b[M') >= 2)
    check('^F no full repaint', '\x1b[H' not in d)
    check('^F keeps 2 lines of overlap',
          [r.rstrip() for r in fast[0:2]] == [r.rstrip() for r in before[21:23]])
    e.key('\x0c')
    check('^F scroll matches full redraw', edit_rows(e) == fast)

    m = e.mark(); e.key('\x02')        # ^B: page back (undoes the ^F view)
    d = e.since(m)
    fast = edit_rows(e)
    check('^B rolls back (repeated RI)',
          d.count('\x1bM') >= 2)
    check('^B no full repaint', '\x1b[H' not in d)
    e.key('\x0c')
    check('^B scroll matches full redraw', edit_rows(e) == fast)

    # o mid-screen opens a line via ESC[L, no full redraw; content order correct.
    e = Editor(tall)
    e.key('5G')
    m = e.mark(); e.key('o'); e.key('NEWLN'); e.key('\x1b')
    d = e.since(m)
    check('o uses ESC[L', '\x1b[L' in d)
    check('o no full redraw', '\x1b[H' not in d)
    rws = rows(e)
    i4 = next(i for i, r in enumerate(rws) if 'line 004' in r)
    check('o inserts below current line', rws[i4 + 1].strip() == 'NEWLN'
          and rws[i4 + 2].strip() == 'line 005')

    # O mid-screen opens above via ESC[L.
    e = Editor(tall)
    e.key('5G')
    m = e.mark(); e.key('O'); e.key('ABV'); e.key('\x1b')
    d = e.since(m)
    check('O uses ESC[L', '\x1b[L' in d)
    rws = rows(e)
    ia = next(i for i, r in enumerate(rws) if r.strip() == 'ABV')
    check('O inserts above current line', rws[ia + 1].strip() == 'line 004')

    # The dd/J fast paths reproduce the EDIT AREA; the startup "Press K for help"
    # banner is drawn once by STSTART and a full redraw (^L) blanks it, so an
    # on-screen 5G (which no longer repaints) leaves the banner up while ^L drops
    # it.  Compare the edit area only, filtering that banner line.
    def edit_area(g):
        return [r for r in g if 'Press K for help' not in r]
    # dd mid-screen: ESC[M, no full redraw, line removed, neighbours close up.
    e = Editor(tall)
    e.key('5G')
    m = e.mark(); e.key('dd')
    d = e.since(m)
    check('dd uses ESC[M', '\x1b[M' in d)
    check('dd no full redraw', '\x1b[H' not in d)
    rl = [r.strip() for r in rows(e)]
    check('dd removed the line', 'line 004' not in rl)
    i3 = rl.index('line 003')
    check('dd closes the gap', rl[i3 + 1] == 'line 005')
    # dd fast path matches a forced full redraw (ground truth).
    fast = [r.strip() for r in rows(e)]
    e.key('\x0c')
    check('dd fast path == full redraw',
          edit_area([r.strip() for r in rows(e)]) == edit_area(fast))

    # J mid-screen: ESC[M, joins the next line, neighbours close up.
    e = Editor(tall)
    e.key('5G')
    m = e.mark(); e.key('J')
    d = e.since(m)
    check('J uses ESC[M', '\x1b[M' in d)
    rl = [r.strip() for r in rows(e)]
    check('J joined the lines', 'line 004 line 005' in rl)
    fast = [r.strip() for r in rows(e)]
    e.key('\x0c')
    check('J fast path == full redraw',
          edit_area([r.strip() for r in rows(e)]) == edit_area(fast))

    # J at the bottom of a screen-filling file: the line freed at the bottom must
    # become '~' (past EOF), not a blank row.  (Bug: G,k,J left a blank bottom.)
    e = Editor(tall)
    e.key('G'); e.key('k'); e.key('J')
    grid = [''.join(r).rstrip() for r in e.screen().screen]
    fast = grid[:]
    e.key('\x0c')                       # full redraw = ground truth
    full = [''.join(r).rstrip() for r in e.screen().screen]
    check('J at bottom: freed row is "~" not blank', '~' in fast)
    check('J at bottom: fast path == full redraw', fast == full)

    # dd at the bottom leaves the cursor on its own past-EOF row, which stays
    # BLANK (not '~') -- OUTBOTLN must match SCRDRAW's "no tilde on cursor row".
    e = Editor(tall)
    e.key('G'); e.key('dd')
    fast = [''.join(r).rstrip() for r in e.screen().screen]
    e.key('\x0c')
    full = [''.join(r).rstrip() for r in e.screen().screen]
    check('dd at bottom: fast path == full redraw', fast == full)

    # insert-mode CR splits a line.  It must use the one-line-insert fast path
    # (like `o`: ESC[L + redraw), NOT a full-screen repaint, and the result must
    # match a forced full redraw -- including at the bottom-row scroll boundary.
    e = Editor(b''.join(b'row%02d-ABCDEFGH\r\n' % i for i in range(10)))
    e.key('jj')                          # line 3
    e.key('5l')                          # cursor on the '-' (split "row02" | "-...")
    e.key('i')
    m = e.mark(); e.key('\r'); splitout = e.since(m)   # split here
    e.key('\x1b')
    fast = [''.join(r).rstrip() for r in e.screen().screen]
    e.key('\x0c')                        # full redraw = ground truth
    full = [''.join(r).rstrip() for r in e.screen().screen]
    check('insert-CR: no full-screen repaint', '\x1b[H' not in splitout)
    check('insert-CR: used ESC[L insert-line fast path', '\x1b[L' in splitout)
    check('insert-CR: upper half truncated on its row', 'row02' in fast[2] and 'row02-' not in fast[2])
    check('insert-CR: lower half on the next row', fast[3].startswith('-ABCDEFGH'))
    check('insert-CR: fast path == full redraw', fast == full)

    # same at the bottom edit row, where the split forces a viewport scroll.
    e = Editor(tall)
    e.key('G')                           # last line, cursor on the bottom row
    e.key('0'); e.key('i'); e.key('zz\r')   # type then split at the bottom
    e.key('\x1b')
    fast = [''.join(r).rstrip() for r in e.screen().screen]
    e.key('\x0c')
    full = [''.join(r).rstrip() for r in e.screen().screen]
    check('insert-CR at bottom: fast path == full redraw', fast == full)

    # bulk-move primitives (GBMLNE/GBMLNS/VADVG scan+block-move): verify the
    # tricky cases -- a lone CR is content, $ traverses it; goal column rides a
    # tab line; sticky $ clamps across varying line lengths.
    el = Editor(b'XaY\rZb more\r\nsecond line\r\n')
    el.key('$x'); el.key(':wq\r', idle=3000)   # $ to last char, delete it, save
    check('lone-CR: $ lands at real EOL', el.diskfile('TEST', 'TXT').split(b'\r\n')[0] == b'XaY\rZb mor')
    et = Editor(b'abcdefghij\r\n\tTABBED line two\r\nthird line here\r\n')
    et.key('5ljjx'); et.key(':wq\r', idle=3000)
    check('goalcol over tab line', et.diskfile('TEST', 'TXT').split(b'\r\n')[2] == b'thirdline here')
    es = Editor(b'a very long first line here\r\nshort\r\nanother long line of text\r\n')
    es.key('$jjx'); es.key(':wq\r', idle=3000)
    check('sticky $ clamps to last char', es.diskfile('TEST', 'TXT').split(b'\r\n')[2] == b'another long line of tex')

    # unified undo: change-family (cw/C/cc/s) is now undoable (delete+insert),
    # reversed in one `u`.  (Was a no-op before; OPDELN/DELEOL record the
    # deletion and insert mode counts the insertions onto the same record.)
    ub = b'hello world foo\r\nsecond line\r\n'
    def line0(keys):
        e = Editor(ub);
        for k in keys: e.key(k)
        e.key(':wq\r', idle=3000)
        return e.diskfile('TEST', 'TXT').split(b'\r\n')[0]
    check('cw + u restores word',  line0(['cwBYE\x1b', 'u']) == b'hello world foo')
    check('C + u restores line',   line0(['6l', 'CX\x1b', 'u']) == b'hello world foo')
    check('cc + u restores line',  line0(['ccNEW\x1b', 'u']) == b'hello world foo')
    check('s + u restores char',   line0(['sZ\x1b', 'u']) == b'hello world foo')
    check('cw without u applies',  line0(['cwBYE\x1b']) == b'BYE world foo')
    check('r + u restores char',   line0(['rZ', 'u']) == b'hello world foo')
    check('~ + u restores case',   line0(['~', 'u']) == b'hello world foo')
    check('~ without u toggles',   line0(['~']) == b'Hello world foo')
    # R replace-mode is undoable: one record reverses overwrite + EOL-append.
    check('R + u restores chars',  line0(['RXY\x1b', 'u']) == b'hello world foo')
    check('R without u overwrites', line0(['RXY\x1b']) == b'XYllo world foo')
    check('R past EOL + u',         line0(['$', 'RABCDE\x1b', 'u']) == b'hello world foo')
    # J is undoable too (delete the break + insert a space)
    ej = Editor(b'hello\r\nworld\r\nthird\r\n')
    ej.key('J'); ej.key('u'); ej.key(':wq\r', idle=3000)
    check('J + u restores split', ej.diskfile('TEST', 'TXT').replace(b'\x1a', b'').split(b'\r\n')[:3]
          == [b'hello', b'world', b'third'])

    # multi-line linewise yank / put / undo (one undo record, full block yank)
    five = b'L0 a\r\nL1 b\r\nL2 c\r\nL3 d\r\nL4 e\r\n'
    def mlines(keys):
        e = Editor(five)
        for k in keys: e.key(k)
        e.key(':wq\r', idle=3000)
        return e.diskfile('TEST', 'TXT').replace(b'\x1a', b'').split(b'\r\n')
    check('3dd deletes 3 lines',  mlines(['3dd'])[:3] == [b'L3 d', b'L4 e', b''])
    check('3dd + u restores all', mlines(['3dd', 'u'])[:6] == [b'L0 a', b'L1 b', b'L2 c', b'L3 d', b'L4 e', b''])
    check('2yy/p copies 2 lines', mlines(['2yy', 'p'])[:5] == [b'L0 a', b'L0 a', b'L1 b', b'L1 b', b'L2 c'])
    check('3dd/p pastes block',   mlines(['3dd', 'p'])[:5] == [b'L3 d', b'L0 a', b'L1 b', b'L2 c', b'L4 e'])

    # nyy reports 'N lines yanked' on the status row (singular for one line).
    ey = Editor(five); ey.key('3yy')
    check('3yy reports lines yanked', '3 lines yanked' in ey.screen().render())
    ey = Editor(five); ey.key('yy')
    check('yy reports singular line', '1 line yanked' in ey.screen().render())
    # capped at EOF: 9yy on a 5-line file yanks 5
    ey = Editor(five); ey.key('9yy')
    check('9yy capped at EOF', '5 lines yanked' in ey.screen().render())

    # linewise put redraws O-style (insert the line(s), no full-screen blank);
    # edit area must still match a forced full redraw.
    e = Editor(five)
    e.key('2yy')
    m = e.mark(); e.key('p'); pout = e.since(m)
    fast = [''.join(r).rstrip() for r in e.screen().screen[:23]]
    e.key('\x0c')
    full = [''.join(r).rstrip() for r in e.screen().screen[:23]]
    check('put: no full-screen repaint', '\x1b[H' not in pout)
    check('put: used ESC[L insert-line', '\x1b[L' in pout)
    check('put: edit area == full redraw', fast == full)

    # --- VT100 DSR terminal-size auto-detection -------------------------------
    small = b'one\r\ntwo\r\nthree\r\n'
    # default: a 24x80 terminal answers the probe -> 24x80 (unchanged baseline)
    check('autodetect 24x80', Editor(small, term=(24, 80)).geom() == (24, 80))
    # a taller/wider terminal is adopted when nothing was specified
    check('autodetect 30x100', Editor(small, term=(30, 100)).geom() == (30, 100))
    check('autodetect 50x132', Editor(small, term=(50, 132)).geom() == (50, 132))
    # selective fill: /C100 fixes columns, rows still come from the probe (40)
    check('explicit /C100 keeps cols, detects rows',
          Editor(small, args=' /C100', term=(40, 120)).geom() == (40, 100))
    # and the reverse via the config file: columns= set, rows detected
    check('cfg columns= keeps cols, detects rows',
          Editor(small, cfg=b'columns=110\r\n', term=(45, 120)).geom() == (45, 110))
    # both given -> no probe at all (terminal size ignored)
    check('explicit /L30/C90 ignores terminal',
          Editor(small, args=' /L30 /C90', term=(50, 132)).geom() == (30, 90))
    # oversize report clamps to [24..200] rows, [80..132] cols
    check('oversize report clamps', Editor(small, term=(250, 200)).geom() == (200, 132))
    # sub-minimum report clamps up to the 80x24 floor
    check('sub-min report clamps up', Editor(small, term=(10, 40)).geom() == (24, 80))
    # MAXROWS bumped to 200: a tall terminal is honored, not capped at 60
    check('rows 120 honored (past old 60 cap)',
          Editor(small, term=(120, 80)).geom() == (120, 80))
    # a terminal that never answers ESC[6n: editor times out and keeps defaults.
    # idle must exceed VTSTMO (20000) so the timeout spin doesn't abort startup.
    check('no-DSR terminal falls back to 24x80',
          Editor(small, term=None, idle=30000).geom() == (24, 80))

    print(f'\n{PASS[0]} passed, {FAIL[0]} failed')
    if FAIL[0]:
        e2 = Editor(content)
        e2.key('jjll'); e2.screen().dump('after jjll')
    sys.exit(1 if FAIL[0] else 0)


if __name__ == '__main__':
    try:
        main()
    finally:
        # Quit every simulator still alive (and clear the scratch dir) so no
        # altairsim process lingers, whatever happened in main().
        _cleanup_all()

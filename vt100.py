"""Minimal VT100 screen emulator for capturing vi output."""

class VT100:
    def __init__(self, rows=24, cols=80):
        self.rows = rows
        self.cols = cols
        self.screen = [[' '] * cols for _ in range(rows)]
        # reverse-video attribute per cell (1 = reverse, 0 = normal), parallel
        # to self.screen so tests can assert which columns are highlighted.
        self.attr = [[0] * cols for _ in range(rows)]
        self._rev = 0   # current SGR reverse state applied to written cells
        self.row = 0   # 0-based
        self.col = 0
        self._buf = ''   # escape sequence accumulator
        self._scroll_top = 0        # scroll region top (0-based, inclusive)
        self._scroll_bot = rows - 1 # scroll region bottom (0-based, inclusive)

    def feed(self, text):
        for ch in text:
            self._process(ch)

    def _process(self, ch):
        if self._buf:
            self._buf += ch
            self._handle_esc()
            return
        if ch == '\x1b':
            self._buf = '\x1b'
        elif ch == '\r':
            self.col = 0
        elif ch == '\n':
            if self.row == self._scroll_bot:
                # LF at the bottom margin scrolls the region up one line and
                # leaves the cursor on the bottom line (real VT100 behavior).
                del self.screen[self._scroll_top]
                del self.attr[self._scroll_top]
                self.screen.insert(self._scroll_bot, [' '] * self.cols)
                self.attr.insert(self._scroll_bot, [0] * self.cols)
            else:
                self.row = min(self.row + 1, self.rows - 1)
        elif ch == '\b':
            self.col = max(self.col - 1, 0)
        elif ch == '\x07':  # bell
            pass
        elif ord(ch) >= 32:
            if self.col < self.cols:
                self.screen[self.row][self.col] = ch
                self.attr[self.row][self.col] = self._rev
            self.col += 1
            if self.col >= self.cols:
                self.col = self.cols - 1

    def _handle_esc(self):
        b = self._buf
        if b == '\x1b':
            return  # wait for more
        if b == '\x1b[':
            return  # wait for more
        if len(b) < 2:
            return

        # ESC [ sequences
        if b[1] == '[':
            # wait until we get a letter terminator
            if len(b) < 3:
                return
            term = b[-1]
            if term.isalpha() or term in '~@':
                self._csi(b[2:-1], term)
                self._buf = ''
            # else keep accumulating
            return

        # ESC M = Reverse Index (RI): at the top margin scroll the region down
        # one line (cursor stays); otherwise move the cursor up one line.
        if b[1] == 'M':
            if self.row == self._scroll_top:
                del self.screen[self._scroll_bot]
                del self.attr[self._scroll_bot]
                self.screen.insert(self._scroll_top, [' '] * self.cols)
                self.attr.insert(self._scroll_top, [0] * self.cols)
            else:
                self.row = max(self.row - 1, 0)
            self._buf = ''
            return

        # ESC = / ESC > (keypad mode) — ignore
        if b[1] in '=>':
            self._buf = ''
            return

        # unknown 2-char — discard
        self._buf = ''

    def _csi(self, params, cmd):
        parts = params.split(';') if params else ['']
        def p(i, default=1):
            try: return int(parts[i]) if parts[i] else default
            except: return default

        if cmd == 'H' or cmd == 'f':  # cursor position
            self.row = max(0, min(p(0) - 1, self.rows - 1))
            self.col = max(0, min(p(1) - 1, self.cols - 1))
        elif cmd == 'A':  # up
            self.row = max(0, self.row - p(0))
        elif cmd == 'B':  # down
            self.row = min(self.rows - 1, self.row + p(0))
        elif cmd == 'C':  # right
            self.col = min(self.cols - 1, self.col + p(0))
        elif cmd == 'D':  # left
            self.col = max(0, self.col - p(0))
        elif cmd == 'J':  # erase display
            n = p(0, 0)
            if n == 2:
                self.screen = [[' '] * self.cols for _ in range(self.rows)]
                self.attr = [[0] * self.cols for _ in range(self.rows)]
            elif n == 0:
                for c in range(self.col, self.cols):
                    self.screen[self.row][c] = ' '; self.attr[self.row][c] = 0
                for r in range(self.row + 1, self.rows):
                    self.screen[r] = [' '] * self.cols
                    self.attr[r] = [0] * self.cols
            elif n == 1:
                for c in range(0, self.col + 1):
                    self.screen[self.row][c] = ' '; self.attr[self.row][c] = 0
                for r in range(0, self.row):
                    self.screen[r] = [' '] * self.cols
                    self.attr[r] = [0] * self.cols
        elif cmd == 'K':  # erase line
            n = p(0, 0)
            if n == 0:
                for c in range(self.col, self.cols):
                    self.screen[self.row][c] = ' '; self.attr[self.row][c] = 0
            elif n == 1:
                for c in range(0, self.col + 1):
                    self.screen[self.row][c] = ' '; self.attr[self.row][c] = 0
            elif n == 2:
                self.screen[self.row] = [' '] * self.cols
                self.attr[self.row] = [0] * self.cols
        elif cmd == 'r':  # DECSTBM set scroll region (VT100: also resets cursor to (0,0))
            top = max(0, p(0, 1) - 1)
            bot = min(self.rows - 1, p(1, self.rows) - 1)
            if top < bot:
                self._scroll_top = top
                self._scroll_bot = bot
            else:
                self._scroll_top = 0
                self._scroll_bot = self.rows - 1
            self.row = 0   # VT100: DECSTBM always homes the cursor
            self.col = 0
        elif cmd == 'M':  # delete line(s) — scroll up within scroll region
            count = max(1, p(0, 1))
            r = self.row
            top = self._scroll_top
            bot = self._scroll_bot
            if top <= r <= bot:
                for _ in range(count):
                    self.screen.pop(r)
                    self.screen.insert(bot, [' '] * self.cols)
                    self.attr.pop(r)
                    self.attr.insert(bot, [0] * self.cols)
        elif cmd == 'L':  # insert line(s) — scroll down within scroll region
            count = max(1, p(0, 1))
            r = self.row
            top = self._scroll_top
            bot = self._scroll_bot
            if top <= r <= bot:
                for _ in range(count):
                    if bot < len(self.screen):
                        self.screen.pop(bot)
                        self.attr.pop(bot)
                    self.screen.insert(r, [' '] * self.cols)
                    self.attr.insert(r, [0] * self.cols)
        elif cmd == 'P':  # DCH: delete N chars at cursor, shift rest left
            count = max(1, p(0))
            row = self.screen[self.row]; arow = self.attr[self.row]
            del row[self.col:self.col + count]
            row.extend([' '] * count)
            del arow[self.col:self.col + count]
            arow.extend([0] * count)
        elif cmd == '@':  # ICH: insert N blanks at cursor, shift rest right
            count = max(1, p(0))
            row = self.screen[self.row]; arow = self.attr[self.row]
            row[self.col:self.col] = [' '] * count
            del row[self.cols:]
            arow[self.col:self.col] = [0] * count
            del arow[self.cols:]
        elif cmd == 'm':  # SGR: track reverse-video (7 = on, 0/empty = reset)
            for part in (parts or ['']):
                v = part if part else '0'
                if v == '7':
                    self._rev = 1
                elif v == '0':
                    self._rev = 0
        elif cmd == 'h' or cmd == 'l':  # mode set/reset — ignore
            pass

    def hl_cols(self, row):
        """0-based columns on `row` (0-based) currently in reverse video."""
        return [c for c in range(self.cols) if self.attr[row][c]]

    def render(self):
        lines = [''.join(row).rstrip() for row in self.screen]
        # trim trailing blank lines
        while lines and not lines[-1]:
            lines.pop()
        return '\n'.join(lines)

    def dump(self, label=''):
        if label:
            print(f'\n=== {label} === cursor=({self.row+1},{self.col+1})')
        else:
            print(f'\n=== cursor=({self.row+1},{self.col+1})')
        print('+' + '-' * self.cols + '+')
        for i, row in enumerate(self.screen):
            marker = '>' if i == self.row else ' '
            print(f'{marker}{i+1:02d}|{"".join(row)}|')
        print('+' + '-' * self.cols + '+')

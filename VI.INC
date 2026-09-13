;============================================================
; FILE:    VI.INC
; PROJECT: VIEDIT -- vi-style CP/M editor (clean-foundation rebuild; built
;          in the viedit/ directory, produces VIEDIT.COM)
; CPU:     Intel 8080 (strict -- no Z80 extensions)
; ASSEMBLER: Microsoft M80
;------------------------------------------------------------
; Shared equates for all VIEDIT modules. INCLUDE first in every .MAC.
; See DESIGN.md for the architecture.
;
; MODULES (link order):
;   VI.MAC       entry, TPA sizing, main loop, terminal autodetect, globals
;   VISCREEN.MAC VT100 output, screen redraw
;   VIKEY.MAC    key decoder
;   VIBUF.MAC    gap buffer text engine (verbatim CRLF storage)
;   VIWIN.MAC    in-RAM line-navigation primitives
;   VIFILEIO.MAC load (block copy), save (stream + ^Z pad)
;   VICMD.MAC    command dispatch + motions
;   VICMDOPS.MAC operators d/c/y, insert/replace handlers
;   VICMDEXC.MAC ex command line (:), :r/:w/:e/:stat
;   VICMDSCH.MAC find-char (f F t T), /?nN, *#, fast search, %
;   VICMDUND.MAC undo (u), repeat (.), shift (>> <<), marks
;   VICMDSUB.MAC ex substitute (:s)
;   VICMDDAT.MAC shared data: vars, messages, CMDTAB/ACTTAB, BLDTS
;   VIEND.MAC    BMDEND marker (linked last)
; VICMD was split by feature so each source file stays editable on the CP/M box
; (<= ~44 KB) and the single L80 command fits CP/M's ~127-char console line.
;============================================================

;------------------------------------------------------------
; CP/M SYSTEM ADDRESSES
;------------------------------------------------------------
BDOS    EQU     0005H           ; BDOS entry point
WBOOT   EQU     0000H           ; warm boot / exit to CP/M
DFTFCB  EQU     005CH           ; default FCB (command argument 1)
CMDTAIL EQU     0080H           ; command tail / default DMA

;------------------------------------------------------------
; BDOS FUNCTION NUMBERS
;------------------------------------------------------------
BF_CONIN  EQU   1               ; console input (blocking)
BF_CONOUT EQU   2               ; console output: E = char
BF_RAWIO  EQU   6               ; direct console I/O
BF_PRTSTR EQU   9               ; print $-terminated string: DE = addr
BF_RDLINE EQU   10              ; buffered console line input
BF_CONST  EQU   11              ; console status
BF_FOPEN  EQU   15              ; open file: DE=FCB -> A=FFH if not found
BF_FCLOSE EQU   16              ; close file: DE=FCB
BF_FDEL   EQU   19              ; delete file: DE=FCB
BF_FREAD  EQU   20              ; sequential read: DE=FCB -> A=0 ok
BF_FWRITE EQU   21              ; sequential write: DE=FCB -> A=0 ok
BF_FMAKE  EQU   22              ; create file: DE=FCB -> A=FFH if dir full
BF_RENAME EQU   23              ; rename: DE=FCB pair [old 0..15][new 16..31]
BF_GETDSK EQU   25              ; get current disk -> A (0=A, 1=B, ...)
BF_SETDMA EQU   26              ; set DMA address: DE=addr
BF_READRN EQU   33              ; random read:  DE=FCB (r0/r1/r2 = record)
BF_WRITRN EQU   34              ; random write: DE=FCB
BF_FSIZE  EQU   35              ; compute file size -> FCB r0/r1/r2 = #records

;------------------------------------------------------------
; ASCII AND CP/M CONTROL CODES
;------------------------------------------------------------
NUL     EQU     00H             ; null (OUTSTR terminator)
BEL     EQU     07H             ; bell
BS      EQU     08H             ; backspace (Ctrl-H)
TAB     EQU     09H             ; horizontal tab
LF      EQU     0AH             ; line feed  -- the line TERMINATOR
CR      EQU     0DH             ; carriage return -- decoration before LF
ESC     EQU     1BH             ; escape
DEL     EQU     7FH             ; rubout / delete
CPMEOF  EQU     1AH             ; CP/M end-of-file marker (Ctrl-Z)

;------------------------------------------------------------
; STORAGE MODEL -- VERBATIM (see DESIGN.md D1)
;
; The buffer stores file bytes EXACTLY as on disk: CR, LF, ^Z, all of it.
; No transform on load or save (block I/O).
;
; THE LINE-ENDING INVARIANT (the single rule that makes verbatim safe):
;   * LF terminates a line.
;   * A CR immediately before that LF is part of the line break: never
;     displayed; the cursor crosses the whole CR,LF (or a bare LF) as ONE
;     unit and is NEVER left resting between the CR and the LF.
;   * Any CR not immediately before an LF is an ordinary content byte.
;   * ^Z marks logical end-of-text.
; Enforced by GBMVRT/GBMVLT (cross CRLF atomically); every other primitive
; relies on the cursor never being mid-pair.
;------------------------------------------------------------

;------------------------------------------------------------
; SCREEN LAYOUT (VT100 / ANSI) -- defaults; runtime-configurable later
;------------------------------------------------------------
SCRROWS EQU     24              ; terminal rows
SCRCOLS EQU     80              ; terminal columns
EDITROW EQU     23              ; edit-area rows (1..EDITROW)
STATROW EQU     24              ; status / ex / ^G row
MAXCOLS EQU     255             ; max content bytes per displayed line
                                ; (255 = the 1-byte cursor-column ceiling;
                                ;  longer lines warned at load -- D7)
TABWID  EQU     8               ; tab stop interval
LNMAX   EQU     260             ; INRDLN line-buffer cap (MAXCOLS + CRLF + slack)

;------------------------------------------------------------
; BUFFER DESCRIPTOR FIELD OFFSETS (BD_xxx)
;
; One descriptor for the active buffer. CURBDP -> active descriptor.
; BDFLD: A = BD_xxx offset -> HL = field address.
;
;   R1:  base .. base+GAPBG-1        (text before cursor)
;   Gap: base+GAPBG .. base+GAPEN-1
;   R2:  base+GAPEN .. base+BSIZE-1  (text at/after cursor)
;   Char under cursor = R2[0] = byte at base+GAPEN.
;   BD_CSOFF == BD_GAPBG always.
;------------------------------------------------------------
BD_BSTAR EQU    0               ; (2) buffer base address
BD_BSIZE EQU    2               ; (2) buffer capacity in bytes
BD_GAPBG EQU    4               ; (2) gap start offset (= CSOFF)
BD_GAPEN EQU    6               ; (2) gap end offset (R2 head)
BD_TXEND EQU    8               ; (2) logical text length (R1+R2)
BD_TOPLN EQU    10              ; (2) top-of-screen line (window-relative, 0-based)
BD_CSROW EQU    12              ; (1) cursor screen row 0-based
BD_CSCOL EQU    13              ; (1) cursor byte column 0-based
BD_CSOFF EQU    14              ; (2) cursor byte offset (= BD_GAPBG)
BD_MRKBG EQU    16              ; (2) visual mark start (NO_MARK = unset)
BD_MRKEN EQU    18              ; (2) visual mark end   (NO_MARK = unset)
BD_MODIF EQU    20              ; (1) 1 = unsaved changes
BD_SPARE EQU    21              ; (1) reserved
BD_FNAME EQU    22              ; (12) drive(1) + name(8) + ext(3)
BD_SIZE  EQU    34              ; bytes per descriptor

;------------------------------------------------------------
; MEMORY / GAP CONSTANTS
;------------------------------------------------------------
MINMEM  EQU     9*1024          ; minimum gap buffer to run
MINGAP  EQU     0800H           ; gap kept free for editing headroom
NO_MARK EQU     0FFFFH          ; BD_MRKBG/BD_MRKEN sentinel

;------------------------------------------------------------
; FCB FIELD OFFSETS
;------------------------------------------------------------
FCB_DR  EQU     0               ; drive byte (0 = current)
FCB_FN  EQU     1               ; filename, 8 bytes
FCB_FT  EQU     9               ; extension, 3 bytes
FCB_EX  EQU     12              ; extent number
FCB_S1  EQU     13              ; bytes in last record
FCB_S2  EQU     14              ; extent high
FCB_RC  EQU     15              ; record count
FCB_CR  EQU     32              ; current record
FCB_R0  EQU     33              ; random record lo
FCB_R1  EQU     34              ; random record mid
FCB_R2  EQU     35              ; random record hi
FCB_SIZ EQU     36              ; total FCB size

RECSIZ  EQU     128             ; CP/M DMA record size

;------------------------------------------------------------
; EDITOR MODE CONSTANTS (EDMODE) -- used from Phase 4 on
;------------------------------------------------------------
EM_CMD  EQU     0               ; command mode (default)
EM_INS  EQU     1               ; insert mode
EM_RPL  EQU     2               ; replace mode
EM_EX   EQU     3               ; ex command line
EM_SRCH EQU     4               ; search entry

EXMAX   EQU     40              ; max chars on the ':' ex command line (EXBUF cap).
                                ; Shared: EXBUF (VICMDDAT) is DS EXMAX+1, the ex
                                ; reader (VICMDEXC) caps input at EXMAX.

;------------------------------------------------------------
; KEY DECODER -- GETKEY return types (B register) and action codes
; (C register when B = KT_ACT).  See VIKEY.MAC.
;------------------------------------------------------------
KT_NONE EQU     0               ; no key (unrecognised ESC sequence, discarded)
KT_ACT  EQU     1               ; C = ACT_xxx action code
KT_CHAR EQU     2               ; C = ASCII character (printable or control)

ACT_CURUP EQU   1               ; cursor up    (ESC[A / ESC OA)
ACT_CURDN EQU   2               ; cursor down  (ESC[B / ESC OB)
ACT_CURRT EQU   3               ; cursor right (ESC[C / ESC OC)
ACT_CURLT EQU   4               ; cursor left  (ESC[D / ESC OD)
ACT_LINST EQU   5               ; Home / line start (ESC[H / ESC[1~)
ACT_LINEN EQU   6               ; End  / line end   (ESC[F / ESC[4~)
ACT_PGUP  EQU   7               ; page up   (ESC[5~)
ACT_PGDN  EQU   8               ; page down (ESC[6~)
ACT_INS   EQU   9               ; Insert key (ESC[2~)
ACT_DEL   EQU   10              ; Delete key (ESC[3~)
ACT_ESC   EQU   11              ; lone ESC (after timeout)
ACT_MAX   EQU   11              ; highest defined action code

ESCTMO  EQU     500             ; polls before a lone ESC is confirmed

;------------------------------------------------------------
; UNDO record type codes (UNDOTYP) + capacity.  Shared so the undo engine
; and its callers in other modules agree on the constants.  See VICMDUND.
;------------------------------------------------------------
UT_NONE EQU     0               ; no recorded change (undo is a no-op)
UT_INS  EQU     1               ; text was inserted -> undo deletes it
UT_DEL  EQU     2               ; text was deleted  -> undo reinserts it
UBMAX   EQU     1000            ; max bytes recordable in UNDOBUF (< its DS size)

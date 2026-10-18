# Third-party code

The project's own code (`src/`, `tests/`, `tools/`) is GPL-3.0-or-later; see
`LICENSE`. The vendored processor cores below keep their BSD-3-Clause license
and notices, which allow them to be part of a GPL-3.0 program.

Only the individually BSD-3-Clause-licensed processor/disassembler files are
used; this project does not link the MAME application or the modem command
wrapper. License terms are in `vendor/LICENSE-BSD-3-Clause.txt`. Original author
notices remain in the source files.

## ADSP-2181

`vendor/adsp2181/adsp2181_core.{c,h}`, `2100ops.inc`, and the upstream core test
come from [cryan209/modem-dsp-emu](https://github.com/cryan209/modem-dsp-emu),
commit `f0c9d8e39abb385851810b8de581cc2590e42c7b`, directory
`tools/adsp2181emu`. These are standalone adaptations of Aaron Giles's
MAME ADSP-21xx core, explicitly marked BSD-3-Clause.

`dasm.cpp` and `2100dasm.h` come from that directory's standalone disassembler.
Local changes fix its per-instruction buffer addressing, reject out-of-range
disassembly requests, and keep assertions enabled in Release CPU tests.
A local addition gives the core a host user pointer (`adsp2181_set_user`,
`adsp2181_user`), so callbacks find their board without a global and several
boards can run in one process.
The modem-specific helper functions remain in the library but are not used by
the DrumStation board. Their presence is not evidence of DrumStation accuracy.

## MN1880

`vendor/mn1880/` is adapted from [MAME's MN1880 implementation](https://github.com/mamedev/mame/tree/master/src/devices/cpu/mn1880),
copyright AJR, BSD-3-Clause. Upstream commit at download:
`1e3244a07eb04e0f9b98516e533de358afa785c9`.
The upstream commit identifies the original source.

The standalone adaptation replaces MAME lifecycle/debugger/address-space
services with C++ callbacks, preserves the instruction microstates, and throws
on unsupported opcodes instead of silently advancing. One behaviour differs
from upstream: an interrupt taken between a `rep` prefix and its instruction
keeps IP past the prefetched opcode, because that opcode is stacked and
restored rather than fetched again; upstream stepped IP back and the repeated
instruction then ran on the wrong operands after `reti`. Disassembler changes
provide a small data-buffer/formatting shim and a command-line entry point.

## Board research

[MAME's DrumStation driver](https://github.com/mamedev/mame/blob/master/src/mame/novation/drumsta.cpp),
copyright AJR, BSD-3-Clause, supplied the initial processor identification,
ROM checksums, memory map, and IDMA latch wiring. The upstream driver is marked
nonworking/no-sound. This project's board adds functional BDMA, timer/UART
scheduling, panel idle inputs, traces, and inferred serial audio extraction.

The Novation manuals and user-provided ROM images are research inputs, not
covered by the code licenses above, and are not included in source distribution.

## Master reverb

The reverb in `src/main.cpp` (`MasterFx`) is written for this project. Its
comb and allpass delay lengths and stereo offset are the tuning of Freeverb by
Jezar at Dreampoint, which its author placed in the public domain.

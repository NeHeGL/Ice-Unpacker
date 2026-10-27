#!/usr/bin/env python3
"""Unpack Ice Packer (Pack-Ice) compressed Atari ST executables.

Based on the ice_decrunch.c reference decompressor from
github.com/larsbrinkhoff/pack-ice, cross-checked against the real 68000
depacker routine extracted from a genuine Ice Packer v2.40-aware program
(MUG U.K.'s Multi-Depacker) and validated against real Ice-packed files.

How it works:
  The packed data is a small header (a 4-byte "ICE!" signature, a 4-byte
  span -- the total on-disk size of header+compressed bitstream -- and a
  4-byte unpacked length) followed by a compressed bitstream that is read
  and decoded *backwards*, from its own end toward its start. Output is
  built backwards too: the decompressor writes the last byte of the
  original file first, ending at the first. This lets it decompress
  in-place with no extra buffer, a common trick in Atari ST-scene
  crunchers of this era.

  Each token is either a short literal-copy run (bytes copied straight
  from the tail of the compressed stream) or a back-reference match
  (bytes copied from earlier in the already-decompressed output), chosen
  by a handful of variable-length (unary-prefixed) bit codes.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

try:
    import PySimpleGUI as sg
    HAS_GUI = True
except ImportError:
    HAS_GUI = False


def _resource_path(name: str) -> Path:
    """Find a data file next to the script, or inside a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


# The signature is normally "ICE!"; some Pack-Ice 2.x executables spell it "Ice!". The payload format is the same.
ICE_SIGNATURES = (b"ICE!", b"Ice!")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def find_ice_signature(data: bytes) -> int:
    """Returns the byte offset of the earliest 'ICE!' / 'Ice!' signature, or -1 if not found."""
    hits = [i for i in (data.find(sig) for sig in ICE_SIGNATURES) if i >= 0]
    return min(hits) if hits else -1


def identify_ice(data: bytes) -> dict:
    offset = find_ice_signature(data)
    if offset == -1:
        raise ValueError("Not an Ice Packer file ('ICE!' / 'Ice!' signature not found)")
    span, unpacked_len = struct.unpack_from(">II", data, offset + 4)
    if offset + span > len(data):
        raise ValueError("Ice header span runs past end of file -- corrupt or not Ice-packed")
    return {
        "offset": offset,
        "span": span,
        "unpacked_len": unpacked_len,
    }


# ---------------------------------------------------------------------------
# Decompressor (direct port of ice_decrunch.c's bit-level algorithm,
# cross-checked against the real 68000 depacker's own disassembly)
# ---------------------------------------------------------------------------

class _BitState:
    __slots__ = ("packed", "ppos", "bits")

    def __init__(self, packed: bytes, packed_end: int):
        self.packed = packed
        self.ppos = packed_end
        self.ppos -= 1
        self.bits = packed[self.ppos]

    def get_bit(self) -> int:
        bit = 1 if (self.bits & 0x80) else 0
        self.bits = (self.bits << 1) & 0xFF
        if self.bits == 0:
            self.ppos -= 1
            self.bits = self.packed[self.ppos]
            bit = 1 if (self.bits & 0x80) else 0
            self.bits = ((self.bits << 1) & 0xFF) + 1
        return bit

    def get_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.get_bit()
        return v


def _get_depack_length(st: _BitState) -> int:
    bits_to_get = (0, 0, 1, 2, 10)
    number_to_add = (2, 3, 4, 6, 10)
    for i in range(4):
        if st.get_bit() == 0:
            break
    else:
        i = 4
    bits = bits_to_get[i]
    length = st.get_bits(bits) if bits > 0 else 0
    return length + number_to_add[i]


def _get_depack_offset(st: _BitState, length: int) -> int:
    if length == 2:
        if st.get_bit():
            bits, add = 9, 0x3F
        else:
            bits, add = 6, -1
        return st.get_bits(bits) + add

    bits_to_get = (8, 5, 12)
    number_to_add = (31, -1, 287)
    for i in range(2):
        if st.get_bit() == 0:
            break
    else:
        i = 2
    offset = st.get_bits(bits_to_get[i]) + number_to_add[i]
    if offset < 0:
        offset -= length - 2
    return offset


def _get_direct_length(st: _BitState) -> int:
    bits_to_get = (1, 2, 2, 3, 8, 15)
    all_ones = (1, 3, 3, 7, 0xFF, 0x7FFF)
    number_to_add = (1, 2, 5, 8, 15, 270, 270)
    n = 0
    for i in range(6):
        n = st.get_bits(bits_to_get[i])
        if n != all_ones[i]:
            break
    else:
        i = 6
    return n + number_to_add[i]


def _decompress(packed: bytes, span: int, unpacked_len: int) -> bytes:
    st = _BitState(packed, span)
    out = bytearray(unpacked_len)
    upos = unpacked_len

    while True:
        if st.get_bit():
            length = _get_direct_length(st)
            st.ppos -= length
            upos -= length
            if upos < 0 or st.ppos < 0:
                raise ValueError("Decompression ran past buffer bounds (literal run) -- "
                                  "file may be corrupt or not really Ice-packed")
            out[upos:upos + length] = packed[st.ppos:st.ppos + length]

        if upos > 0:
            length = _get_depack_length(st)
            offset = _get_depack_offset(st, length)
            upos -= length
            if upos < 0:
                raise ValueError("Decompression ran past buffer bounds (back-reference) -- "
                                  "file may be corrupt or not really Ice-packed")
            src = upos + length + offset
            for k in range(length - 1, -1, -1):
                out[upos + k] = out[src + k]
        else:
            return bytes(out)


# ---------------------------------------------------------------------------
# Main unpack logic
# ---------------------------------------------------------------------------

def unpack_ice(data: bytes) -> bytes:
    """Decompress an Ice Packer-compressed data blob (which may be embedded
    inside a larger file, e.g. a MUG U.K. Multi-Depacker-style container --
    only the bytes from the 'ICE!' signature onward are used).

    Returns the raw decompressed bytes.
    """
    info = identify_ice(data)
    offset, span, unpacked_len = info["offset"], info["span"], info["unpacked_len"]
    packed = data[offset:offset + span]
    result = _decompress(packed, span, unpacked_len)
    if len(result) != unpacked_len:
        raise ValueError(f"Expected {unpacked_len} bytes, got {len(result)}")
    return result


def unpack_file(src: Path, dest: Path) -> tuple[int, int, dict]:
    packed = src.read_bytes()
    info = identify_ice(packed)
    result = unpack_ice(packed)
    dest.write_bytes(result)
    return len(packed), len(result), info


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unpack Ice Packer (Pack-Ice) compressed Atari ST files"
    )
    parser.add_argument("input", type=Path, nargs="?",
                        help="Packed .prg / .tos / .ttp / data file")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output file (default: <name>_unpacked.prg)")
    parser.add_argument("--identify", action="store_true",
                        help="Show Ice header info and exit")
    args = parser.parse_args(argv)

    if not args.input:
        parser.print_help()
        return 1

    try:
        packed = args.input.read_bytes()

        if args.identify:
            info = identify_ice(packed)
            print(f"ICE! signature at : offset {info['offset']}")
            print(f"Compressed span    : {info['span']:,} bytes")
            print(f"Unpacked length    : {info['unpacked_len']:,} bytes")
            print(f"Packed file size   : {len(packed):,} bytes")
            return 0

        output = args.output or args.input.with_name(args.input.stem + "_unpacked.prg")
        src_size, dest_size, info = unpack_file(args.input, output)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Unpacked    : {output} ({dest_size:,} bytes)")
    print(f"From        : {args.input} ({src_size:,} bytes packed, "
          f"{info['span']:,} bytes compressed span)")
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

# Same structure/brightness as PFX Unpacker's own theme, hue-shifted toward
# a cool ice-blue -- a distinct identity, not a recolor of the same accent.
ICE_THEME = {
    "BACKGROUND": "#1E2620",       # dark charcoal-teal window background
    "TEXT": "#E8EDEA",             # near-white body text
    "INPUT": "#FFFFFF",            # white input fields
    "TEXT_INPUT": "#1C2A30",       # dark text inside input fields
    "SCROLL": "#1E2620",
    "BUTTON": ("#FFFFFF", "#3B6F70"),  # white text on muted ice-teal
    "PROGRESS": ("#000000", "#000000"),
    "BORDER": 1,
    "SLIDER_DEPTH": 0,
    "PROGRESS_DEPTH": 0,
}
ACCENT_COLOR = "#6FD6E8"   # subtitle / accent ice-blue
MUTED_COLOR = "#8AA0A0"    # de-emphasized secondary text (credit line)
INFO_TEXT = "#3FB6E0"      # cyan-blue, for the stats line


def run_gui() -> None:
    if not HAS_GUI:
        raise SystemExit("PySimpleGUI not installed; use CLI mode.")

    sg.theme_add_new("IceDark", ICE_THEME)
    sg.theme("IceDark")

    layout = [
        [sg.Text("Ice Unpacker", font=("Helvetica", 16, "bold"))],
        [sg.Text("Decompress Atari ST files packed with Ice Packer / Pack-Ice",
                 font=("Helvetica", 10), text_color=ACCENT_COLOR)],
        [sg.Text("")],  # Spacing
        [
            sg.Text("Source:", size=(8, 1)),
            sg.Input(key="-SRC-", enable_events=True, size=(45, 1),
                     disabled=True, use_readonly_for_disable=False),
            sg.FileBrowse(file_types=(("Atari executables", "*.prg;*.tos;*.ttp;*.*"),)),
        ],
        [sg.Text("", size=(8, 1)), sg.Text("-", key="-INFO-", size=(50, 1),
                                            font=("Helvetica", 9), text_color=INFO_TEXT)],
        [
            sg.Text("Output:", size=(8, 1)),
            sg.Input(key="-DEST-", size=(45, 1)),
            sg.Button("Unpack", key="-UNPACK-", disabled=True),
        ],
        [sg.Text("", size=(8, 1)), sg.Text("-", key="-STATUS-", size=(60, 1),
                                            font=("Helvetica", 9), text_color=INFO_TEXT)],
        [sg.Text("")],  # Spacing
        [sg.Button("Exit"), sg.Push(), sg.Text("by Jeff Molofee (NeHe)", font=("Helvetica", 8), text_color=MUTED_COLOR)],
    ]
    icon_path = _resource_path("icon.ico")
    window = sg.Window(
        "Ice Unpacker", layout, finalize=True,
        icon=str(icon_path) if icon_path.exists() else None,
    )
    src_full_path = None
    dest_full_path = None

    while True:
        event, values = window.read()
        if event in (sg.WIN_CLOSED, "Exit"):
            break

        if event == "-SRC-" and values["-SRC-"]:
            src_full_path = Path(values["-SRC-"])
            try:
                info = identify_ice(src_full_path.read_bytes())
                dest_full_path = src_full_path.with_name(src_full_path.stem + "_unpacked.prg")
                window["-SRC-"].update(src_full_path.name)
                window["-INFO-"].update(
                    f"ICE! @ {info['offset']}  |  {info['unpacked_len']:,} bytes unpacked"
                )
                window["-DEST-"].update(dest_full_path.name)
                window["-UNPACK-"].update(disabled=False)
                window["-STATUS-"].update("")
            except Exception as exc:
                window["-INFO-"].update(f"Not Ice-packed: {exc}")
                window["-DEST-"].update("")
                window["-UNPACK-"].update(disabled=True)

        if event == "-UNPACK-" and src_full_path:
            dest_name = values["-DEST-"].strip()
            if not dest_name:
                sg.popup_error("Output filename can't be empty.")
                continue
            dest_full_path = src_full_path.with_name(dest_name)
            try:
                _, dest_size, info = unpack_file(src_full_path, dest_full_path)
                window["-DEST-"].update(dest_full_path.name)
                window["-STATUS-"].update(
                    f"Done — {dest_size:,} bytes written to {dest_full_path.name}"
                )
            except Exception as exc:
                sg.popup_error(str(exc))

    window.close()


# ---------------------------------------------------------------------------

def _attach_console_if_cli() -> None:
    """On Windows, the compiled .exe is built windowed (no console of its
    own) so double-clicking it for GUI mode never flashes an extra window
    -- and, unlike a console-subsystem build, closing a console can't take
    the GUI down with it. But that means stdout/stderr start out unusable
    for CLI mode. When launched with args from an existing terminal, attach
    to *that* console instead of owning one, so output is actually visible.
    """
    if sys.platform != "win32" or len(sys.argv) <= 1:
        return
    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")
    except Exception:
        pass


def main() -> int:
    _attach_console_if_cli()
    if len(sys.argv) > 1:
        return run_cli()
    if HAS_GUI:
        run_gui()
        return 0
    print(
        "Usage: ice_unpacker.py packed.prg [-o out.prg] [--identify]",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

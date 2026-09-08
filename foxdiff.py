r"""Compare a foxBMS flash dump against a local build.

Reconstructs the 1 MiB image the build would have produced (flash.bin padded
with 0xFF up to 0x0FFF00, then the 256-byte flashheader.bin), diffs it against
the dump, and decodes the VER_ValidStruct_s flash header of both.

    python foxdiff.py backup.bin --build build\primary\embedded-software\mcu-primary\src\general
    python foxdiff.py backup.bin --build <dir> --elf <dir>\foxbms_primary.elf
    python foxdiff.py backup.bin --header-only
"""
import argparse
import bisect
import os
import re
import struct
import subprocess
import sys

FLASH_BASE = 0x08000000
HEADER_OFF = 0x0FFF00          # 0x080FFF00 - FLASH_BASE
IMAGE_SIZE = 0x100000          # 1 MiB: FLASH (1024K-256) + FLASH_HEADER (256)

VALID_MARKER = 0x1122334455667788
INVALID_MARKER = 0x8877665544332211
NOT_INVALID_MARKER = 0xFFFFFFFFFFFFFFFF
BUILDVARIANT = {0: "UNKNOWN", 1: "LABOR", 2: "TESTBENCH"}


def cstr(b):
    return b.split(b"\x00")[0].decode("ascii", "replace")


def decode_header(h):
    """h = the 256 bytes at 0x080FFF00, laid out as VER_ValidStruct_s."""
    valid, invalid = struct.unpack_from("<QQ", h, 0x00)
    marker = ("VALID" if valid == VALID_MARKER else
              "INVALID" if valid == INVALID_MARKER else "unknown 0x%016X" % valid)
    inv = ("not-invalidated" if invalid == NOT_INVALID_MARKER else
           "0x%016X" % invalid)
    return [
        ("valid marker",     marker),
        ("invalid marker",   inv),
        ("checksum",         "0x%08X" % struct.unpack_from("<I", h, 0x10)[0]),
        ("version",          cstr(h[0x18:0x28])),
        ("project",          cstr(h[0x28:0x38])),
        ("build variant",    BUILDVARIANT.get(h[0x38], "0x%02X" % h[0x38])),
        ("chksum enabled",   "yes" if h[0x3C] else "NO"),
        ("range count",      str(struct.unpack_from("<I", h, 0x44)[0])),
        ("chksum start",     "0x%08X" % struct.unpack_from("<I", h, 0x48)[0]),
        ("chksum end",       "0x%08X" % struct.unpack_from("<I", h, 0x4C)[0]),
        ("vector table",     "0x%08X" % struct.unpack_from("<I", h, 0x50)[0]),
        ("build date",       cstr(h[0xA0:0xAC])),
        ("build time",       cstr(h[0xAC:0xB8])),
    ]


def print_headers(a, b, name_a, name_b):
    fa = decode_header(a)
    fb = decode_header(b) if b is not None else None
    w = max(len(v) for _, v in fa) if fa else 10
    if fb is None:
        print("  %-16s %s" % ("field", name_a))
        for k, v in fa:
            print("  %-16s %s" % (k, v))
        return
    print("  %-16s %-*s  %s" % ("field", w, name_a, name_b))
    for (k, va), (_, vb) in zip(fa, fb):
        mark = "   " if va == vb else "  <"
        print("  %-16s %-*s  %s%s" % (k, w, va, vb, mark))


GIT_RE = re.compile(rb"[0-9a-f]{40}|NOVALIDCOMMIT|NOREMOTE")
URL_RE = re.compile(rb"[ -~]{8,120}")


def git_strings(img, label):
    """The build compiles repo_url/commit_id into .rodata as plain char arrays
    (wscript gitinfo_cfg.c), so they are literally present in the image."""
    print("  %s:" % label)
    hits = GIT_RE.findall(img)
    for h in sorted(set(hits)):
        print("    commit  %s" % h.decode())
    urls = set()
    for m in URL_RE.finditer(img):
        t = m.group()
        if b"://" in t or b"git@" in t or b".git" in t:
            urls.add(t.strip())
    for u in sorted(urls)[:8]:
        print("    remote  %s" % u.decode("ascii", "replace"))
    if not hits and not urls:
        print("    (none found)")


def build_image(flash_bin, header_bin):
    with open(flash_bin, "rb") as f:
        app = f.read()
    with open(header_bin, "rb") as f:
        hdr = f.read()
    if len(app) > HEADER_OFF:
        sys.exit("%s is %d bytes, overlaps the flash header region" % (flash_bin, len(app)))
    if len(hdr) != 256:
        sys.exit("%s is %d bytes, expected 256" % (header_bin, len(hdr)))
    return app + b"\xff" * (HEADER_OFF - len(app)) + hdr


def load_symbols(elf, nm):
    """-> (sorted start addrs, [(start, end, name)])"""
    try:
        out = subprocess.check_output([nm, "-S", "-n", elf], universal_newlines=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print("  (symbol lookup unavailable: %s)" % e)
        return [], []
    syms = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4:
            addr, size, _type, name = parts
            try:
                a, s = int(addr, 16), int(size, 16)
            except ValueError:
                continue
            if s:
                syms.append((a, a + s, name))
    syms.sort()
    return [s[0] for s in syms], syms


def symbol_at(starts, syms, addr):
    i = bisect.bisect_right(starts, addr) - 1
    if i >= 0 and syms[i][0] <= addr < syms[i][1]:
        return syms[i][2]
    return None


def diff_runs(a, b, gap):
    """Differing byte ranges, merging runs separated by < gap equal bytes."""
    runs = []
    n = min(len(a), len(b))
    i = 0
    while i < n:
        if a[i] != b[i]:
            j = i
            last = i
            while j < n:
                if a[j] != b[j]:
                    last = j
                elif j - last >= gap:
                    break
                j += 1
            runs.append((i, last + 1))
            i = last + 1
        else:
            i += 1
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="the 1 MiB backup read from the device")
    ap.add_argument("--build", help="dir holding foxbms_primary_flash.bin / _flashheader.bin")
    ap.add_argument("--flash")
    ap.add_argument("--header")
    ap.add_argument("--elf", help="foxbms_primary.elf, to name the differing regions")
    ap.add_argument("--nm", default="arm-none-eabi-nm")
    ap.add_argument("--header-only", action="store_true")
    ap.add_argument("--git", action="store_true",
                    help="extract the embedded repo URL / commit id from both images")
    ap.add_argument("--gap", type=int, default=32, help="merge runs closer than this")
    ap.add_argument("--max-runs", type=int, default=40)
    ap.add_argument("--sort", choices=["addr", "size"], default="addr",
                    help="'size' lists the biggest changed regions first")
    a = ap.parse_args()

    with open(a.dump, "rb") as f:
        dump = f.read()
    print("dump   %s (%d bytes)" % (a.dump, len(dump)))
    if len(dump) < IMAGE_SIZE:
        print("  WARNING: shorter than 1 MiB, the header region may be missing")

    if a.header_only or not (a.build or (a.flash and a.header)):
        print("\nFlash header on the device:")
        print_headers(dump[HEADER_OFF:HEADER_OFF + 256], None, "device", None)
        if a.git:
            print("\nEmbedded git info:")
            git_strings(dump, "device")
        return 0

    flash = a.flash or os.path.join(a.build, "foxbms_primary_flash.bin")
    header = a.header or os.path.join(a.build, "foxbms_primary_flashheader.bin")
    built = build_image(flash, header)
    print("build  %s (%d bytes app + header)" % (flash, os.path.getsize(flash)))

    print("\nFlash headers:")
    print_headers(dump[HEADER_OFF:HEADER_OFF + 256], built[HEADER_OFF:], "device", "local build")

    if a.git:
        print("\nEmbedded git info:")
        git_strings(dump, "device")
        git_strings(built, "local build")

    runs = diff_runs(dump, built, a.gap)
    total = sum(e - s for s, e in runs)
    app_runs = [r for r in runs if r[0] < HEADER_OFF]
    print("\n%d differing bytes in %d region(s); %d region(s) outside the flash header"
          % (total, len(runs), len(app_runs)))
    if not app_runs:
        print("-> the application image is byte-identical; only the header differs.")
        return 0

    starts, syms = ([], [])
    if a.elf:
        starts, syms = load_symbols(a.elf, a.nm)

    shown = sorted(app_runs, key=lambda r: r[1] - r[0], reverse=True) \
        if a.sort == "size" else app_runs
    print("\n  address     size   symbol")
    for s, e in shown[:a.max_runs]:
        name = symbol_at(starts, syms, FLASH_BASE + s) if syms else None
        print("  0x%08X  %6d  %s" % (FLASH_BASE + s, e - s, name or ""))
    if len(app_runs) > a.max_runs:
        print("  ... %d more" % (len(app_runs) - a.max_runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
sidecar_unsandbox.py - Disables Linux sandbox policy for sidecars on Android.

Forces google3/third_party/jetski/cortex/sidecars/sidecars.(*SidecarManager).sandboxPolicy
to return nil (`MOV X0, XZR; RET`), so sidecar processes run via LocalExecutor.Start
instead of exebox.(*Box).Start which requires Linux namespaces/mounts/cgroups unavailable
on Android (EACCES / exit status 13).
"""
import os
import struct
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PATCHES_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, os.path.join(PATCHES_ROOT, "engine"))

import elf_scanner
import pclntab_scanner

FUNC_NAME = "google3/third_party/jetski/cortex/sidecars/sidecars.(*SidecarManager).sandboxPolicy"

# Go AArch64 stack-split prologue:
#   +0: ldr x16, [x28, #16]     (0xf9400b90)
#   +4: sub x17, sp, #imm12     ((insn & 0xFF0003FF) == 0xD10003F1)
#   +8: cmp x17, x16            (0xeb10023f)
LDR_X16_X28_16 = b"\x90\x0b\x40\xf9"
CMP_X17_X16 = b"\x3f\x02\x10\xeb"

# Replacement (8 bytes):
#   mov x0, xzr                 (0xaa1f03e0 -> e0 03 1f aa)
#   ret                         (0xd65f03c0 -> c0 03 5f d6)
PATCH_BYTES = b"\xe0\x03\x1f\xaa\xc0\x03\x5f\xd6"

# Allow for Go pclntab .text start alignment delta (+0xc0 observed in liblanguage_server.so)
SEARCH_WINDOW = 0x200


def scan(data: bytearray) -> tuple:
    elf_bases = elf_scanner.find_elf_headers(data)
    clean_sites = []
    patched_sites = []

    for idx, base in enumerate(elf_bases):
        region_end = elf_bases[idx + 1] if idx + 1 < len(elf_bases) else len(data)
        text_info = elf_scanner.get_text_section(data, base)
        if not text_info:
            continue
        text_off, text_size = text_info

        pcln = pclntab_scanner.find_pclntab(data, base, region_end)
        if not pcln:
            continue

        fn_info = pclntab_scanner.find_function(data, pcln, FUNC_NAME)
        if not fn_info:
            continue

        entry_off, _ = fn_info
        fn_base = text_off + entry_off
        end = min(fn_base + SEARCH_WINDOW, text_off + text_size - 12)

        for pos in range(fn_base, end + 1, 4):
            tail = data[pos + 8:pos + 12]
            if tail != CMP_X17_X16:
                continue
            head = data[pos:pos + 8]
            if head[:4] == LDR_X16_X28_16:
                second_insn = struct.unpack_from("<I", data, pos + 4)[0]
                if (second_insn & 0xFF0003FF) == 0xD10003F1:
                    clean_sites.append(pos)
                    break
            elif head == PATCH_BYTES:
                patched_sites.append(pos)
                break

    return clean_sites, patched_sites


def apply(data: bytearray, verify: bool = False, dry_run: bool = False, allow_missing: bool = False) -> tuple:
    clean_sites, patched_sites = scan(data)
    info = {
        "name": "sidecar_unsandbox",
        "description": "Force (*SidecarManager).sandboxPolicy to return nil on Android",
        "found_clean": len(clean_sites),
        "found_patched": len(patched_sites),
        "offsets": [hex(x) for x in (clean_sites or patched_sites)],
    }

    if verify:
        if len(patched_sites) == 1 and len(clean_sites) == 0:
            info["status"] = "verified"
            return True, info
        if allow_missing and len(clean_sites) == 0 and len(patched_sites) == 0:
            info["status"] = "skipped_missing"
            return True, info
        info["status"] = "verify_failed"
        return False, info

    if len(patched_sites) == 1 and len(clean_sites) == 0:
        info["status"] = "already_patched"
        return True, info

    if len(clean_sites) == 0:
        if allow_missing:
            info["status"] = "skipped_missing"
            return True, info
        info["status"] = "signature_not_found"
        return False, info

    if not dry_run:
        pos = clean_sites[0]
        data[pos:pos + len(PATCH_BYTES)] = PATCH_BYTES
        info["status"] = "patched"
    else:
        info["status"] = "dry_run"

    return True, info

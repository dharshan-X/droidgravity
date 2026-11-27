#!/usr/bin/env python3
"""
test_sidecar_unsandbox.py - Unit tests for modular sidecar_unsandbox patch.
"""
import os
import struct
import sys
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, os.path.join(MODULES_DIR, "groups", "syscalls"))

import sidecar_unsandbox


class TestSidecarUnsandbox(unittest.TestCase):
    def _build_synthetic_binary(self, shift: int = 0xC0) -> bytearray:
        buf = bytearray(0x2000)
        text_off = 0x1000
        entry_off = 0x100
        real_entry = text_off + entry_off + shift
        # ldr x16, [x28, #16]; sub x17, sp, #0x330; cmp x17, x16; b.ls +0x188
        struct.pack_into("<IIII", buf, real_entry, 0xF9400B90, 0xD10CC3F1, 0xEB10023F, 0x5400C509)
        return buf

    def test_scan_apply_verify_and_idempotency(self):
        raw = self._build_synthetic_binary(shift=0xC0)
        with mock.patch.object(sidecar_unsandbox.elf_scanner, "find_elf_headers", return_value=[0]), \
             mock.patch.object(sidecar_unsandbox.elf_scanner, "get_text_section", return_value=(0x1000, 0x1000)), \
             mock.patch.object(sidecar_unsandbox.pclntab_scanner, "find_pclntab", return_value={"stub": True}), \
             mock.patch.object(sidecar_unsandbox.pclntab_scanner, "find_function", return_value=(0x100, 0x400)):

            clean, patched = sidecar_unsandbox.scan(raw)
            self.assertEqual(clean, [0x11C0])
            self.assertEqual(patched, [])

            # Verify before patch must fail
            ok, info = sidecar_unsandbox.apply(raw, verify=True)
            self.assertFalse(ok)
            self.assertEqual(info["status"], "verify_failed")

            # Dry run leaves binary unchanged
            ok, info = sidecar_unsandbox.apply(raw, dry_run=True)
            self.assertTrue(ok)
            self.assertEqual(info["status"], "dry_run")
            self.assertEqual(raw[0x11C0:0x11C4], sidecar_unsandbox.LDR_X16_X28_16)

            # Live patch writes mov x0, xzr; ret while preserving cmp x17, x16
            ok, info = sidecar_unsandbox.apply(raw, dry_run=False)
            self.assertTrue(ok)
            self.assertEqual(info["status"], "patched")
            self.assertEqual(raw[0x11C0:0x11C8], sidecar_unsandbox.PATCH_BYTES)
            self.assertEqual(raw[0x11C8:0x11CC], sidecar_unsandbox.CMP_X17_X16)

            # Idempotent re-apply
            ok, info = sidecar_unsandbox.apply(raw, dry_run=False)
            self.assertTrue(ok)
            self.assertEqual(info["status"], "already_patched")

            # Verify after patch succeeds
            ok, info = sidecar_unsandbox.apply(raw, verify=True)
            self.assertTrue(ok)
            self.assertEqual(info["status"], "verified")

    def test_missing_signature_respects_allow_missing(self):
        raw = bytearray(0x2000)
        with mock.patch.object(sidecar_unsandbox.elf_scanner, "find_elf_headers", return_value=[]):
            ok, info = sidecar_unsandbox.apply(raw, allow_missing=False)
            self.assertFalse(ok)
            self.assertEqual(info["status"], "signature_not_found")

            ok, info = sidecar_unsandbox.apply(raw, allow_missing=True)
            self.assertTrue(ok)
            self.assertEqual(info["status"], "skipped_missing")


if __name__ == "__main__":
    unittest.main()

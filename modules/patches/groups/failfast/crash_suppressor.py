"""
crash_suppressor.py - Bypasses preinit CPU feature check and preinit atomic flag initialization for ARMv8.0-A CPUs.
"""
from typing import Tuple, Dict, Any, List, Union

CPU_VARIANTS = [
    (
        bytes.fromhex("d3f800900a60905268de41f9e903282a3f010aeae1020054e8033836e8045036"),
        bytes.fromhex("190000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5"),
    ),
    (
        bytes.fromhex("b30701d00a60905268be40f9e903282a3f010aeae1020054e8033836e8045036"),
        bytes.fromhex("190000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5"),
    ),
    (
        bytes.fromhex("f30d01d00a609052683e40f9e903282a3f010aeae1020054e8033836e8045036"),
        bytes.fromhex("190000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5"),
    ),
    (
        bytes.fromhex("b31301b00a609052680245f9e903282a3f010aeae1020054e8033836e8045036"),
        bytes.fromhex("190000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5"),
    ),
]

ATOMIC_VARIANTS = [
    (
        bytes.fromhex("88f500f008a13591290080520801e9f8480000b4c0035fd61f2003d5"),
        bytes.fromhex("88f500f008a13591090140f92a008052490000b4c0035fd60a0100f9"),
    ),
    (
        bytes.fromhex("680401f008a12c91290080520801e9f8480000b4c0035fd61f2003d5"),
        bytes.fromhex("680401f008a12c91090140f92a008052490000b4c0035fd60a0100f9"),
    ),
    (
        bytes.fromhex("a80a01f008a12891290080520801e9f8480000b4c0035fd61f2003d5"),
        bytes.fromhex("a80a01f008a12891090140f92a008052490000b4c0035fd60a0100f9"),
    ),
    (
        bytes.fromhex("4810019008a10e91290080520801e9f8480000b4c0035fd60000009000103291e1031faa5eea00140000000000000000"),
        bytes.fromhex("4810019008a10e91090140f92a008052c90000b4c0035fd60000009000103291e1031faa5eea00140a0100f9fbffff17"),
    ),
]

def find_all(data: Union[bytes, bytearray, memoryview], pattern: bytes) -> List[int]:
    matches = []
    pos = 0
    while True:
        pos = data.find(pattern, pos)
        if pos == -1:
            break
        matches.append(pos)
        pos += len(pattern)
    return matches

def apply(data: bytearray, verify: bool = False, dry_run: bool = False, allow_missing: bool = False) -> Tuple[bool, Dict[str, Any]]:
    raw_cpu_ops = []
    repl_cpu = []
    for target, replacement in CPU_VARIANTS:
        for pos in find_all(data, target):
            raw_cpu_ops.append((pos, replacement))
        repl_cpu.extend(find_all(data, replacement))

    raw_atomic_ops = []
    repl_atomic = []
    for target, replacement in ATOMIC_VARIANTS:
        for pos in find_all(data, target):
            raw_atomic_ops.append((pos, replacement))
        repl_atomic.extend(find_all(data, replacement))

    info = {
        "name": "failfast_cpu_and_atomic",
        "cpu_raw": [hex(p) for p, _ in raw_cpu_ops],
        "cpu_patched": [hex(x) for x in repl_cpu],
        "atomic_raw": [hex(p) for p, _ in raw_atomic_ops],
        "atomic_patched": [hex(x) for x in repl_atomic]
    }

    cpu_total = len(raw_cpu_ops) + len(repl_cpu)
    atomic_total = len(raw_atomic_ops) + len(repl_atomic)

    if (cpu_total == 0 or atomic_total == 0):
        if allow_missing:
            info["status"] = "skipped_missing"
            return True, info
        info["status"] = "missing_signature"
        return False, info

    if verify:
        v_ok = (len(raw_cpu_ops) == 0 and len(repl_cpu) >= 1 and len(raw_atomic_ops) == 0 and len(repl_atomic) == 1)
        info["status"] = "verified" if v_ok else "unverified"
        return v_ok, info

    if len(raw_cpu_ops) == 0 and len(raw_atomic_ops) == 0:
        info["status"] = "already_patched"
        return True, info

    if not dry_run:
        for pos, repl in raw_cpu_ops:
            data[pos:pos + len(repl)] = repl
        for pos, repl in raw_atomic_ops:
            data[pos:pos + len(repl)] = repl
        info["status"] = "patched"
    else:
        info["status"] = "dry_run"

    return True, info

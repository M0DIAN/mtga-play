"""Print trusted helper code for direct execution in node_repl.

The REPL may deny realpath-based file imports while allowing ordinary file reads.
This emits source to paste into a JS cell; it never uses eval or new Function.
"""
from pathlib import Path
import re
import sys


def _module_source(filename, replacements):
    source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    for original, replacement in replacements.items():
        if source.count(original) != 1:
            raise ValueError(f"{filename} entrypoint changed; update bootstrap generator explicitly")
        source = source.replace(original, replacement)
    if re.search(r"^\s*(?:import|export)\b", source, re.MULTILINE):
        raise ValueError(f"{filename} has an unhandled module declaration")
    return source.rstrip() + ";\n"


def bootstrap_source():
    # Each helper gets its own lexical scope. Factories and preflight are public;
    # the readiness function is shared by these exact bundled module versions.
    contract = _module_source("state_contract.mjs", {
        "export function assessReadiness(": "function assessReadiness(",
    })
    controller = _module_source("arena_ui.mjs", {
        "import { readFile, appendFile } from 'node:fs/promises';":
            "const { readFile, appendFile } = await import('node:fs/promises');",
        "import { performance } from 'node:perf_hooks';":
            "const { performance } = await import('node:perf_hooks');",
        "import { randomUUID } from 'node:crypto';":
            "const { randomUUID } = await import('node:crypto');",
        "import { assessReadiness } from './state_contract.mjs';":
            "const assessReadiness = mtgaAssessReadiness;",
        "export async function createArenaController(sky, options) {":
            "globalThis.createArenaController = async function createArenaController(sky, options) {",
    })
    session = _module_source("match_session.mjs", {
        "import { readFile } from 'node:fs/promises';":
            "const { readFile } = await import('node:fs/promises');",
        "import { createHash, randomUUID } from 'node:crypto';":
            "const { createHash, randomUUID } = await import('node:crypto');",
        "import { resolve, sep } from 'node:path';":
            "const { resolve, sep } = await import('node:path');",
        "import { performance } from 'node:perf_hooks';":
            "const { performance } = await import('node:perf_hooks');",
        "import { assessReadiness } from './state_contract.mjs';":
            "const assessReadiness = mtgaAssessReadiness;",
        "export async function createMatchSession(arena, options) {":
            "globalThis.createMatchSession = async function createMatchSession(arena, options) {",
    })
    crop = _module_source("frame_crop.mjs", {
        "import { inflateSync, deflateSync } from 'node:zlib';":
            "const { inflateSync, deflateSync } = await import('node:zlib');",
        "export function cropPNG(": "function cropPNG(",
        "export async function cropCurrentFrame(": "async function cropCurrentFrame(",
    })
    preflight = _module_source("repl_preflight.mjs", {
        "import { mkdtemp, writeFile, readFile, rm, stat } from 'node:fs/promises';":
            "const { mkdtemp, writeFile, readFile, rm, stat } = await import('node:fs/promises');",
        "import { isAbsolute, join } from 'node:path';":
            "const { isAbsolute, join } = await import('node:path');",
        "import { createArenaController } from './arena_ui.mjs';":
            "const createArenaController = mtgaCreateArenaController;",
        "import { createMatchSession } from './match_session.mjs';":
            "const createMatchSession = mtgaCreateMatchSession;",
        "import { cropPNG, cropCurrentFrame } from './frame_crop.mjs';":
            "const { cropPNG, cropCurrentFrame } = mtgaCropHelpers;",
        "export async function runReplPreflight({ tempRoot } = {}) {":
            "globalThis.runReplPreflight = async function runReplPreflight({ tempRoot } = {}) {",
    })
    return ("{\nlet mtgaAssessReadiness;\n{\n" + contract
            + "mtgaAssessReadiness = assessReadiness;\n}\n{\n" + controller
            + "}\n{\n" + session + "}\n"
            + "let mtgaCropHelpers;\n{\n" + crop
            + "mtgaCropHelpers = { cropPNG, cropCurrentFrame };\n"
            + "globalThis.cropPNG = cropPNG; globalThis.cropCurrentFrame = cropCurrentFrame;\n}\n"
            + "const mtgaCreateArenaController = globalThis.createArenaController;\n"
            + "const mtgaCreateMatchSession = globalThis.createMatchSession;\n{\n"
            + preflight + "}\n}\n")


if __name__ == "__main__":
    try:
        sys.stdout.write(bootstrap_source())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

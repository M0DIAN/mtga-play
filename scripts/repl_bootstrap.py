"""Print trusted helper code for direct execution in node_repl.

The REPL may deny realpath-based file imports while allowing ordinary file reads.
This emits source to paste into a JS cell; it never uses eval or new Function.
"""
from pathlib import Path
import sys


def bootstrap_source():
    source = Path(__file__).with_name("arena_ui.mjs").read_text(encoding="utf-8")
    replacements = {
        "import { readFile, appendFile } from 'node:fs/promises';":
            "const { readFile, appendFile } = await import('node:fs/promises');",
        "import { performance } from 'node:perf_hooks';":
            "const { performance } = await import('node:perf_hooks');",
        "export async function createArenaController(sky, options) {":
            "globalThis.createArenaController = async function createArenaController(sky, options) {",
    }
    for original, replacement in replacements.items():
        if source.count(original) != 1:
            raise ValueError("Helper entrypoint changed; update bootstrap generator explicitly")
        source = source.replace(original, replacement)
    return "{\n" + source.rstrip() + ";\n}\n"


if __name__ == "__main__":
    try:
        sys.stdout.write(bootstrap_source())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

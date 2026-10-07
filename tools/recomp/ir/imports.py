"""Read stack-cleanup metadata from the runtime's ImportShim tables.

This deliberately small source reader accepts literal table entries and the
single-DLL three-argument macros used by the runtime. Unsupported expressions
and conflicting declarations stay unknown; it never invents a prototype.
ARGC_CDECL gives cleanup (zero), not an argument count. Its arguments still
need call-site inference. This is census evidence, not an ABI for emitted calls.
"""
from pathlib import Path
import re


COUNT = r"(?:[0-9]+|ARGC_CDECL|ARGC_UNKNOWN)"
ENTRY = re.compile(r'\{\s*"([^"\n]+)"\s*,\s*"([^"\n]+)"\s*,\s*(' + COUNT + r')\s*,')
TABLE = re.compile(r'\b(?:const\s+)?ImportShim\s+\w+\s*\[\s*\]\s*=\s*\{')
MACRO = re.compile(r'#define\s+(\w+)\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*\)'
                   r'\s*\{\s*"([^"\n]+)"\s*,\s*\2\s*,\s*\3\s*,\s*\4\s*\}')


def read_import_cleanup(root=None):
    """Return {(lowercase DLL, exact export): purge bytes or None}.

    Restrict matches to ImportShim arrays; unrelated structs and test tables
    cannot provide import signatures. Conflicts are conservatively unknown.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
    result = {}
    for directory in (root / "runtime", root / "dx"):
        for path in sorted(directory.glob("*.cpp")):
            source = path.read_text()
            source = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
            source = re.sub(r'\\\n', '', source)
            macros = {m[1]: m[5] for m in MACRO.finditer(source)}
            for table in TABLE.finditer(source):
                depth, end = 1, table.end()
                # Table initializers contain no brace-bearing strings in the
                # accepted shapes. Stop at their closing brace, not the next fn.
                while end < len(source) and depth:
                    depth += (source[end] == '{') - (source[end] == '}')
                    end += 1
                body = source[table.end():end - 1]
                entries = [(m[1], m[2], m[3]) for m in ENTRY.finditer(body)]
                for macro, dll in macros.items():
                    pattern = re.compile(r'\b' + re.escape(macro) + r'\(\s*"([^"\n]+)"'
                                         r'\s*,\s*(' + COUNT + r')\s*,')
                    entries.extend((dll, m[1], m[2]) for m in pattern.finditer(body))
                for dll, name, count in entries:
                    purge = (None if count == "ARGC_UNKNOWN" else
                             0 if count == "ARGC_CDECL" else 4 * int(count))
                    key = (dll.lower(), name)
                    result[key] = purge if key not in result or result[key] == purge else None
    return result

import functools
import importlib.util
import re
from collections import namedtuple
from pathlib import Path

KIT = Path(__file__).resolve().parents[3]
GENERATOR = KIT / "tools" / "gen_com_interfaces.py"
HEADERS = KIT / "third_party"
SDK_VERSION = 33
DECLARATION = re.compile(r"(?P<ret>[^;{}()#\n]+?)\bWINAPI\s+(?P<name>\w+)\s*\((?P<params>[^;{}]*?)\)\s*;")
INTERFACE = re.compile(r"^(?:const\s+)?(?:struct\s+)?(I\w*[a-z]\w*)\s*(?P<stars>\*+)\s*\w*$")
WORD_TYPES = frozenset(("UINT", "INT", "DWORD", "BOOL", "FLOAT", "float", "int", "LONG", "ULONG", "HRESULT",
                        "D3DFORMAT", "D3DPOOL", "D3DCOLOR", "D3DXHANDLE", "D3DRESOURCETYPE", "HMODULE", "HWND",
                        "WORD", "BYTE", "char", "D3DXIMAGE_FILEFORMAT", "D3DTEXTUREFILTERTYPE", "void"))
D3DX9_HEADERS = ("wine-d3d9/d3dx9core.h", "wine-d3d9/d3dx9effect.h", "wine-d3d9/d3dx9math.h",
                 "wine-d3d9/d3dx9shader.h", "wine-d3d9/d3dx9tex.h")
SOURCES = {
    "d3d9.dll": (("wine-d3d9/d3d9.h",), frozenset(("Direct3DCreate9",))),
    "d3d8.dll": (("wine-d3d8/d3d8.h",), frozenset(("Direct3DCreate8",))),
    "d3dx9_33.dll": (D3DX9_HEADERS, None),
}

Signature = namedtuple("Signature", "returns inputs outputs")


def _generator():
    spec = importlib.util.spec_from_file_location("gen_com_interfaces", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _interface(text, stars):
    found = INTERFACE.match(" ".join(text.split()))
    return found.group(1) if found and len(found["stars"]) == stars else None


def _word(text):
    text = " ".join(text.split())
    if "*" in text:
        return True
    words = text.replace("const ", "").split()
    return bool(words) and words[0] in WORD_TYPES and not (len(words) == 1 and words[0] == "void")


def _signature(ret, params):
    params = [] if params.strip() in ("", "void") else [part.strip() for part in params.split(",")]
    if not all(_word(part) for part in params):
        return None
    inputs = {k: name for k, part in enumerate(params) if (name := _interface(part, 1))}
    outputs = {k: name for k, part in enumerate(params) if (name := _interface(part, 2))}
    return Signature(_interface(ret, 1), inputs, outputs)


@functools.cache
def _declared(references):
    text = _generator().preprocess("\n".join((HEADERS / reference).read_text(encoding="utf-8")
                                             for reference in references), SDK_VERSION)
    found = {}
    for match in DECLARATION.finditer(text):
        found.setdefault(match["name"], set()).add(_signature(match["ret"], match["params"]))
    return {name: next(iter(signatures)) for name, signatures in found.items() if len(signatures) == 1}


def import_signature(dll, function):
    source = SOURCES.get(dll.lower())
    if source is None:
        return None
    references, names = source
    if names is not None and function not in names:
        return None
    signature = _declared(references).get(function)
    if signature is None or not (signature.returns or signature.inputs or signature.outputs):
        return None
    return signature

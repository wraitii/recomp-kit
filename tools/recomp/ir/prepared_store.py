import pickle

from .ssa import Value
from .x87_scalar import UNSAFE


class Writer(pickle.Pickler):
    def persistent_id(self, obj):
        if obj is UNSAFE:
            return "x87-unsafe"
        return obj.id if isinstance(obj, Value) else None


class Reader(pickle.Unpickler):
    def __init__(self, stream, values):
        super().__init__(stream)
        self.values = values

    def persistent_load(self, ident):
        return UNSAFE if ident == "x87-unsafe" else self.values[ident]


def write(path, prepared):
    values = prepared.ssa.values
    with path.open("wb") as stream:
        pickle.dump([(v.id, v.opc, v.size) for v in values], stream)
        Writer(stream).dump(([(v.args, v.data) for v in values], prepared))


def read(path):
    with path.open("rb") as stream:
        values = [Value(ident, opc, size) for ident, opc, size in pickle.load(stream)]
        rows, prepared = Reader(stream, values).load()
    for value, (args, data) in zip(values, rows):
        value.args, value.data = args, data
    return prepared

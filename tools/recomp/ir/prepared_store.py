import functools
import pickle

from .ssa import Value
from .x87_scalar import UNSAFE

MAGIC = b'recomp-prepared-v2\n'


def _value_ref(ident):
    raise RuntimeError('prepared value reference outside its reader')


def _lookup(values, ident):
    if type(ident) is not int or not 0 <= ident < len(values):
        raise ValueError('prepared body value reference %r is out of range' % (ident,))
    return values[ident]


def _unsafe_ref():
    return UNSAFE


class Writer(pickle.Pickler):
    def reducer_override(self, obj):
        if obj is UNSAFE:
            return _unsafe_ref, ()
        if type(obj) is Value:
            return _value_ref, (obj.id,)
        return NotImplemented


class Reader(pickle.Unpickler):
    def __init__(self, stream, values):
        super().__init__(stream)
        self.values = values

    def find_class(self, module, name):
        if module == __name__:
            if name == '_value_ref':
                return functools.partial(_lookup, self.values)
            if name == '_unsafe_ref':
                return _unsafe_ref
        return super().find_class(module, name)


def write(path, prepared):
    values = prepared.ssa.values
    with path.open("wb") as stream:
        stream.write(MAGIC)
        pickle.dump([(v.id, v.opc, v.size) for v in values], stream)
        Writer(stream).dump(([(v.args, v.data) for v in values], prepared))


def read(path):
    with path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError('prepared body has an unsupported format')
        metadata = pickle.load(stream)
        if any(ident != index for index, (ident, _opc, _size) in enumerate(metadata)):
            raise ValueError('prepared body value table is not dense')
        values = [Value(ident, opc, size) for ident, opc, size in metadata]
        rows, prepared = Reader(stream, values).load()
        if len(rows) != len(values):
            raise ValueError('prepared body argument table disagrees with its value table')
    for value, (args, data) in zip(values, rows):
        value.args, value.data = args, data
    return prepared

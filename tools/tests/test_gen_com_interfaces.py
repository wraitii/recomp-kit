"""COM metadata comes from the reference, with fail-closed handler selection."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('gen_com_interfaces', Path(__file__).parents[1] / 'gen_com_interfaces.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)

HEADER = b'''DECLARE_INTERFACE_IID_(ITest,IUnknown,"11223344-5566-7788-99aa-bbccddeeff00")
{
    STDMETHOD_(ULONG,AddRef)(THIS) PURE;
    STDMETHOD(Write)(THIS_ DWORD value, void *out) PURE;
    STDMETHOD(Missing)(THIS_ UINT flags) PURE;
};
'''


def binding():
    return {'interface': 'ITest', 'table': 'g_test', 'iid': 'IID_ITest',
            'handlers': {'AddRef': 'com_AddRef', 'Write': 'Test_Write'}}


def test_order_arity_iid_and_named_unsupported():
    output = generator.render(HEADER, [binding()])
    assert '{"AddRef", 1, com_AddRef}' in output
    assert '{"Write", 3, Test_Write}' in output
    assert '{"Missing", 2, imports_unsupported}' in output
    assert output.index('AddRef') < output.index('Write') < output.index('Missing')
    assert '0x44, 0x33, 0x22, 0x11, 0x66, 0x55, 0x88, 0x77' in output


def test_unknown_declarations_cannot_shorten_vtable():
    with pytest.raises(ValueError, match='unparsed'):
        generator.render(HEADER.replace(b'STDMETHOD(Missing)', b'STDMETHOD_UNEXPECTED(Missing)'), [binding()])
    with pytest.raises(ValueError, match='unsupported parameter'):
        generator.render(HEADER.replace(b'UINT flags', b'void (*callback)(int)'), [binding()])


def test_stale_or_unsafe_handler_mapping_fails():
    entry = binding()
    entry['handlers']['RemovedMethod'] = 'Old_Handler'
    with pytest.raises(ValueError, match='unknown handler'):
        generator.render(HEADER, [entry])
    entry = binding()
    entry['handlers']['Write'] = 'arbitrary();'
    with pytest.raises(ValueError, match='invalid C'):
        generator.render(HEADER, [entry])
    with pytest.raises(ValueError, match='duplicate'):
        generator.render(HEADER, [binding(), binding()])

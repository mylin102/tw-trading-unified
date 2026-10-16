"""S5 real CLI negative and lexical-scope probes, pure stdlib checker."""
import subprocess
import sys
from pathlib import Path

CHECKER=Path(__file__).resolve().parents[2]/'scripts/validation/check_undefined_names.py'

def run(root):
    return subprocess.run([sys.executable,'-B',str(CHECKER),str(root)],capture_output=True,text=True)

def test_cli_catches_cross_function_name(tmp_path):
    (tmp_path/'bad.py').write_text('def producer():\n    _bar_dict = {}\n    return _bar_dict\ndef consumer():\n    return _bar_dict["spread_z"]\n')
    result=run(tmp_path)
    assert result.returncode==1, result.stderr
    assert 'bad.py:5' in result.stdout and '_bar_dict' in result.stdout

def test_cli_accepts_builtins_globals_imports_closures_comprehensions(tmp_path):
    (tmp_path/'good.py').write_text('import math\nBASE=2\ndef f(value):\n    def inner():\n        return value + BASE\n    return [math.sqrt(inner()) for _ in range(2)]\n')
    result=run(tmp_path)
    assert result.returncode==0, result.stdout+result.stderr
    assert '0 undefined names' in result.stdout

def test_cli_catches_nested_scope_and_module_unknown(tmp_path):
    (tmp_path/'nested.py').write_text('def f():\n    return lambda: absent_name\nresult = undefined_module_name\n')
    result=run(tmp_path)
    assert result.returncode==1
    assert 'absent_name' in result.stdout and 'undefined_module_name' in result.stdout

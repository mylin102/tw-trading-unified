#!/usr/bin/env python3
"""Pure stdlib lexical undefined-name gate. Exit 1 for violations, 2 for syntax/I/O.

Checks referenced globals against actual module bindings and interpreter
builtins, preserving Python closure/comprehension scope semantics via symtable.
Does not claim to detect control-flow unbound locals or dynamic getattr errors.
"""
import argparse
import ast
import builtins
import symtable
from pathlib import Path

INTERPRETER_GLOBALS={'__name__','__file__','__package__','__spec__','__loader__',
                     '__builtins__','__doc__','__cached__','__annotations__'}
EXCLUDE={'tests','test','vendor','venv','.venv','site-packages','__pycache__','.git'}

def check_file(path):
    source=path.read_text(encoding='utf-8')
    tree=ast.parse(source,filename=str(path))
    table=symtable.symtable(source,str(path),'exec')
    bound={s.get_name() for s in table.get_symbols()
           if s.is_assigned() or s.is_imported() or s.is_namespace()}
    known=bound | set(dir(builtins)) | INTERPRETER_GLOBALS
    loads={}
    for node in ast.walk(tree):
        if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load):
            loads.setdefault(node.id,[]).append(node.lineno)
    violations=set()
    def visit(scope):
        for symbol in scope.get_symbols():
            name=symbol.get_name()
            if symbol.is_referenced() and symbol.is_global() and name not in known:
                lines=loads.get(name,[scope.get_lineno()])
                for line in lines:
                    if line >= scope.get_lineno():
                        violations.add((line,name))
                        break
        for child in scope.get_children():
            visit(child)
    visit(table)
    return sorted(violations)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots',nargs='*',type=Path,default=[Path('strategies'),Path('core')])
    args=parser.parse_args(argv)
    count=0
    errors=0
    for root in args.roots:
        if not root.exists():
            print(f'{root}: missing scan root')
            errors+=1
            continue
        paths=[root] if root.is_file() else sorted(root.rglob('*.py'))
        for path in paths:
            if any(part in EXCLUDE for part in path.parts):
                continue
            try:
                found=check_file(path)
            except (OSError,UnicodeError,SyntaxError) as exc:
                print(f'{path}: parse/read error: {exc}')
                errors+=1
                continue
            for line,name in found:
                print(f'{path}:{line}: undefined name {name}')
                count+=1
    print(f'{count} undefined names; {errors} scan errors')
    return 2 if errors else 1 if count else 0

if __name__=='__main__':
    raise SystemExit(main())

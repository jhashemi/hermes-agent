import ast, glob, sys

errors = []
for f in glob.glob('src/**/*.py', recursive=True):
    try:
        ast.parse(open(f).read())
    except SyntaxError as e:
        errors.append(f"{f}: {e}")

if errors:
    print(f"Syntax errors found: {errors}")
    sys.exit(1)
else:
    print(f"Parse check OK — {len(glob.glob('src/**/*.py', recursive=True))} files checked, 0 syntax errors")

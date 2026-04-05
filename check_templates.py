#!/usr/bin/env python3
"""Validates Jinja2 syntax in all template files passed as arguments."""

import re
import sys

from jinja2 import Environment

env = Environment()
errors = []
for path in sys.argv[1:]:
    try:
        src = open(path).read()
        # Detect broken Jinja2 tags caused by HTML formatters (e.g. Biome)
        if re.search(r"\{ %", src):
            errors.append(f"{path}: broken Jinja2 tag '{{ %' found — run: sed -i 's/{{ %/{{%/g' {path}")
            continue
        env.parse(src)
    except Exception as e:
        errors.append(f"{path}: {e}")
if errors:
    print("\n".join(errors))
    sys.exit(1)

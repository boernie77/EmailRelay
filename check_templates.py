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
        # Detect broken Jinja2 tags caused by HTML formatters (e.g. Biome adds spaces or newlines)
        if re.search(r"\{[\r\n ]+%", src):
            errors.append(
                f"{path}: broken Jinja2 tag found (formatter added space/newline in '{{%') — fix: python3 -c \"import re; open('{path}','w').write(re.sub(r'\\\\{{[\\\\r\\\\n ]+%','{{%',open('{path}').read()))\""
            )
            continue
        env.parse(src)
    except Exception as e:
        errors.append(f"{path}: {e}")
if errors:
    print("\n".join(errors))
    sys.exit(1)

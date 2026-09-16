#!/bin/bash
# Does Jarvis still load? The one check a self-edit must pass before it is applied.
# Safe to run any time; it starts nothing.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python -c "
import jarvis.core, jarvis.server, jarvis.__main__
from jarvis.tools import load_all, REGISTRY
load_all()
assert len(REGISTRY) >= 10, 'tools failed to register'
print('ok:', len(REGISTRY), 'tools')
"

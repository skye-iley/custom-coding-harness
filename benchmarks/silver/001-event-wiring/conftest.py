"""Put the instance root on sys.path so `import app` etc. work from tests/.

Deliberately inside the instance, never at the repo root: `pytest tests/` in the
harness suite must neither collect nor import anything under benchmarks/
(invariant 26).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

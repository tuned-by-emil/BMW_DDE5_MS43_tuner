"""Optional integration resources are never required by a public clone."""
from pathlib import Path
from unittest import skipUnless

ROOT = Path(__file__).resolve().parents[1]


def requires_resource(relative_path):
    return skipUnless((ROOT / relative_path).is_file(),
                      'Optional local resource is not installed: ' + relative_path)

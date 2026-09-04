import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from closeloop.synthetic import GeneratorConfig, write_dataset  # noqa: E402

SMALL = GeneratorConfig(
    n_clean=30, n_split=3, n_batch=2, n_timing_out=3, n_timing_in=2,
    n_fee=3, n_fx=2, n_duplicate=2, n_fraud=1, n_chatter=1, seed=7,
)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    write_dataset(out, SMALL)
    return out

"""Host-side loader for the shared classifier contract.

Lives outside ``mcapp/classifier/`` on purpose: that package is a subtree
vendored from mc-chat and must stay path-agnostic. The contract itself
arrives through the ``src/mcapp/contract/`` subtree; it is the one JSON file
the production release must ship (scripts/release.sh).
"""

from __future__ import annotations

from pathlib import Path

from .classifier.axes import AxesEngine
from .classifier.engine import Engine, load_contract_file

DEFAULT_CONTRACT_PATH: Path = (
    Path(__file__).resolve().parent / "contract" / "classifier_contract.json"
)


def load_engine(path: Path = DEFAULT_CONTRACT_PATH) -> Engine:
    """Load the L1 engine from the contract JSON."""
    return load_contract_file(path)


# Stored ``tags`` changed shape (v7 tags -> v8 topics) without a contract change: this code-side
# suffix makes every old row outdated so the startup backfill re-tags it (mirrors mc-chat).
STORED_TAGS_SUFFIX = "+v8tags"


def contract_id(engine: Engine, axes: AxesEngine | None = None) -> str:
    """Identity of the inputs a stored classification depends on.

    ``<taxonomy_version>:<source_commit>+v8tags``, plus ``+axes:<commit>`` when an axes engine
    is given: origin reads timing and format, so an axes-only contract change must re-run the
    reclassify sweep too (it runs after the axes backfill, ``main.run_classifier_startup``).
    """
    base = f"{engine.taxonomy_version}:{engine.source_commit}{STORED_TAGS_SUFFIX}"
    return base if axes is None else f"{base}+axes:{axes.source_commit}"

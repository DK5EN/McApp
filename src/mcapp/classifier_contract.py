"""Host-side loader for the shared classifier contract.

Lives outside ``mcapp/classifier/`` on purpose: that package is a subtree
vendored from mc-chat and must stay path-agnostic. The contract itself
arrives through the ``src/mcapp/contract/`` subtree; it is the one JSON file
the production release must ship (scripts/release.sh).
"""

from __future__ import annotations

from pathlib import Path

from .classifier.engine import Engine, load_contract_file

DEFAULT_CONTRACT_PATH: Path = (
    Path(__file__).resolve().parent / "contract" / "classifier_contract.json"
)


def load_engine(path: Path = DEFAULT_CONTRACT_PATH) -> Engine:
    """Load the L1 engine from the contract JSON."""
    return load_contract_file(path)


def contract_id(engine: Engine) -> str:
    """Identity of the loaded rule set: ``<taxonomy_version>:<source_commit>``."""
    return f"{engine.taxonomy_version}:{engine.source_commit}"

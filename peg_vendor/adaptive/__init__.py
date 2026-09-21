"""
adaptive/ — Life Compute adaptive molecular search stack.

Analogous to nova_adaptive/ but operates on ZINC15/SAVI molecular SMILES
space instead of reaction combinatorial DB.  All five modules are independent
and can be imported without Boltz2 or Solana present.

Modules
-------
life_pulse     Sobol quasi-random sweep over ZINC15/SAVI molecular vocabulary.
life_art       RandomForest affinity predictor trained on Morgan fingerprints
               + real Boltz2 scores.  (optional — stub not yet written)
life_scout     Protein-family-aware candidate routing and batch generation.
               (optional — stub not yet written)
life_diversity Shannon entropy enforcement + Tanimoto deduplication.
life_generate  Generative AI (Phase 4): fragment recombination, scaffold
               hopping, and guided mutation from top Boltz2 molecules.

Import policy: each module is imported inside its own try/except so a missing
or broken submodule does NOT prevent the rest of the package from loading.
miner_daemon.py guards _PULSE_AVAILABLE / _TOOLS_AVAILABLE itself.
"""

# ── life_pulse (always available — core sweep) ────────────────────────────────
try:
    from .life_pulse import run_sweep, get_next_candidates, proxy_score
except Exception as _e:  # noqa: BLE001
    import warnings
    warnings.warn(f"adaptive.life_pulse unavailable: {_e}", ImportWarning, stacklevel=2)
    run_sweep = get_next_candidates = proxy_score = None  # type: ignore[assignment]

# ── life_art (optional — not yet implemented) ─────────────────────────────────
try:
    from .life_art import train as art_train, rank_candidates, load_model
except ImportError:
    art_train = rank_candidates = load_model = None  # type: ignore[assignment]

# ── life_scout (optional — not yet implemented) ───────────────────────────────
try:
    from .life_scout import get_focused_candidates, detect_protein_family
except ImportError:
    get_focused_candidates = detect_protein_family = None  # type: ignore[assignment]

# ── life_diversity ────────────────────────────────────────────────────────────
try:
    from .life_diversity import (
        SubmissionMemory,
        greedy_diverse_select,
        batch_shannon_entropy,
        is_novel,
    )
except Exception as _e:  # noqa: BLE001
    import warnings
    warnings.warn(f"adaptive.life_diversity unavailable: {_e}", ImportWarning, stacklevel=2)
    SubmissionMemory = greedy_diverse_select = batch_shannon_entropy = is_novel = None  # type: ignore[assignment]

# ── life_generate ─────────────────────────────────────────────────────────────
try:
    from .life_generate import (
        generate_candidates,
        is_boltz_safe_smiles,
        fragment_recombination,
        scaffold_hopping,
        guided_mutation,
    )
except Exception as _e:  # noqa: BLE001
    import warnings
    warnings.warn(f"adaptive.life_generate unavailable: {_e}", ImportWarning, stacklevel=2)
    generate_candidates = is_boltz_safe_smiles = None  # type: ignore[assignment]
    fragment_recombination = scaffold_hopping = guided_mutation = None  # type: ignore[assignment]

# ── life_chembl ───────────────────────────────────────────────────────────────
try:
    from .life_chembl import (
        download_chembl_actives,
        get_chembl_actives,
        validate_against_chembl,
        get_chembl_seeds,
        download_all as chembl_download_all,
    )
except Exception as _e:  # noqa: BLE001
    import warnings
    warnings.warn(f"adaptive.life_chembl unavailable: {_e}", ImportWarning, stacklevel=2)
    download_chembl_actives = get_chembl_actives = validate_against_chembl = None  # type: ignore[assignment]
    get_chembl_seeds = chembl_download_all = None  # type: ignore[assignment]

# ── life_proteinnet (per-protein ML pre-screener) ─────────────────────────────
try:
    from .life_proteinnet import (
        train_all       as proteinnet_train_all,
        pre_screen      as proteinnet_pre_screen,
        get_model_report,
        should_retrain  as proteinnet_should_retrain,
    )
except Exception as _e:  # noqa: BLE001
    import warnings
    warnings.warn(f"adaptive.life_proteinnet unavailable: {_e}", ImportWarning, stacklevel=2)
    proteinnet_train_all = proteinnet_pre_screen = None  # type: ignore[assignment]
    get_model_report = proteinnet_should_retrain = None  # type: ignore[assignment]

__all__ = [
    "run_sweep", "get_next_candidates", "proxy_score",
    "art_train", "rank_candidates", "load_model",
    "get_focused_candidates", "detect_protein_family",
    "SubmissionMemory", "greedy_diverse_select", "batch_shannon_entropy", "is_novel",
    "generate_candidates", "is_boltz_safe_smiles",
    "fragment_recombination", "scaffold_hopping", "guided_mutation",
    "download_chembl_actives", "get_chembl_actives", "validate_against_chembl",
    "get_chembl_seeds", "chembl_download_all",
    "proteinnet_train_all", "proteinnet_pre_screen", "get_model_report",
    "proteinnet_should_retrain",
]

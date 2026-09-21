"""
life_peg.py — Prime-editing (pegRNA) candidate design + feature extraction.

STAGE 1 SCOPE: feature extraction ONLY.  There is deliberately no scoring
model, no LIFE-BRAIN wiring, no miner/validator integration and no on-chain
surface in this module.  Nothing here imports from or mutates production code.

What a pegRNA is, in the terms this module uses
-----------------------------------------------
A prime editor is a Cas9 H840A *nickase* fused to a reverse transcriptase.
The pegRNA carries four functional parts:

    spacer (20nt)  - targets the protospacer, exactly like a normal gRNA.
                     Requires an NGG PAM immediately 3' of the protospacer.
    scaffold       - constant Cas9-binding region.  Carries no design signal,
                     so it is not featurised.
    RT template    - copied by the RT into the nicked strand.  This is what
      (RTT)          physically encodes the edit.
    PBS            - primer binding site; anneals to the nicked 3' flap and
                     primes reverse transcription.

Cas9 nicks the PAM-containing strand between protospacer positions 17 and 18,
i.e. 3nt 5' of the PAM.  The RTT must therefore start at the nick and extend
far enough downstream to cover the edit *plus* homology beyond it.

Feature set provenance
----------------------
Reproduces the published PRIDICT / DeepPrime / DTMP-Prime feature families:
RTT/PBS/correction length, GC content, melting temperature, maximum
homopolymer stretch, secondary-structure energy, and positional features
(nick-to-edit distance, edit-to-RTT-3'-end flank).  It does NOT reproduce
their trained weights - only the input representation, which is public.

Determinism contract (matters for Stage 3)
------------------------------------------
Pure Python + stdlib.  No numpy, no ViennaRNA, no RNG, no dict-ordering
dependence, no floating-point reduction whose order could vary.  Stage 3
requires the validator to recompute these vectors bit-identically to the
miner; a native library (ViennaRNA) would make that contingent on both sides
having the same C build, so the structure terms use an explicit, fully
specified nearest-neighbour proxy instead.  Every returned value is a plain
Python float.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parents[1]
PEG_REFERENCE_DIR = REPO / "data" / "peg_reference"

# ── Design-space bounds ───────────────────────────────────────────────────────
# Ranges follow the published pegRNA design space.  DeepPrime was trained on
# 259K pegRNAs with PBS lengths 1-17, RT-template lengths 1-50 and edit
# positions 1-30; PRIDICT uses the same families.  The lower bounds here are
# tighter than the training set because very short PBS/RTT cannot physically
# prime or cover an edit plus homology flank.
SPACER_LEN = 20
PBS_MIN, PBS_MAX = 8, 17
RTT_MIN, RTT_MAX = 10, 50
NICK_OFFSET_FROM_PAM = 3        # Cas9 nicks 3nt 5' of the PAM
MIN_FLANK_AFTER_EDIT = 5        # homology required 3' of the edit inside RTT
MAX_NICK_TO_EDIT = 30           # matches the published edit-position range

EDIT_TYPES = ("substitution", "insertion", "deletion")

_COMP = str.maketrans("ACGTN", "TGCAN")
_VALID_DNA = frozenset("ACGT")


def revcomp(seq: str) -> str:
    return seq.translate(_COMP)[::-1]


# ══════════════════════════════════════════════════════════════════════════════
# Sequence primitives
# ══════════════════════════════════════════════════════════════════════════════

def gc_content(seq: str) -> float:
    """G+C fraction.  Empty sequence returns 0.0 rather than raising."""
    if not seq:
        return 0.0
    return (seq.count("G") + seq.count("C")) / len(seq)


def base_fractions(seq: str) -> tuple[float, float, float, float]:
    """(A, C, G, T) fractions.  Fixed order; never dict iteration order."""
    if not seq:
        return (0.0, 0.0, 0.0, 0.0)
    n = len(seq)
    return (seq.count("A") / n, seq.count("C") / n,
            seq.count("G") / n, seq.count("T") / n)


def max_homopolymer(seq: str) -> int:
    """Longest single-base run.  PRIDICT tracks max polyA/T/G/C stretch."""
    if not seq:
        return 0
    best = run = 1
    for i in range(1, len(seq)):
        run = run + 1 if seq[i] == seq[i - 1] else 1
        if run > best:
            best = run
    return best


def melting_temp(seq: str) -> float:
    """
    Melting temperature in degrees C.

    Wallace rule (2*(A+T) + 4*(G+C)) below 14nt, where nearest-neighbour
    models are unreliable; the standard salt-adjusted GC formula at and above
    14nt.  Both are closed-form and deterministic.  Empty -> 0.0.
    """
    if not seq:
        return 0.0
    n = len(seq)
    at = seq.count("A") + seq.count("T")
    gc = seq.count("G") + seq.count("C")
    if n < 14:
        return float(2 * at + 4 * gc)
    return 64.9 + 41.0 * (gc - 16.4) / n


# ── Secondary-structure proxy ────────────────────────────────────────────────
# Deterministic stand-in for ViennaRNA MFE.  Not a claim to reproduce Vienna's
# number - it is a monotone stability proxy: more, longer, more GC-rich
# self-complementary stems => more negative score.  Fully specified here so
# miner and validator cannot drift (Stage 3 requirement).

_STACK_ENERGY = {   # kcal/mol, sign-negative = stabilising
    ("G", "C"): -3.0, ("C", "G"): -3.0,
    ("A", "T"): -2.0, ("T", "A"): -2.0,
    ("G", "T"): -1.0, ("T", "G"): -1.0,   # wobble, RNA context
}

_MIN_STEM = 4        # shortest hairpin stem counted
_MIN_LOOP = 3        # sterically required minimum loop


def _pairs(a: str, b: str) -> float:
    return _STACK_ENERGY.get((a, b), 0.0)


def fold_energy_proxy(seq: str) -> float:
    """
    Best single-hairpin stability over all (stem, loop) placements.

    Scans every stem start i, every stem end j with a >= _MIN_LOOP gap, and
    extends the stem while bases pair.  Returns the most negative total stack
    energy found, or 0.0 for an unstructured sequence.  O(n^2) with n <= ~40
    here, so cost is negligible.
    """
    n = len(seq)
    if n < 2 * _MIN_STEM + _MIN_LOOP:
        return 0.0
    best = 0.0
    for i in range(n):
        for j in range(n - 1, i + _MIN_LOOP, -1):
            energy = 0.0
            stem = 0
            while (i + stem < j - stem
                   and (j - stem) - (i + stem) > _MIN_LOOP):
                e = _pairs(seq[i + stem], seq[j - stem])
                if e == 0.0:
                    break
                energy += e
                stem += 1
            if stem >= _MIN_STEM and energy < best:
                best = energy
    return best


def self_complementarity(seq: str) -> float:
    """
    Fraction of 5' bases that Watson-Crick pair with the mirrored 3' base.

    Cheap global hairpin-propensity term, complementary to the local stem
    search in fold_energy_proxy.  Window is min(5, len//2).
    """
    if len(seq) < 4:
        return 0.0
    w = min(5, len(seq) // 2)
    comp = {"A": "T", "T": "A", "C": "G", "G": "C"}
    hits = sum(1 for i in range(w) if comp.get(seq[i]) == seq[-(i + 1)])
    return hits / w


def duplex_complementarity(a: str, b: str) -> float:
    """
    Best ungapped complementarity between two sequences, as a fraction of the
    shorter one.  Used for spacer-vs-extension interference, a known cause of
    pegRNA self-inactivation.
    """
    if not a or not b:
        return 0.0
    rb = revcomp(b)
    short = min(len(a), len(rb))
    best = 0
    for off in range(-(len(rb) - 1), len(a)):
        run = 0
        for i in range(len(a)):
            k = i - off
            if 0 <= k < len(rb) and a[i] == rb[k]:
                run += 1
        if run > best:
            best = run
    return best / short


# ══════════════════════════════════════════════════════════════════════════════
# Stage 0.1 reference data
# ══════════════════════════════════════════════════════════════════════════════

_ref_cache: dict[str, dict] = {}


def load_gene_reference(gene: str) -> Optional[dict]:
    """Load a Stage 0.1 gene record.  Returns None if absent."""
    gene = gene.upper()
    if gene in _ref_cache:
        return _ref_cache[gene]
    path = PEG_REFERENCE_DIR / f"{gene}.json"
    if not path.exists():
        return None
    rec = json.loads(path.read_text())
    _ref_cache[gene] = rec
    return rec


def load_reference_index() -> Optional[dict]:
    path = PEG_REFERENCE_DIR / "index.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_reference_genes() -> list[str]:
    idx = load_reference_index()
    return sorted(idx["genes"]) if idx else []


def get_hotspot(gene: str, label: str) -> Optional[dict]:
    rec = load_gene_reference(gene)
    if not rec:
        return None
    for h in rec["hotspots"]:
        if h["label"] == label:
            return h
    return None


def hotspot_positions(gene: str) -> list[int]:
    """Genomic edit coordinates of every validated hotspot for *gene*."""
    rec = load_gene_reference(gene)
    if not rec:
        return []
    return [h["genomic_codon_start"] for h in rec["hotspots"]]


def distance_to_nearest_hotspot(gene: str, genomic_pos: int) -> Optional[int]:
    """
    Base-pair distance from *genomic_pos* to the nearest validated hotspot of
    *gene*.  None when the gene has no reference entry, so callers can tell
    "no data" apart from "distance 0".
    """
    positions = hotspot_positions(gene)
    if not positions:
        return None
    return min(abs(genomic_pos - p) for p in positions)


# ══════════════════════════════════════════════════════════════════════════════
# Candidate representation
# ══════════════════════════════════════════════════════════════════════════════

class PegCandidate:
    """
    A concrete pegRNA design against a concrete genomic window.

    Every coordinate is an offset into `window`, so a candidate is fully
    self-describing and can be re-verified without re-reading the reference.
    """

    __slots__ = (
        "gene", "hotspot_label", "window", "strand",
        "spacer", "pbs", "rtt", "edit_type",
        "pam_offset", "nick_offset", "edit_offset",
        "wt_seq", "edited_seq", "genomic_edit_pos",
    )

    def __init__(self, *, gene: str, hotspot_label: str, window: str,
                 strand: str, spacer: str, pbs: str, rtt: str,
                 edit_type: str, pam_offset: int, nick_offset: int,
                 edit_offset: int, wt_seq: str, edited_seq: str,
                 genomic_edit_pos: int) -> None:
        self.gene = gene
        self.hotspot_label = hotspot_label
        self.window = window
        self.strand = strand
        self.spacer = spacer
        self.pbs = pbs
        self.rtt = rtt
        self.edit_type = edit_type
        self.pam_offset = pam_offset
        self.nick_offset = nick_offset
        self.edit_offset = edit_offset
        self.wt_seq = wt_seq
        self.edited_seq = edited_seq
        self.genomic_edit_pos = genomic_edit_pos

    # ── Derived geometry ────────────────────────────────────────────────────
    @property
    def nick_to_edit(self) -> int:
        """Bases from the nick to the first edited base."""
        return self.edit_offset - self.nick_offset

    @property
    def correction_length(self) -> int:
        """Number of bases altered (insertion/deletion use the size change)."""
        if self.edit_type == "substitution":
            return sum(1 for a, b in zip(self.wt_seq, self.edited_seq) if a != b)
        return abs(len(self.edited_seq) - len(self.wt_seq))

    @property
    def edit_to_rtt_end(self) -> int:
        """Homology flank between the end of the edit and the RTT 3' end."""
        edit_end = self.nick_to_edit + max(1, len(self.edited_seq))
        return len(self.rtt) - edit_end

    @property
    def extension(self) -> str:
        """The 3' extension as synthesised: RTT then PBS."""
        return self.rtt + self.pbs

    def to_dict(self) -> dict:
        return {
            "gene": self.gene,
            "hotspot_label": self.hotspot_label,
            "strand": self.strand,
            "spacer": self.spacer,
            "pbs": self.pbs,
            "rtt": self.rtt,
            "edit_type": self.edit_type,
            "pam_offset": self.pam_offset,
            "nick_offset": self.nick_offset,
            "edit_offset": self.edit_offset,
            "nick_to_edit": self.nick_to_edit,
            "correction_length": self.correction_length,
            "edit_to_rtt_end": self.edit_to_rtt_end,
            "wt_seq": self.wt_seq,
            "edited_seq": self.edited_seq,
            "genomic_edit_pos": self.genomic_edit_pos,
        }

    def __repr__(self) -> str:
        return (f"PegCandidate({self.gene}/{self.hotspot_label} "
                f"{self.edit_type} PBS={len(self.pbs)} RTT={len(self.rtt)} "
                f"nick_to_edit={self.nick_to_edit})")


# ══════════════════════════════════════════════════════════════════════════════
# Candidate design
# ══════════════════════════════════════════════════════════════════════════════

def find_pam_sites(window: str, near_offset: int,
                   max_distance: int = MAX_NICK_TO_EDIT) -> list[int]:
    """
    Offsets of real NGG PAMs on the + strand of *window* whose resulting nick
    lies 5' of *near_offset* and within *max_distance* of it.

    Returns the PAM's first base (the ambiguous N).  Only genuine NGG matches
    present in the sequence are returned; nothing is synthesised.
    """
    out = []
    for i in range(len(window) - 2):
        if window[i + 1] == "G" and window[i + 2] == "G":
            nick = i - NICK_OFFSET_FROM_PAM
            if nick < SPACER_LEN:
                continue                       # no room for a 20nt spacer
            if not (0 < near_offset - nick <= max_distance):
                continue                       # edit must be 3' of the nick
            out.append(i)
    return out


def _apply_edit(window: str, edit_offset: int, edit_type: str,
                new_seq: str, wt_len: int) -> tuple[str, str]:
    """Return (wt_seq, edited_seq) for the affected span."""
    wt = window[edit_offset:edit_offset + wt_len]
    if edit_type == "substitution":
        return wt, new_seq
    if edit_type == "insertion":
        return "", new_seq
    if edit_type == "deletion":
        return wt, ""
    raise ValueError(f"unknown edit_type: {edit_type}")


def design_candidates(gene: str, hotspot_label: str, *,
                      edit_type: str = "substitution",
                      new_seq: Optional[str] = None,
                      wt_len: int = 3,
                      pbs_lengths: Iterable[int] = range(PBS_MIN, PBS_MAX + 1),
                      rtt_lengths: Iterable[int] = range(RTT_MIN, RTT_MAX + 1),
                      search_both_strands: bool = True,
                      ) -> list[PegCandidate]:
    """
    Enumerate geometrically valid pegRNA candidates for one Stage 0.1 hotspot.

    Both DNA strands are searched by default.  A prime editor may nick either
    strand; that choice is independent of which strand the CDS lies on, and
    restricting the search to the + strand silently discards most of the real
    design space (TERT's window, for example, carries 44 NGG sites on the -
    strand against 8 on the +).

    Combinations that cannot physically work - no real PAM in reach, edit 5'
    of the nick, RTT too short to cover the edit plus flank - are rejected.
    Nothing is fabricated to fill the grid; a hotspot with no usable PAM on
    either strand yields an empty list.

    edit_type semantics
      substitution : `new_seq` replaces `wt_len` bases at the hotspot
      insertion    : `new_seq` is inserted, nothing removed
      deletion     : `wt_len` bases removed, `new_seq` ignored
    """
    if edit_type not in EDIT_TYPES:
        raise ValueError(f"edit_type must be one of {EDIT_TYPES}")

    h = get_hotspot(gene, hotspot_label)
    if h is None:
        return []

    window = h["window"]
    edit_offset = h["edit_offset_in_window"]

    if edit_type == "substitution" and new_seq is None:
        # Default: transition at the first base of the hotspot codon.  A real
        # change, derived from the reference base - never a random letter.
        wt_base = window[edit_offset]
        transition = {"A": "G", "G": "A", "C": "T", "T": "C"}
        new_seq = transition[wt_base] + window[edit_offset + 1:edit_offset + wt_len]
    elif edit_type == "insertion" and new_seq is None:
        new_seq = "A"
    if new_seq is None:
        new_seq = ""

    wt_seq, edited_seq = _apply_edit(window, edit_offset, edit_type,
                                     new_seq, wt_len)

    candidates = _design_on_strand(
        gene, hotspot_label, h, window, edit_offset,
        wt_seq, edited_seq, edit_type, "+", pbs_lengths, rtt_lengths)

    if search_both_strands:
        # Re-express the same edit in reverse-complement coordinates and reuse
        # the identical geometry logic.  The edit itself is unchanged; only the
        # frame of reference flips.
        rc_window = revcomp(window)
        rc_edit_offset = len(window) - (edit_offset + len(wt_seq))
        if edit_type == "insertion":
            rc_edit_offset = len(window) - edit_offset
        candidates += _design_on_strand(
            gene, hotspot_label, h, rc_window, rc_edit_offset,
            revcomp(wt_seq), revcomp(edited_seq), edit_type, "-",
            pbs_lengths, rtt_lengths)

    return candidates


def _design_on_strand(gene: str, hotspot_label: str, h: dict,
                      window: str, edit_offset: int,
                      wt_seq: str, edited_seq: str, edit_type: str,
                      strand: str,
                      pbs_lengths: Iterable[int],
                      rtt_lengths: Iterable[int]) -> list[PegCandidate]:
    """
    Enumerate candidates against one strand's view of the window.

    `window` and `edit_offset` are already expressed in that strand's
    coordinates, so this routine is strand-agnostic and is shared by both
    passes.  `strand` is recorded on the candidate for reporting only.
    """
    pbs_lengths = list(pbs_lengths)
    rtt_lengths = list(rtt_lengths)
    candidates: list[PegCandidate] = []

    for pam_off in find_pam_sites(window, edit_offset):
        nick = pam_off - NICK_OFFSET_FROM_PAM
        spacer = window[nick - SPACER_LEN:nick]
        if len(spacer) != SPACER_LEN or not set(spacer) <= _VALID_DNA:
            continue

        nick_to_edit = edit_offset - nick
        replaced = len(wt_seq)
        inserted = len(edited_seq)

        for rtt_len in rtt_lengths:
            # RTT must reach past the edit with real homology beyond it.
            if rtt_len < nick_to_edit + inserted + MIN_FLANK_AFTER_EDIT:
                continue
            # Genomic span the RTT copies, accounting for indel size change.
            genomic_span = rtt_len - inserted + replaced
            if nick + genomic_span > len(window):
                continue

            rtt_genomic = window[nick:nick + genomic_span]
            local = edit_offset - nick
            rtt_edited = (rtt_genomic[:local] + edited_seq
                          + rtt_genomic[local + replaced:])
            if len(rtt_edited) != rtt_len:
                continue
            # The RTT is synthesised as the reverse complement of the strand
            # it templates.
            rtt = revcomp(rtt_edited)

            for pbs_len in pbs_lengths:
                if nick - pbs_len < 0:
                    continue
                pbs = revcomp(window[nick - pbs_len:nick])
                if not set(pbs) <= _VALID_DNA:
                    continue
                candidates.append(PegCandidate(
                    gene=gene, hotspot_label=hotspot_label, window=window,
                    strand=strand, spacer=spacer, pbs=pbs, rtt=rtt,
                    edit_type=edit_type, pam_offset=pam_off,
                    nick_offset=nick, edit_offset=edit_offset,
                    wt_seq=wt_seq, edited_seq=edited_seq,
                    genomic_edit_pos=h["genomic_codon_start"],
                ))
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# Feature extraction
# ══════════════════════════════════════════════════════════════════════════════

# Fixed feature layout.  Stage 3's symmetry harness pins N_PEG_FEATURES, so
# any change here is a breaking change on BOTH miner and validator sides.
PEG_FEATURE_NAMES: tuple[str, ...] = (
    # 0-7  spacer
    "spacer_len", "spacer_gc", "spacer_a", "spacer_c", "spacer_g", "spacer_t",
    "spacer_tm", "spacer_homopolymer",
    # 8-15  PBS
    "pbs_len", "pbs_gc", "pbs_a", "pbs_c", "pbs_g", "pbs_t",
    "pbs_tm", "pbs_homopolymer",
    # 16-23  RT template
    "rtt_len", "rtt_gc", "rtt_a", "rtt_c", "rtt_g", "rtt_t",
    "rtt_tm", "rtt_homopolymer",
    # 24-26  edit type one-hot
    "edit_substitution", "edit_insertion", "edit_deletion",
    # 27-31  edit geometry
    "correction_len", "nick_to_edit", "edit_to_rtt_end",
    "rtt_pbs_ratio", "edit_rel_pos_in_rtt",
    # 32-36  structure
    "ext_fold_energy", "ext_self_comp", "spacer_ext_comp",
    "rtt_starts_c", "pbs_gc_clamp",
    # 37-42  target context from Stage 0.1 reference
    "hotspot_dist", "hotspot_exact", "hotspot_is_coding",
    "pam_density", "pam_to_edit", "strand_plus",
)

N_PEG_FEATURES = len(PEG_FEATURE_NAMES)

# Normalisation constants.  Explicit so both sides scale identically.
_TM_SCALE = 100.0
_LEN_SCALE = 30.0
_HOMOPOLYMER_SCALE = 10.0
_FOLD_SCALE = 30.0
_HOTSPOT_DIST_SCALE = 1000.0
_NICK_EDIT_SCALE = float(MAX_NICK_TO_EDIT)

# Sentinel for "context not supplied".  Out of band for every feature that
# uses it (all are non-negative when known), so a model can separate absent
# from zero.  The vector length never changes.
_ABSENT = -1.0


def _seq_block(seq: str) -> list[float]:
    """The 8 per-segment features, in fixed order, for one sequence."""
    a, c, g, t = base_fractions(seq)
    return [
        len(seq) / _LEN_SCALE,
        gc_content(seq),
        a, c, g, t,
        melting_temp(seq) / _TM_SCALE,
        max_homopolymer(seq) / _HOMOPOLYMER_SCALE,
    ]


def featurize_pegrna(spacer: str, pbs: str, rtt: str, edit_type: str, *,
                     gene: Optional[str] = None,
                     genomic_edit_pos: Optional[int] = None,
                     nick_to_edit: Optional[int] = None,
                     correction_length: Optional[int] = None,
                     edit_to_rtt_end: Optional[int] = None,
                     window: Optional[str] = None,
                     edit_offset: Optional[int] = None,
                     pam_offset: Optional[int] = None,
                     strand: str = "+",
                     ) -> Optional[list[float]]:
    """
    Return the fixed-length pegRNA feature vector, or None if inputs are
    invalid.

    Required: spacer / pbs / rtt / edit_type.  Everything else is optional
    context; when a context argument is absent the corresponding feature takes
    an explicit neutral value rather than being silently dropped, so the
    vector length is invariant.

    Returns plain Python floats only.  No RNG, no numpy, no I/O beyond the
    cached Stage 0.1 reference read.
    """
    spacer = (spacer or "").upper()
    pbs = (pbs or "").upper()
    rtt = (rtt or "").upper()

    if edit_type not in EDIT_TYPES:
        return None
    if not all(s and set(s) <= _VALID_DNA for s in (spacer, pbs, rtt)):
        return None

    feats: list[float] = []
    feats += _seq_block(spacer)                                    # 0-7
    feats += _seq_block(pbs)                                       # 8-15
    feats += _seq_block(rtt)                                       # 16-23

    # 24-26  edit type one-hot
    feats += [1.0 if edit_type == t else 0.0 for t in EDIT_TYPES]

    # 27-31  edit geometry
    corr = float(correction_length) if correction_length is not None else 1.0
    n2e = float(nick_to_edit) if nick_to_edit is not None else _ABSENT
    e2e = float(edit_to_rtt_end) if edit_to_rtt_end is not None else _ABSENT
    feats += [
        corr / _LEN_SCALE,
        n2e / _NICK_EDIT_SCALE,
        e2e / _LEN_SCALE,
        len(rtt) / len(pbs),
        n2e / len(rtt) if n2e >= 0.0 else _ABSENT,
    ]

    # 32-36  structure
    extension = rtt + pbs
    feats += [
        fold_energy_proxy(extension) / _FOLD_SCALE,
        self_complementarity(extension),
        duplex_complementarity(spacer, extension),
        # A C at the RTT 5' end is a known PRIDICT efficiency determinant
        # (it disfavours the 5' flap being excised).
        1.0 if rtt[0] == "C" else 0.0,
        gc_content(pbs[-3:]),
    ]

    # 37-42  target context from Stage 0.1 reference
    hot_dist, hot_exact, hot_coding = _ABSENT, 0.0, _ABSENT
    if gene is not None and genomic_edit_pos is not None:
        d = distance_to_nearest_hotspot(gene, genomic_edit_pos)
        if d is not None:
            hot_dist = min(d / _HOTSPOT_DIST_SCALE, 1.0)
            hot_exact = 1.0 if d == 0 else 0.0
        rec = load_gene_reference(gene)
        for h in (rec["hotspots"] if rec else []):
            if h["genomic_codon_start"] == genomic_edit_pos:
                hot_coding = 1.0 if h["hotspot_source"] == "cds_codon" else 0.0
                break

    pam_density, pam_to_edit = _ABSENT, _ABSENT
    if window and edit_offset is not None:
        lo = max(0, edit_offset - MAX_NICK_TO_EDIT)
        hi = min(len(window) - 2, edit_offset + MAX_NICK_TO_EDIT)
        pam_density = sum(1 for i in range(lo, hi)
                          if window[i + 1] == "G" and window[i + 2] == "G") / 10.0
        if pam_offset is not None:
            pam_to_edit = abs(edit_offset - pam_offset) / _NICK_EDIT_SCALE

    feats += [hot_dist, hot_exact, hot_coding,
              pam_density, pam_to_edit,
              1.0 if strand == "+" else 0.0]

    assert len(feats) == N_PEG_FEATURES, \
        f"expected {N_PEG_FEATURES} features, built {len(feats)}"
    return [float(x) for x in feats]


def featurize_candidate(cand: PegCandidate) -> Optional[list[float]]:
    """featurize_pegrna() with every context field supplied from *cand*."""
    return featurize_pegrna(
        cand.spacer, cand.pbs, cand.rtt, cand.edit_type,
        gene=cand.gene,
        genomic_edit_pos=cand.genomic_edit_pos,
        nick_to_edit=cand.nick_to_edit,
        correction_length=cand.correction_length,
        edit_to_rtt_end=cand.edit_to_rtt_end,
        window=cand.window,
        edit_offset=cand.edit_offset,
        pam_offset=cand.pam_offset,
        strand=cand.strand,
    )


def feature_dict(vector: list[float]) -> dict[str, float]:
    """Name -> value, for inspection and debugging."""
    return dict(zip(PEG_FEATURE_NAMES, vector))

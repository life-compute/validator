"""The Boltz 2.2.1 `predict` option surface, pinned.

Why this file exists
--------------------
The Worker publishes its scorer profile as an OBJECT of bare option names
(`{"recycling_steps": 1, "use_msa_server": false}`).  Turning that into argv
is where a spelling or form mistake becomes an INTERNAL_ERROR 20 minutes
into a GPU job, or — far worse — a silently different scorer.

So every token we are about to hand to `boltz.main.predict` is checked
against this table FIRST, in-process, with no boltz import (the client
package stays unit-testable without a GPU).  `tests/test_boltz_profile_args.py`
re-derives the table from the real `boltz predict --help` of the installed
interpreter and fails if the two ever disagree.

Form matters as much as spelling:

* SWITCH — a click flag with `is_flag=True`.  It is present or absent and
  takes NO value.  `--no_kernels true` is wrong: click would read `true` as
  the positional DATA argument.
* VALUE  — takes exactly one argument, emitted as two argv tokens
  (`--recycling_steps 1`).  The `=` form is also accepted by click, but we
  always emit the two-token form so the hashed flag record is canonical.

Note the underscores: Boltz 2.2.1 spells these `--recycling_steps`,
`--use_msa_server`.  A hyphenated `--use-msa-server` is NOT a Boltz 2.2.1
option (that was the Boltz-1.0.1 era spelling carried in the old default
profile) and is rejected here instead of reaching the GPU.
"""
from __future__ import annotations

SWITCH = "switch"
VALUE = "value"

# name -> SWITCH | VALUE, verbatim from `boltz predict --help` (boltz 2.2.1).
OPTION_SPEC: dict[str, str] = {
    "out_dir": VALUE,
    "cache": VALUE,
    "checkpoint": VALUE,
    "devices": VALUE,
    "accelerator": VALUE,
    "recycling_steps": VALUE,
    "sampling_steps": VALUE,
    "diffusion_samples": VALUE,
    "max_parallel_samples": VALUE,
    "step_scale": VALUE,
    "write_full_pae": SWITCH,
    "write_full_pde": SWITCH,
    "output_format": VALUE,
    "num_workers": VALUE,
    "override": SWITCH,
    "seed": VALUE,
    "use_msa_server": SWITCH,
    "msa_server_url": VALUE,
    "msa_pairing_strategy": VALUE,
    "msa_server_username": VALUE,
    "msa_server_password": VALUE,
    "api_key_header": VALUE,
    "api_key_value": VALUE,
    "use_potentials": SWITCH,
    "model": VALUE,
    "method": VALUE,
    "preprocessing-threads": VALUE,
    "affinity_mw_correction": SWITCH,
    "sampling_steps_affinity": VALUE,
    "diffusion_samples_affinity": VALUE,
    "affinity_checkpoint": VALUE,
    "max_msa_seqs": VALUE,
    "subsample_msa": SWITCH,
    "num_subsampled_msa": VALUE,
    "no_kernels": SWITCH,
    "write_embeddings": SWITCH,
}

# The runner supplies these itself; a profile must not also set them or the
# two would fight (and `--out_dir` would point outside the temp sandbox).
RUNNER_OWNED = frozenset({"out_dir", "seed"})

# Choice options, so a wrong value is caught here and not 20 minutes in.
CHOICES: dict[str, frozenset[str]] = {
    "accelerator": frozenset({"gpu", "cpu", "tpu"}),
    "output_format": frozenset({"pdb", "mmcif"}),
    "model": frozenset({"boltz1", "boltz2"}),
}


class BoltzArgError(ValueError):
    """A built argv token list is not valid for Boltz 2.2.1."""


def validate_tokens(tokens) -> None:
    """Fail closed on anything `boltz predict` would not accept.

    Checks spelling, form (switch vs value), choice membership, duplicates,
    stray positionals, and runner-owned options.
    """
    toks = [str(t) for t in tokens]
    seen: set[str] = set()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok.startswith("--"):
            raise BoltzArgError(
                f"stray positional token {tok!r} — click would read it as the "
                f"DATA argument ('Got unexpected extra arguments')")
        name, eq, inline = tok[2:].partition("=")
        if name not in OPTION_SPEC:
            near = [k for k in OPTION_SPEC if k.replace("_", "-") == name
                    or k == name.replace("-", "_")]
            hint = f" — did you mean --{near[0]}?" if near else ""
            raise BoltzArgError(f"unknown Boltz 2.2.1 option --{name}{hint}")
        if name in RUNNER_OWNED:
            raise BoltzArgError(
                f"--{name} is set by the runner and must not come from the profile")
        if name in seen:
            raise BoltzArgError(f"--{name} given more than once")
        seen.add(name)

        form = OPTION_SPEC[name]
        if form == SWITCH:
            if eq:
                raise BoltzArgError(
                    f"--{name} is a switch and takes no value (got {tok!r})")
            i += 1
            continue

        if eq:
            value = inline
            i += 1
        else:
            if i + 1 >= len(toks) or toks[i + 1].startswith("--"):
                raise BoltzArgError(f"--{name} takes a value but none was given")
            value = toks[i + 1]
            i += 2
        allowed = CHOICES.get(name)
        if allowed is not None and value not in allowed:
            raise BoltzArgError(
                f"--{name} must be one of {sorted(allowed)}, got {value!r}")

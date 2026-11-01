"""The Worker's published scorer profile must build a VALID Boltz 2.2.1 argv.

Two independent halves, both required:

1. `test_option_spec_matches_installed_boltz_help` re-derives the option
   table from the REAL `boltz predict --help` of the interpreter this suite
   runs under, and fails if `coordinator_client.boltzspec.OPTION_SPEC`
   disagrees about a name OR about its form (switch vs value-taking).  That
   is the only check that can notice a Boltz upgrade renaming an option.

2. Everything else is pure: it takes the profile the live Worker publishes
   (profile_version 1.1.0, frozen verbatim in
   `tests/data/worker_scorer_profiles_1_1_0.json`), builds the argv the
   validator would hand to `predict.main`, and asserts each flag's spelling,
   form and value — plus the locked numbers (recycling 1, sampling 25,
   affinity diffusion samples 1, subsampled MSA 1024, seed 68, no MSA
   server).

No GPU, no network, no keypair.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_client import boltzspec
from coordinator_client.contract import ContractError, ScorerProfile
from coordinator_client.rescore import RescoreFailed, boltz_flags_for, seed_for

DATA = Path(__file__).parent / "data"
PUBLISHED = json.loads((DATA / "worker_scorer_profiles_1_1_0.json").read_text())
PROFILES = PUBLISHED["scorer_profiles"]


def profile(modality: str) -> ScorerProfile:
    return ScorerProfile.from_dict(PROFILES[modality])


def argv(modality: str, has_msa: bool = True) -> list[str]:
    return boltz_flags_for(profile(modality), modality, has_msa, 1024)


def pairs(tokens: list[str]) -> dict[str, str | None]:
    """argv -> {option: value}, value None for a switch.  Mirrors click."""
    out: dict[str, str | None] = {}
    i = 0
    while i < len(tokens):
        name = tokens[i][2:]
        assert tokens[i].startswith("--"), f"stray positional {tokens[i]!r}"
        if boltzspec.OPTION_SPEC[name] == boltzspec.SWITCH:
            out[name] = None
            i += 1
        else:
            out[name] = tokens[i + 1]
            i += 2
    return out


# ── 1. the spec vs the installed Boltz's own help text ──────────────────────

def _help_text() -> str:
    boltz_main = pytest.importorskip("boltz.main")
    import click
    cmd = boltz_main.predict
    assert isinstance(cmd, click.Command)
    with click.Context(cmd, info_name="boltz predict") as ctx:
        return cmd.get_help(ctx)


def parse_help_options(help_text: str) -> dict[str, str]:
    """{option name: switch|value} from a click help listing.

    An option line is `  --name [METAVAR]  description`.  click prints a
    METAVAR only for value-taking options, and it is always either an
    ALL-CAPS token (PATH, INTEGER, TEXT, FLOAT) or a `[a|b|c]` choice list.
    A switch has no metavar, so the text right after the name is prose.
    """
    metavar = re.compile(r"^(?:\[[^\]]+\]|[A-Z][A-Z0-9_]*)$")
    spec: dict[str, str] = {}
    for line in help_text.splitlines():
        m = re.match(r"^\s{2}(--[A-Za-z0-9_\-]+)(?:\s+(\S+))?", line)
        if not m:
            continue
        name = m.group(1)[2:]
        if name == "help":
            continue
        head = m.group(2) or ""
        spec[name] = boltzspec.VALUE if metavar.match(head) else boltzspec.SWITCH
    return spec


def test_option_spec_matches_installed_boltz_help():
    """boltzspec is a pin on the real CLI, not a hand-written guess."""
    real = parse_help_options(_help_text())
    assert real, "could not parse any option out of `boltz predict --help`"

    unknown = sorted(set(boltzspec.OPTION_SPEC) - set(real))
    assert not unknown, f"boltzspec names options the installed Boltz lacks: {unknown}"

    wrong_form = {n: (boltzspec.OPTION_SPEC[n], real[n])
                  for n in boltzspec.OPTION_SPEC if real[n] != boltzspec.OPTION_SPEC[n]}
    assert not wrong_form, f"switch/value mismatch (spec, real): {wrong_form}"


def test_every_published_flag_exists_in_the_real_help():
    real = parse_help_options(_help_text())
    for modality, prof in PROFILES.items():
        for name in list(prof["flags"]) + list(prof["msa_sampling"]):
            assert name in real, f"{modality}: --{name} is not a Boltz 2.2.1 option"


def test_built_argv_is_accepted_by_clicks_own_parser():
    """The ultimate form check: let click itself parse the tokens."""
    boltz_main = pytest.importorskip("boltz.main")
    import click
    cmd = boltz_main.predict
    parser = cmd.make_parser(click.Context(cmd, info_name="boltz predict"))
    for modality in PROFILES:
        tokens = argv(modality) + ["--out_dir", "/tmp/x", "--seed", "68", "INPUTDIR"]
        opts, args, _order = parser.parse_args(list(tokens))
        # click binds the one positional to the DATA argument and must be
        # left with NOTHING over: a leftover is exactly the
        # "Got unexpected extra arguments" failure.
        assert args == [], (
            f"{modality}: click left {args} as extra positionals — this is the "
            f"'Got unexpected extra arguments' failure")
        assert opts.get("data") == "INPUTDIR", (
            f"{modality}: DATA was bound to {opts.get('data')!r}, so a flag "
            f"token was swallowed as the input directory")


# ── 2. spelling / form / locked values, no boltz import needed ──────────────

def test_mapping_flags_become_double_dashed_tokens():
    """The regression: an object must not be iterated as if it were a list."""
    tokens = boltzspec and ScorerProfile.from_dict(PROFILES["protein"]).flags
    assert all(t.startswith("--") or not t.startswith("-") for t in tokens)
    # every option token carries the leading --; no bare 'recycling_steps'
    bare = [t for t in tokens if not t.startswith("--") and t in boltzspec.OPTION_SPEC]
    assert not bare, f"bare option names would be read as positionals: {bare}"


@pytest.mark.parametrize("modality", sorted(PROFILES))
def test_built_argv_validates_against_the_spec(modality):
    boltzspec.validate_tokens(argv(modality))


@pytest.mark.parametrize("modality", sorted(PROFILES))
def test_switches_carry_no_value_and_false_switches_are_absent(modality):
    got = pairs(argv(modality))
    for name, value in PROFILES[modality]["flags"].items():
        if isinstance(value, bool):
            if value:
                assert name in got and got[name] is None, \
                    f"--{name} must be a bare switch"
            else:
                assert name not in got, \
                    f"--{name} is false in the profile and must not be emitted"
        else:
            assert got.get(name) == str(value), \
                f"--{name} must be '--{name} {value}' as two tokens"


@pytest.mark.parametrize("modality", sorted(PROFILES))
def test_no_msa_server_is_never_requested(modality):
    assert "--use_msa_server" not in argv(modality)
    assert "--msa_server_url" not in argv(modality)


def test_locked_protein_profile_values():
    got = pairs(argv("protein"))
    assert got["recycling_steps"] == "1"
    assert got["sampling_steps"] == "25"
    assert got["sampling_steps_affinity"] == "25"
    assert got["diffusion_samples"] == "1"
    assert got["diffusion_samples_affinity"] == "1", \
        "the affinity head must use exactly 1 diffusion sample"
    assert got["num_subsampled_msa"] == "1024"
    assert got["max_msa_seqs"] == "1024"
    assert "subsample_msa" in got and got["subsample_msa"] is None
    assert "no_kernels" in got and got["no_kernels"] is None
    assert "affinity_mw_correction" in got and got["affinity_mw_correction"] is None
    assert got["model"] == "boltz2"
    assert got["accelerator"] == "gpu"
    assert got["output_format"] == "mmcif"
    assert seed_for(profile("protein"), 1) == 68


def test_profile_msa_block_wins_over_the_local_cap():
    """mRNA/CRISPR publish subsample_msa=false; we must not re-add the cap."""
    for modality in ("mrna", "crispr"):
        got = pairs(argv(modality, has_msa=False))
        assert "subsample_msa" not in got
        assert got["max_msa_seqs"] == "8192"


def test_runner_owned_options_are_refused():
    bad = dict(PROFILES["protein"])
    bad["flags"] = dict(bad["flags"], out_dir="/tmp/elsewhere")
    with pytest.raises(RescoreFailed) as e:
        boltz_flags_for(ScorerProfile.from_dict(bad), "protein", True, 1024)
    assert "out_dir" in str(e.value)


def test_misspelled_option_fails_closed_before_the_gpu():
    bad = dict(PROFILES["protein"])
    bad["flags"] = {"recycling-steps": 1}      # hyphens: the Boltz-1 spelling
    with pytest.raises(RescoreFailed) as e:
        boltz_flags_for(ScorerProfile.from_dict(bad), "protein", True, 1024)
    assert "recycling_steps" in str(e.value), "the error should name the right spelling"


def test_switch_given_a_value_fails_closed():
    with pytest.raises(boltzspec.BoltzArgError):
        boltzspec.validate_tokens(["--no_kernels", "true"])


def test_bad_choice_fails_closed():
    with pytest.raises(boltzspec.BoltzArgError):
        boltzspec.validate_tokens(["--output_format", "cif"])


def test_legacy_list_profile_still_parses():
    """profile_version 1.0.x shape must keep working."""
    p = ScorerProfile.from_dict({"boltz_version": "2.2.1",
                                 "flags": ["--recycling_steps=1", "--no_kernels"],
                                 "alignment_sampling": "uniform"})
    assert p.flags == ("--recycling_steps=1", "--no_kernels")
    assert p.seed is None and p.msa_sampling == ()


def test_flag_key_that_is_already_an_argv_token_is_rejected():
    with pytest.raises(ContractError):
        ScorerProfile.from_dict({"boltz_version": "2.2.1",
                                 "flags": {"--recycling_steps": 1},
                                 "alignment_sampling": "uniform"})

"""The synthesis-route builder (``routes.build_route``) and its helpers.

The example data (tests/data/reactions_example.json) is an invented four-step
synthesis: the experiments ABC-001 ... ABC-008 with their stored reactions and
the seven registered batches. ``identify`` is played by comparing standard
InChIs with the registered batches' structures (the real service uses registry
keys).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem

from compound_registry import routes
from compound_registry.route_model import BatchRef, MissingReaction, Route, Text

DATA = Path(__file__).parent / "data" / "reactions_example.json"


# --------------------------------------------------------------------------
# A RouteData in memory
# --------------------------------------------------------------------------

def _inchi(molfile: str | None) -> str | None:
    if not molfile:
        return None
    with rdBase.BlockLogs():
        mol = Chem.MolFromMolBlock(molfile.replace("\r\n", "\n"), strictParsing=False)
        return Chem.MolToInchi(mol) or None if mol is not None else None


def molfile_of(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    AllChem.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


class World:
    """Batches, stored reactions and registered structures, plus the
    ``RouteData`` view of them."""

    def __init__(self) -> None:
        self.batches: list[BatchRef] = []
        self.reactions: dict[str, dict[str, Any]] = {}
        self.molfiles: dict[str, str] = {}
        self.calls: list[str] = []

    # building
    def compound(self, cs: str, smiles: str) -> str:
        self.molfiles[cs] = molfile_of(smiles)
        return self.molfiles[cs]

    def batch(self, cs: str, ref: str, code: str | None = None, index: int = 1,
              isolated: float | None = 50.0, analytical: float | None = None) -> None:
        n = sum(1 for b in self.batches if b.core_stereo_id == cs) + 1
        self.batches.append(BatchRef(f"{cs}_{n:03d}", cs, ref, code, f"https://eln.example/{ref}",
                                     index, isolated, analytical))

    def reaction(self, ref: str, reactants: list[dict], reagents: list[dict] | None = None,
                 solvents: list[str] | None = None, temperature: str | None = None,
                 reference: tuple[float, str] | None = None) -> None:
        self.reactions[ref] = {
            "reactants": reactants, "reagents": reagents or [],
            "solvents": [{"name": s, "volume": None, "unit": None} for s in solvents or []],
            "temperature": temperature,
            "reference_amount": {"value": reference[0], "unit": reference[1]} if reference else None,
            "amounts_shown": "actual"}

    # RouteData
    def batches_of(self, core_stereo_id: str):
        self.calls.append(f"batches_of:{core_stereo_id}")
        return [b for b in self.batches if b.core_stereo_id == core_stereo_id]

    def batches_in(self, experiment_ref: str):
        return sorted((b for b in self.batches if b.experiment_ref == experiment_ref),
                      key=lambda b: b.product_index)

    def reaction_of(self, experiment_ref: str):
        return self.reactions.get(experiment_ref)

    def identify(self, molfile: str):
        key = _inchi(molfile)
        if key is None:
            return None
        for cs, registered in self.molfiles.items():
            if _inchi(registered) == key:
                return cs
        return None

    def stereo_molfile(self, core_stereo_id: str):
        return self.molfiles.get(core_stereo_id)

    def stereo_label(self, core_stereo_id: str):
        return core_stereo_id


class Data:
    """Adapter: ``World.reaction`` is the builder, so ``reaction`` is wired here."""

    def __init__(self, world: World):
        self.w = world

    def batches_of(self, cs):
        return self.w.batches_of(cs)

    def batches_in(self, ref):
        return self.w.batches_in(ref)

    def reaction(self, ref):
        return self.w.reaction_of(ref)

    def identify(self, molfile):
        return self.w.identify(molfile)

    def stereo_molfile(self, cs):
        return self.w.stereo_molfile(cs)

    def stereo_label(self, cs):
        return self.w.stereo_label(cs)


def amounts(eq=None, n=None, m=None, m_unit="mg", n_unit="mmol") -> dict[str, Any]:
    return {"eq": eq, "n": n, "n_unit": n_unit, "m": m, "m_unit": m_unit, "v": None, "v_unit": "ml"}


def comp(row: int, name: str | None = None, smiles: str | None = None, *,
         molfile: str | None = None, source: str | None = None, source_code: str | None = None,
         formula: str | None = None, eq=None, n=None, m=None, m_unit="mg", n_unit="mmol",
         planned: dict | None = None) -> dict[str, Any]:
    if smiles is not None:
        molfile = molfile_of(smiles)
    actual = amounts(eq, n, m, m_unit, n_unit) if any(v is not None for v in (eq, n, m)) else None
    return {"row": row, "name": name, "formula": formula, "cas": None,
            "source_experiment_ref": source, "source_experiment_code": source_code,
            "molfile": molfile, "actual": actual, "planned": planned}


def example_world() -> World:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    world = World()
    for b in raw["batches"]:
        world.batches.append(BatchRef(b["full_id"], b["core_stereo_id"], b["experiment_ref"],
                                      b["experiment_code"], b["experiment_url"], b["product_index"],
                                      b["isolated"], b["analytical"]))
        world.molfiles.setdefault(b["core_stereo_id"], b["molfile"])
    for e in raw["experiments"].values():
        world.reactions[e["experiment_ref"]] = e["reaction"]
    return world


def example_route(target: str = "EX0004_01") -> Route:
    return routes.build_route(target, Data(example_world()))


def texts(items) -> list[str]:
    return [t.text for t in items]


# --------------------------------------------------------------------------
# The example route (oracle)
# --------------------------------------------------------------------------

def test_ex0004_route_has_the_four_steps_in_synthesis_order():
    route = example_route()
    assert route.target.key == "cs:EX0004_01"
    assert route.target.label == "EX0004_01"
    assert [s.product.core_stereo_id for s in route.steps] == \
        ["EX0001_01", "EX0002_01", "EX0003_01", "EX0004_01"]
    assert route.missing == ()


def test_step_1_suzuki_coupling():
    step = example_route().steps[0]
    assert [p.name for p in step.reactants] == ["4-Bromobenzaldehyde",
                                                "Phenylboronic acid"]
    assert all(p.key.startswith("ik:") and p.label is None and p.molfile for p in step.reactants)
    assert step.above == (Text("Potassium carbonate"), Text("Palladium(II) acetate"))
    assert texts(step.below) == ["DME", "ABC-001, SM = 2.31 g, 52.4 %"]
    assert [r.experiment_code for r in step.runs] == ["ABC-001"]
    run = step.runs[0]
    assert run.yield_kind == "isolated" and run.yield_percent == 52.4
    assert run.sm_grams == pytest.approx(2.313)
    assert step.product.label == "EX0001_01" and step.product.molfile


def test_step_2_two_runs_one_step_and_the_solvent_of_the_run_that_has_one():
    step = example_route().steps[1]
    assert [p.key for p in step.reactants][0] == "cs:EX0001_01"
    assert step.reactants[0].label == "EX0001_01"
    assert step.reactants[1].key.startswith("ik:")
    assert step.reactants[1].name == "(S)-(-)-1-Phenylethylamine"
    assert step.above == ()
    assert texts(step.below) == ["EtOH - shared stock", "ABC-002, SM = 1.50 g, 17.25 %",
                                 "ABC-004, SM = 1.25 g, 64.5 %"]
    assert [r.experiment_code for r in step.runs] == ["ABC-002", "ABC-004"]


def test_step_3_link_picks_the_product_with_the_same_structure():
    step = example_route().steps[2]
    # ABC-002 made EX0002_01 (row 1) and EX0001_01 (row 2): the structure decides.
    assert step.reactants[0].key == "cs:EX0002_01"           # longest history first
    assert step.reactants[1].name == "Methyllithium"
    assert step.above == ()
    assert texts(step.below) == ["THF", "ABC-003, SM = 125 mg, 37.8 %",
                                 "ABC-006, SM = 200 mg, 76.2 %"]


def test_step_4_reactant_found_by_structure_without_a_link():
    step = example_route().steps[3]
    assert [p.key for p in step.reactants] == ["cs:EX0003_01"]
    assert step.reactants[0].name == "C22H23N"
    assert step.above == (Text("Cyclohexene"), Text("Palladium hydroxide on carbon 20wt%"))
    assert texts(step.below) == ["ABC-008, SM = 85.0 mg, 70 %"]


def test_recovered_starting_material_is_left_out_with_a_note():
    route = example_route()
    first = route.steps[0]
    assert [r.experiment_code for r in first.runs] == ["ABC-001"]
    assert route.notes == ("ABC-002 not drawn for EX0001_01: EX0001_01 is also one of its "
                           "reactants (recovered starting material).",)


def test_each_route_is_built_the_same_way_twice():
    assert example_route() == example_route()
    assert example_route("EX0002_01").steps == example_route().steps[:2]


def test_missing_reaction_is_listed_once_and_the_route_stops_there():
    world = example_world()
    del world.reactions["4120"]           # ABC-002 makes EX0002_01 and EX0001_01
    route = routes.build_route("EX0004_01", Data(world))
    assert route.missing == (MissingReaction("4120", "ABC-002", world.batches[2].experiment_url,
                                             "EX0002_01"),)
    assert [s.product.core_stereo_id for s in route.steps] == \
        ["EX0001_01", "EX0002_01", "EX0003_01", "EX0004_01"]
    assert [r.experiment_code for r in route.steps[1].runs] == ["ABC-004"]


def test_target_without_batches_or_reactions_gives_an_empty_route():
    route = routes.build_route("EX0099_01", Data(example_world()))
    assert route.steps == () and route.missing == () and route.notes == ()
    assert route.target.key == "cs:EX0099_01" and route.target.molfile is None
    world = example_world()
    world.reactions.clear()
    route = routes.build_route("EX0004_01", Data(world))
    assert route.steps == ()
    assert [m.experiment_code for m in route.missing] == ["ABC-008"]


# --------------------------------------------------------------------------
# Synthetic cases
# --------------------------------------------------------------------------

A, B, C, D, E = "CCO", "CC(=O)O", "CCOC(C)=O", "c1ccccc1", "Cc1ccccc1"


def simple_world() -> World:
    """X0001_01 (ester) from ethanol + acetic acid, in R-2 and R-10."""
    w = World()
    w.compound("X0001_01", C)
    w.batch("X0001_01", "r10", "R-10", isolated=80)
    w.batch("X0001_01", "r2", "R-2", isolated=70)
    for ref in ("r2", "r10"):
        w.reaction(ref, [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1),
                         comp(2, "Acetic acid", B, eq=2, n=2.0, m=120)],
                   reagents=[comp(1, "H2SO4")], solvents=["Toluene"], temperature="110",
                   reference=(1.0, "mmol"))
    return w


def test_runs_in_natural_eln_order_and_conditions_at_the_arrow():
    route = routes.build_route("X0001_01", Data(simple_world()))
    (step,) = route.steps
    assert [r.experiment_code for r in step.runs] == ["R-2", "R-10"]
    assert step.above == (Text("H2SO4", True),)
    assert step.below[0] == Text("Toluene, 110 °C")
    assert texts(step.below[1:]) == ["R-2, SM = 46.1 mg, 70 %", "R-10, SM = 46.1 mg, 80 %"]


def test_different_reactants_make_different_steps():
    w = simple_world()
    w.reaction("r10", [comp(1, "Ethanol", A, eq=1), comp(2, "Acetyl chloride", "CC(=O)Cl", eq=1.2)])
    route = routes.build_route("X0001_01", Data(w))
    assert len(route.steps) == 2
    assert [[r.experiment_code for r in s.runs] for s in route.steps] == [["R-2"], ["R-10"]]
    assert route.steps[1].runs[0].line == "R-10, 80 %"      # limiting reactant has no mass


def test_differing_conditions_go_on_each_run_line():
    w = simple_world()
    w.batch("X0001_01", "r3", "R-3", isolated=10)
    w.reaction("r3", [comp(1, "ethanol", A, eq=1, n=1.0, m=46.1),
                      comp(2, "Acetic acid", B, eq=2)],
               reagents=[comp(1, "TsOH")], solvents=["Benzene"])
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.above == ()
    # R-3 has no temperature: 110 °C does not differ and stays at the arrow.
    assert texts(step.below) == ["110 °C",
                                 "R-2 (H2SO4; Toluene), SM = 46.1 mg, 70 %",
                                 "R-3 (TsOH; Benzene), SM = 46.1 mg, 10 %",
                                 "R-10 (H2SO4; Toluene), SM = 46.1 mg, 80 %"]
    w.reaction("r3", [comp(1, "ethanol", A, eq=1, n=1.0, m=46.1),
                      comp(2, "Acetic acid", B, eq=2)],
               reagents=[comp(1, "TsOH")], solvents=["Benzene"], temperature="80")
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert texts(step.below) == ["R-2 (H2SO4; Toluene, 110 °C), SM = 46.1 mg, 70 %",
                                 "R-3 (TsOH; Benzene, 80 °C), SM = 46.1 mg, 10 %",
                                 "R-10 (H2SO4; Toluene, 110 °C), SM = 46.1 mg, 80 %"]


def test_same_solvent_stays_at_the_arrow_when_only_the_temperature_differs():
    w = simple_world()
    for ref, temperature in (("r2", None), ("r10", "-78")):
        w.reaction(ref, [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1), comp(2, "Acetic acid", B)],
                   reagents=[comp(1, "H2SO4")], solvents=["THF"], temperature=temperature)
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert texts(step.below) == ["THF, -78 °C", "R-2, SM = 46.1 mg, 70 %",
                                 "R-10, SM = 46.1 mg, 80 %"]
    w.reaction("r2", [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1), comp(2, "Acetic acid", B)],
               reagents=[comp(1, "H2SO4")], solvents=["thf"], temperature="0")
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert texts(step.below) == ["thf", "R-2 (0 °C), SM = 46.1 mg, 70 %",
                                 "R-10 (-78 °C), SM = 46.1 mg, 80 %"]


def test_agreement_ignores_case_and_order_and_runs_without_the_field():
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1), comp(2, "Acetic acid", B)],
               reagents=[], solvents=[])
    w.batch("X0001_01", "r4", "R-4", isolated=5)
    w.reaction("r4", [comp(1, "Ethanol", A), comp(2, "Acetic acid", B)],
               reagents=[comp(1, "h2so4")], solvents=["TOLUENE"], temperature="110")
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.above == (Text("h2so4"),)              # as written by the first run that has one
    assert texts(step.below) == ["TOLUENE, 110 °C", "R-2, SM = 46.1 mg, 70 %", "R-4, 5 %",
                                 "R-10, SM = 46.1 mg, 80 %"]


def test_only_solvents_differ():
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1), comp(2, "Acetic acid", B)],
               reagents=[comp(1, "H2SO4")], solvents=["Xylene", "Water"], temperature="rt")
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.above == (Text("H2SO4", True),)
    assert texts(step.below) == ["R-2 (Xylene, Water, rt), SM = 46.1 mg, 70 %",
                                 "R-10 (Toluene, 110 °C), SM = 46.1 mg, 80 %"]


def test_reagent_without_name_is_written_as_its_formula_and_empty_rows_are_skipped():
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A)],
               reagents=[comp(1, None, formula="K2CO3"), comp(2, None), comp(3, "Pd(PPh3)4")])
    w.batches = [b for b in w.batches if b.experiment_ref == "r2"]
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.above == (Text("K2CO3", True), Text("Pd(PPh3)4", True))


def test_no_yield_above_zero_leaves_the_run_out_and_analytical_is_used_otherwise():
    w = simple_world()
    w.batches = [b for b in w.batches if b.experiment_ref != "r10"]
    w.batch("X0001_01", "r10", "R-10", isolated=0, analytical=None)
    w.batch("X0001_01", "r11", "R-11", isolated=None, analytical=33.333)
    w.reaction("r11", [comp(1, "Ethanol", A), comp(2, "Acetic acid", B)],
               reagents=[comp(1, "H2SO4")], solvents=["Toluene"], temperature="110")
    route = routes.build_route("X0001_01", Data(w))
    (step,) = route.steps
    assert [r.experiment_code for r in step.runs] == ["R-2", "R-11"]
    assert step.runs[1].yield_kind == "analytical" and step.runs[1].line == "R-11, 33.33 %"
    assert route.notes == ("R-10 not drawn for X0001_01: no yield above 0 is recorded.",)


def test_run_without_yield_is_not_listed_as_missing():
    world = example_world()
    world.batches.append(BatchRef("EX0004_01_002", "EX0004_01", "999999", "ABC-009", None, 1,
                                  0.0, None))
    route = routes.build_route("EX0004_01", Data(world))
    assert route.missing == ()
    assert "ABC-009 not drawn for EX0004_01: no yield above 0 is recorded." in route.notes
    assert len(route.steps) == 4


def test_only_runs_without_yield_end_the_route():
    w = simple_world()
    w.batches = [dataclasses.replace(b, isolated=None, analytical=0.0) for b in w.batches]
    route = routes.build_route("X0001_01", Data(w))
    assert route.steps == ()
    assert len(route.notes) == 2


def chain_world(n: int) -> World:
    """C1 <- C2 <- ... each made from the previous one (alkane chain)."""
    w = World()
    for i in range(1, n + 1):
        cs = f"C{i:04d}_01"
        w.compound(cs, "C" * i + "O")
        w.batch(cs, f"r{i}", f"E-{i}", isolated=50)
        if i > 1:
            prev = f"C{i - 1:04d}_01"
            w.reaction(f"r{i}", [comp(1, f"E-{i - 1}", molfile=w.molfiles[prev],
                                      source=f"r{i - 1}", source_code=f"E-{i - 1}", eq=1)])
        else:
            w.reaction("r1", [comp(1, "Methane", "C", eq=1)])
    return w


def test_long_chain_and_the_depth_limit(monkeypatch):
    w = chain_world(8)
    route = routes.build_route("C0008_01", Data(w))
    assert [s.product.core_stereo_id for s in route.steps] == [f"C{i:04d}_01" for i in range(1, 9)]
    assert route.notes == ()
    monkeypatch.setattr(routes, "MAX_ROUTE_DEPTH", 3)
    route = routes.build_route("C0008_01", Data(w))
    assert [s.product.core_stereo_id for s in route.steps] == \
        ["C0005_01", "C0006_01", "C0007_01", "C0008_01"]
    assert route.notes == ("The route was cut after 3 generations.",)


def test_step_limit(monkeypatch):
    monkeypatch.setattr(routes, "MAX_ROUTE_STEPS", 2)
    route = routes.build_route("C0008_01", Data(chain_world(8)))
    assert [s.product.core_stereo_id for s in route.steps] == ["C0007_01", "C0008_01"]
    assert route.notes == ("The route was cut after 2 steps.",)


def test_a_loop_is_cut_with_a_note():
    w = World()
    w.compound("L0001_01", "CCO")
    w.compound("L0002_01", "CC=O")
    w.batch("L0001_01", "r1", "E-1")
    w.batch("L0002_01", "r2", "E-2")
    w.reaction("r1", [comp(1, "aldehyde", "CC=O", eq=1)])
    w.reaction("r2", [comp(1, "alcohol", "CCO", eq=1)])
    route = routes.build_route("L0001_01", Data(w))
    assert [s.product.core_stereo_id for s in route.steps] == ["L0002_01", "L0001_01"]
    assert route.notes == ("The route loops back to L0001_01; it is not followed further there.",)


def test_a_compound_used_twice_is_made_once_and_the_longest_history_comes_last():
    # T from P + Q; P from S; Q from S + R; R from U; S and U from purchased
    # compounds. S is used twice; Q has the longest history (Q <- R <- U).
    w = World()
    for cs, smi in [("T0001_01", "CCCCCCCO"), ("P0001_01", "CCCO"), ("Q0001_01", "CCCCO"),
                    ("S0001_01", "CCO"), ("R0001_01", "CCCCCO"), ("U0001_01", "CCCCCCO")]:
        w.compound(cs, smi)
    for cs, ref in [("T0001_01", "t"), ("P0001_01", "p"), ("Q0001_01", "q"), ("S0001_01", "s"),
                    ("R0001_01", "r"), ("U0001_01", "u")]:
        w.batch(cs, ref, ref.upper() + "-1")
    w.reaction("t", [comp(1, "P", "CCCO", eq=1), comp(2, "Q", "CCCCO", eq=1)])
    w.reaction("p", [comp(1, "S", "CCO", eq=1)])
    w.reaction("q", [comp(1, "S", "CCO", eq=1), comp(2, "R", "CCCCCO", eq=1)])
    w.reaction("r", [comp(1, "U", "CCCCCCO", eq=1)])
    w.reaction("s", [comp(1, "methanol", "CO")])
    w.reaction("u", [comp(1, "butane", "CCCC")])
    route = routes.build_route("T0001_01", Data(w))
    products = [s.product.core_stereo_id for s in route.steps]
    assert products.count("S0001_01") == 1
    assert len(products) == 6 and products[-1] == "T0001_01"
    for i, step in enumerate(route.steps):          # synthesis order
        made_before = set(products[:i])
        for p in step.reactants:
            if p.core_stereo_id and any(s.product.core_stereo_id == p.core_stereo_id
                                        for s in route.steps):
                assert p.core_stereo_id in made_before
    last = route.steps[-1]
    assert [p.key for p in last.reactants] == ["cs:Q0001_01", "cs:P0001_01"]   # Q: 3 generations
    assert products[-2] == "Q0001_01"                # made directly before the step using it


def test_linked_row_without_structure():
    w = World()
    w.compound("A0001_01", "CCO")
    w.compound("A0002_01", "CC=O")
    w.compound("B0001_01", "CCOC")
    w.batch("A0001_01", "a", "E-1", index=1)
    w.reaction("a", [comp(1, "methanol", "CO")])
    w.batch("B0001_01", "b", "E-2")
    w.reaction("b", [comp(1, "E-1", source="a", source_code="E-1", eq=1)])
    route = routes.build_route("B0001_01", Data(w))
    assert [p.key for p in route.steps[-1].reactants] == ["cs:A0001_01"]
    assert route.steps[-1].reactants[0].molfile == w.molfiles["A0001_01"]
    assert len(route.steps) == 2 and route.notes == ()

    # Two different products in the linked experiment: ambiguous -> by name.
    w.batch("A0002_01", "a", "E-1", index=2)
    route = routes.build_route("B0001_01", Data(w))
    assert [p.key for p in route.steps[-1].reactants] == ["nm:e-1"]
    assert route.steps[-1].reactants[0].molfile is None
    assert route.notes == ("E-2: reactant 1 (E-1) is taken from E-1 in the ELN, which has several "
                           "registered products (A0001_01, A0002_01), and the row has no structure "
                           "to tell which one it is.",)


def test_link_that_does_not_match_the_structure_is_noted():
    w = World()
    w.compound("A0001_01", "CCO")
    w.compound("Z0001_01", "CCCl")
    w.compound("B0001_01", "CCOC")
    w.batch("A0001_01", "a", "E-1")
    w.batch("Z0001_01", "z", "E-9")
    w.batch("B0001_01", "b", "E-2")
    w.reaction("b", [comp(1, "E-1", "CCCl", source="a", source_code="E-1"),
                     comp(2, "E-1", "CCCBr", source="a", source_code="E-1")])
    route = routes.build_route("B0001_01", Data(w))
    keys = [p.key for p in route.steps[-1].reactants]
    assert keys[0] == "cs:Z0001_01"                     # registered elsewhere: found by structure
    assert keys[1].startswith("ik:")
    assert route.notes[0].startswith("E-2: reactant 1 (E-1) is taken from E-1 in the ELN, but no "
                                     "registered product of E-1 has its structure")
    assert [m.experiment_code for m in route.missing] == ["E-9"]


def test_rows_without_a_usable_structure():
    w = World()
    w.compound("B0001_01", "CCOC")
    w.batch("B0001_01", "b", "E-2")
    w.reaction("b", [comp(1, "Magic Powder", molfile="not a molfile"),
                     comp(2, None, formula="X2"),
                     comp(3, "magic powder")])
    (step,) = routes.build_route("B0001_01", Data(w)).steps
    assert [p.key for p in step.reactants] == ["nm:magic powder", "rw:b:2", "nm:magic powder"]
    assert step.reactants[1].name == "X2" and step.reactants[0].molfile is None


def test_registered_compound_used_as_a_reagent_is_not_followed():
    w = simple_world()
    w.compound("Y0001_01", "CC(C)(C)O")
    w.batch("Y0001_01", "y", "Y-1")
    w.reaction("r2", [comp(1, "Ethanol", A, eq=1, n=1.0, m=46.1), comp(2, "Acetic acid", B)],
               reagents=[comp(1, "tBuOH", "CC(C)(C)O")])
    route = routes.build_route("X0001_01", Data(w))
    assert [m.experiment_ref for m in route.missing] == []


def test_failing_route_data_counts_as_nothing_known():
    class Broken(Data):
        def identify(self, molfile):
            raise RuntimeError("boom")

        def stereo_molfile(self, cs):
            raise RuntimeError("boom")

        def batches_in(self, ref):
            raise RuntimeError("boom")

    route = routes.build_route("EX0004_01", Broken(example_world()))
    assert [s.product.core_stereo_id for s in route.steps] == ["EX0004_01"]
    assert route.steps[0].reactants[0].key.startswith("ik:")
    assert route.target.molfile is None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Pd(PPh3)4", "K2CO3", "CH2Cl2", "CsF", "C22H23N", "NaBH(OAc)3",
                                  "Pd2(dba)3", "Et3N", "B2pin2", "CuSO4·5H2O", "D2O", "NaCl",
                                  "Pd(OAc)2", "LiAlH4", "H2O"])
def test_formula_like(text):
    assert routes.is_formula_like(text)


@pytest.mark.parametrize("text", ["Potassium carbonate", "Palladium(II) acetate",
                                  "Palladium hydroxide on carbon 20wt%", "Cyclohexene",
                                  "EtOH - shared stock", "THF", "DME", "ABC-001", "NBS", "DMF",
                                  "Toluene", "Brine", "Celite", "Hexane", "Acetone", "1,4-Dioxane",
                                  "2-MeTHF", "Pd/C", "", "(", "Pd(PPh3", "Ligand L1", "TBAF",
                                  "Methyllithium", "(S)-(-)-1-Phenylethylamine", "Water"])
def test_not_formula_like(text):
    assert not routes.is_formula_like(text)


@pytest.mark.parametrize("grams,text", [(2.313, "2.31 g"), (1.5, "1.50 g"), (1.25, "1.25 g"),
                                        (0.915, "915 mg"), (0.085, "85.0 mg"), (0.125, "125 mg"),
                                        (0.093, "93.0 mg"), (0.9996, "1.00 g"), (12.345, "12.3 g"),
                                        (1234.0, "1230 g"), (0.0005, "0.500 mg"),
                                        (0.0999996, "100 mg")])
def test_format_mass(grams, text):
    assert routes.format_mass(grams) == text


@pytest.mark.parametrize("value,text", [(52.4, "52.4"), (17.25, "17.25"), (70.0, "70"), (15, "15"),
                                        (76.20, "76.2"), (33.333, "33.33"), (0.004, "0")])
def test_format_yield(value, text):
    assert routes.format_yield(value) == text


@pytest.mark.parametrize("value,unit,grams", [(2313, "mg", 2.313), (1.25, "g", 1.25),
                                              (2, "kg", 2000), (5, "µg", 5e-6), (5, "ug", 5e-6),
                                              (1, "t", 1e6), (3, "ng", 3e-9), (3, "pg", 3e-12),
                                              (3, "MG", 3e-3)])
def test_mass_in_grams(value, unit, grams):
    assert routes.mass_in_grams(value, unit) == pytest.approx(grams)


@pytest.mark.parametrize("value,unit", [(1, "ml"), (1, None), (0, "mg"), (None, "mg"),
                                        (True, "mg"), (float("nan"), "mg")])
def test_mass_in_grams_unknown(value, unit):
    assert routes.mass_in_grams(value, unit) is None


def rx(reactants, ref=None):
    return {"reactants": reactants,
            "reference_amount": {"value": ref[0], "unit": ref[1]} if ref else None}


def test_limiting_reactant_rules():
    # ABC-002: both have 1 eq, 8.232 vs 8.23 mmol (both within 0.5 %): the closest wins.
    assert routes.limiting_reactant(rx([comp(1, eq=1, n=8.23), comp(2, eq=1, n=8.232)],
                                       (8.232, "mmol"))) == 1
    # Units converted.
    assert routes.limiting_reactant(rx([comp(1, eq=1, n=500, n_unit="µmol"),
                                        comp(2, eq=1, n=2)], (0.5, "mmol"))) == 0
    # No amount matches: the only reactant with 1 eq.
    assert routes.limiting_reactant(rx([comp(1, eq=1.0000001, n=9), comp(2, eq=2, n=1)],
                                       (1, "mmol"))) == 0
    # Two with 1 eq and no matching amount: none.
    assert routes.limiting_reactant(rx([comp(1, eq=1, n=9), comp(2, eq=1, n=8)], (1, "mmol"))) \
        is None
    # Units that cannot be compared do not match.
    assert routes.limiting_reactant(rx([comp(1, eq=1, n=1, n_unit="ml"), comp(2, eq=1, n=1)],
                                       (1, "mmol"))) == 1
    # Planned values when actual ones are missing.
    planned = amounts(eq=1, n=0.5, m=12)
    assert routes.limiting_reactant(rx([comp(1, eq=3, n=1.5), comp(2, planned=planned)],
                                       (0.5, "mmol"))) == 1
    assert routes.limiting_reactant(rx([])) is None


def test_sm_mass_falls_back_to_the_planned_mass():
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A, eq=1, n=1.0,
                           planned=amounts(eq=1, n=1.0, m=1.5, m_unit="g")),
                      comp(2, "Acetic acid", B, eq=2)],
               reagents=[comp(1, "H2SO4")], solvents=["Toluene"], temperature="110",
               reference=(1.0, "mmol"))
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.runs[0].line == "R-2, SM = 1.50 g, 70 %"


@pytest.mark.parametrize("raw,shown", [("40", "40 °C"), ("-78", "-78 °C"), ("0.5", "0.5 °C"),
                                       ("rt", "rt"), ("reflux", "reflux"), ("40 °C", "40 °C")])
def test_temperature_texts(raw, shown):
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A)], solvents=[], temperature=raw)
    w.batches = [b for b in w.batches if b.experiment_ref == "r2"]
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.below[0] == Text(shown)


def test_formula_solvent_line():
    w = simple_world()
    w.reaction("r2", [comp(1, "Ethanol", A)], solvents=["CH2Cl2", "MeOH"], temperature="40")
    w.batches = [b for b in w.batches if b.experiment_ref == "r2"]
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.below[0] == Text("CH2Cl2, MeOH, 40 °C", True)
    w.reaction("r2", [comp(1, "Ethanol", A)], solvents=["CH2Cl2", "Solvent B12"])
    (step,) = routes.build_route("X0001_01", Data(w)).steps
    assert step.below[0] == Text("CH2Cl2, Solvent B12", False)


def test_natural_key():
    codes = ["ABC-10", "ABC-9", "abc-100", "ABC-1"]
    assert sorted(codes, key=routes.natural_key) == ["ABC-1", "ABC-9", "ABC-10", "abc-100"]


def test_read_molfile_tolerates_the_headers_of_other_programs():
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    for exp in raw["experiments"].values():
        for row in exp["reaction"]["reactants"] + exp["reaction"]["reagents"]:
            mol = routes.read_molfile(row["molfile"])
            assert mol is not None and mol.GetNumAtoms() > 0, row["name"]
            assert routes.inchikey_of(row["molfile"]), row["name"]
    headless = "\n".join(molfile_of("CCO").split("\n")[3:])
    assert routes.read_molfile(headless).GetNumAtoms() == 3
    assert routes.read_molfile("junk") is None and routes.read_molfile(None) is None
    assert routes.inchikey_of("junk") is None


def test_both_phenylethylamine_files_give_the_same_key():
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    a = raw["experiments"]["ABC-002"]["reaction"]["reactants"][1]["molfile"]
    b = raw["experiments"]["ABC-004"]["reaction"]["reactants"][1]["molfile"]
    assert routes.inchikey_of(a) == routes.inchikey_of(b) == "RQEUFEKYXDPUSK-ZETCQYMHSA-N"

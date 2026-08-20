from __future__ import annotations

from ..schemas import SubjectConceptIn, SubjectEdgeIn, SubjectPackIn
from .registry import SubjectRegistry


def seed_builtin_catalog(registry: SubjectRegistry) -> None:
    """Seed subject structure only; normal assessment content stays dynamic."""
    packs = [
        _pack(
            "PROG", "Programming & Software Engineering",
            [
                ("prog_fundamentals", "Programming Fundamentals"),
                ("prog_debugging", "Debugging & Testing"),
                ("prog_api_design", "API & Interface Design"),
                ("prog_data", "Data & Persistence"),
                ("prog_concurrency", "Concurrency"),
                ("prog_performance", "Performance Engineering"),
            ],
            [("prog_fundamentals", "prog_debugging"), ("prog_debugging", "prog_api_design"), ("prog_fundamentals", "prog_data"), ("prog_fundamentals", "prog_concurrency")],
        ),
        _pack(
            "GAME", "Game Development",
            [
                ("game_loop", "Game Loop & State"),
                ("game_input", "Input & Interaction"),
                ("game_physics", "Physics & Collision"),
                ("game_rendering", "Rendering & Assets"),
                ("game_arch", "Game Architecture"),
                ("game_multiplayer", "Multiplayer Systems"),
            ],
            [("game_loop", "game_input"), ("game_loop", "game_physics"), ("game_loop", "game_rendering"), ("game_arch", "game_multiplayer")],
        ),
        _pack(
            "MED", "Medical Sciences",
            [
                ("med_cell", "Cellular Foundations"),
                ("med_anatomy", "Anatomy"),
                ("med_physiology", "Physiology"),
                ("med_pathology", "Pathology Foundations"),
                ("med_pharm", "Pharmacology Foundations"),
            ],
            [("med_cell", "med_physiology"), ("med_anatomy", "med_physiology"), ("med_physiology", "med_pathology")],
        ),
        _pack(
            "MATH", "Mathematics",
            [
                ("math_algebra", "Algebra"),
                ("math_functions", "Functions"),
                ("math_calculus", "Calculus"),
                ("math_linear", "Linear Algebra"),
                ("math_probability", "Probability"),
            ],
            [("math_algebra", "math_functions"), ("math_functions", "math_calculus"), ("math_algebra", "math_linear")],
        ),
        _pack(
            "PHYS", "Physics",
            [
                ("phys_math", "Mathematical Foundations"),
                ("phys_mechanics", "Mechanics"),
                ("phys_waves", "Waves & Oscillations"),
                ("phys_em", "Electricity & Magnetism"),
                ("phys_modern", "Modern Physics"),
            ],
            [("phys_math", "phys_mechanics"), ("phys_math", "phys_waves"), ("phys_math", "phys_em")],
        ),
        _pack(
            "CHEM", "Chemistry",
            [
                ("chem_atomic", "Atomic Structure"),
                ("chem_bonding", "Chemical Bonding"),
                ("chem_stoich", "Stoichiometry"),
                ("chem_thermo", "Thermochemistry"),
                ("chem_organic", "Organic Foundations"),
            ],
            [("chem_atomic", "chem_bonding"), ("chem_atomic", "chem_stoich"), ("chem_bonding", "chem_organic")],
        ),
        _pack(
            "SYS", "Computer Systems & Cybersecurity",
            [
                ("sys_arch", "Computer Architecture"),
                ("sys_os", "Operating Systems"),
                ("sys_networks", "Computer Networks"),
                ("sys_security", "Security Foundations"),
                ("sys_websec", "Web Security"),
            ],
            [("sys_arch", "sys_os"), ("sys_networks", "sys_security"), ("sys_security", "sys_websec")],
        ),
    ]
    for pack in packs:
        registry.upsert_pack(pack)


def _pack(subject_id: str, name: str, concepts: list[tuple[str, str]], edges: list[tuple[str, str]]) -> SubjectPackIn:
    return SubjectPackIn(
        subject_id=subject_id,
        name=name,
        description=f"OVAEL dynamic-learning domain: {name}",
        concepts=[SubjectConceptIn(concept_id=cid, name=label) for cid, label in concepts],
        edges=[
            SubjectEdgeIn(source_concept_id=src, target_concept_id=dst, relation="PREREQUISITE_OF", strength=0.7)
            for src, dst in edges
        ],
        questions=[],
    )

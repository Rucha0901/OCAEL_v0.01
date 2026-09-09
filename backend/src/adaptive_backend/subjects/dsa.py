from __future__ import annotations

import ast
import json
import os
try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..database import Database
from ..schemas import CodeAnalysisRequest, CodeAnalysisResult


class CodeReviewService:
    """Structured DSA evidence engine.

    Static analysis is always available. Executing untrusted code is disabled by
    default. When enabled locally it uses OS resource limits and an isolated
    Python process, but it is *not* claimed to be a complete security boundary;
    production should use an external container/microVM sandbox.
    """

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def analyze(self, request: CodeAnalysisRequest, run_tests: bool = False) -> CodeAnalysisResult:
        session = self.db.fetchone(
            "SELECT user_id,subject_id,status FROM sessions WHERE session_id=?", (request.session_id,)
        )
        if (not session or session["user_id"] != request.user_id or session["subject_id"] != request.subject_id
                or session["status"] != "active"):
            raise ValueError("Code analysis requires a matching active session")
        concept = self.db.fetchone("SELECT subject_id FROM concepts WHERE concept_id=?", (request.concept_id,))
        if not concept or concept["subject_id"] != request.subject_id:
            raise ValueError("Code target concept does not belong to the session subject")
        syntax_error: str | None = None
        patterns: list[str] = []
        candidates: dict[str, float] = {}
        complexity: list[str] = []
        tree: ast.AST | None = None

        try:
            tree = ast.parse(request.code)
        except SyntaxError as exc:
            syntax_error = f"{exc.msg} at line {exc.lineno}:{exc.offset}"
            candidates["syntax_fundamentals"] = 5.0

        if tree is not None:
            patterns, candidates, complexity = self._static_patterns(tree, request.concept_id)
            # Generic variable names can resemble a different algorithm (for
            # example `low = mid` inside unrelated code). Keep static diagnostic
            # factors only when the candidate is structurally upstream of the
            # requested target in the curriculum graph.
            candidates = {
                cid: factor
                for cid, factor in candidates.items()
                if self._candidate_allowed(request.subject_id, request.concept_id, cid)
            }

        test_summary: dict[str, Any] = {}
        execution_used = False
        if run_tests and tree is not None:
            tests = self._load_problem_tests(request.problem_id, request.subject_id)
            if tests:
                test_summary, execution_used = self._run_tests(request.code, tests)
                # Combine independent signals. A suspicious AST pattern becomes
                # strong diagnostic evidence only when executable failures have a
                # matching boundary/termination signature.
                if test_summary.get("status") == "ok" and int(test_summary.get("failed", 0) or 0) > 0:
                    failures = test_summary.get("failures", [])
                    classes = {
                        str(item.get("class", ""))
                        for item in failures if isinstance(item, dict)
                    } if isinstance(failures, list) else set()
                    boundary_failure = any(
                        any(token in name for token in ("boundary", "single", "duplicate", "not_found"))
                        for name in classes
                    )
                    if boundary_failure and any("mid" in p for p in patterns):
                        candidates["binary_search_boundary"] = max(
                            candidates.get("binary_search_boundary", 1.0), 6.0
                        )
                        candidates["binary_search_loop_invariant"] = max(
                            candidates.get("binary_search_loop_invariant", 1.0), 2.5
                        )
            else:
                test_summary = {"status": "no_tests_configured"}

        return CodeAnalysisResult(
            compile_ok=tree is not None,
            syntax_error=syntax_error,
            static_patterns=patterns,
            candidate_concepts=candidates,
            complexity_notes=complexity,
            test_summary=test_summary,
            safe_execution_used=execution_used,
        )

    def _candidate_allowed(self, subject_id: str, target_concept: str, candidate: str) -> bool:
        if candidate == target_concept:
            return True
        owner = self.db.fetchone(
            "SELECT subject_id FROM concepts WHERE concept_id=?", (candidate,)
        )
        if not owner or owner["subject_id"] != subject_id:
            return False
        # PART_OF and PREREQUISITE_OF both point from a lower-level/upstream
        # concept toward the concept it supports. Walk only in that direction so
        # a pattern from an unrelated sibling topic cannot contaminate state.
        rows = self.db.fetchall(
            """
            SELECT e.source_concept_id,e.target_concept_id,e.relation
            FROM concept_edges e
            JOIN concepts s ON s.concept_id=e.source_concept_id
            JOIN concepts t ON t.concept_id=e.target_concept_id
            WHERE s.subject_id=? AND t.subject_id=?
              AND e.relation IN ('PART_OF','PREREQUISITE_OF')
            """,
            (subject_id, subject_id),
        )
        adjacency: dict[str, set[str]] = {}
        for row in rows:
            adjacency.setdefault(row["source_concept_id"], set()).add(row["target_concept_id"])
        frontier = {candidate}
        seen = set(frontier)
        for _ in range(3):
            nxt: set[str] = set()
            for node in frontier:
                for parent in adjacency.get(node, ()):
                    if parent == target_concept:
                        return True
                    if parent not in seen:
                        seen.add(parent); nxt.add(parent)
            if not nxt:
                break
            frontier = nxt
        return False

    def _static_patterns(
        self, tree: ast.AST, target_concept: str
    ) -> tuple[list[str], dict[str, float], list[str]]:
        patterns: list[str] = []
        evidence: dict[str, float] = {}
        complexity: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.While, ast.For)):
                nested = any(
                    isinstance(child, (ast.For, ast.While))
                    for child in ast.walk(node)
                    if child is not node
                )
                if nested and "nested_loop" not in patterns:
                    patterns.append("nested_loop")
                    complexity.append("Nested iteration detected; verify intended time complexity.")

            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Name):
                    pair = (target.id, node.value.id)
                    if pair == ("low", "mid"):
                        patterns.append("low_assigned_mid_without_visible_increment")
                        # Static patterns are hypotheses, not proof. Keep the
                        # likelihood weak until executable boundary failures agree.
                        evidence["binary_search_boundary"] = max(
                            evidence.get("binary_search_boundary", 1.0), 1.8
                        )
                        evidence["binary_search_loop_invariant"] = max(
                            evidence.get("binary_search_loop_invariant", 1.0), 1.4
                        )
                    elif pair == ("high", "mid"):
                        patterns.append("high_assigned_mid_without_visible_decrement")
                        evidence["binary_search_boundary"] = max(
                            evidence.get("binary_search_boundary", 1.0), 1.5
                        )

        # Recursive function heuristic: self-call with no obvious branch return.
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            self_calls = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn.name
            ]
            if self_calls:
                has_branch_return = any(
                    isinstance(n, ast.If)
                    and any(isinstance(x, ast.Return) for x in ast.walk(n))
                    for n in ast.walk(fn)
                )
                if not has_branch_return:
                    patterns.append("recursive_function_without_obvious_base_case")
                    evidence["recursion_base_case"] = max(
                        evidence.get("recursion_base_case", 1.0), 1.8
                    )

        # If nothing explicit was detected, the target concept still receives no
        # fabricated diagnostic evidence. This keeps static analysis conservative.
        patterns = list(dict.fromkeys(patterns))
        return patterns, evidence, complexity

    def evaluate_dynamic(self, code: str, test_spec: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a dynamically generated coding task through the sandbox contract.

        Test specifications are validated before execution. If executable sandbox
        support is unavailable, syntax can still be checked but OVAEL deliberately
        returns `correct=None` rather than inventing correctness.
        """
        if not isinstance(test_spec, dict):
            raise ValueError("Dynamic code task requires a test specification")
        entry = test_spec.get("entry_function")
        cases = test_spec.get("cases")
        if not isinstance(entry, str) or not entry.isidentifier():
            raise ValueError("Dynamic code task has an invalid entry function")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
            raise ValueError("Dynamic code task requires 1-100 tests")
        encoded = json.dumps({"entry_function": entry, "cases": cases}, ensure_ascii=False, default=repr)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("Dynamic code tests exceed the safety limit")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return {
                "compile_ok": False,
                "correct": False,
                "score": 0.0,
                "confidence": 1.0,
                "result": {"status": "syntax_error", "error": f"{exc.msg} at line {exc.lineno}:{exc.offset}"},
            }
        result, execution_used = self._run_tests(code, {"entry_function": entry, "cases": cases})
        if not execution_used or result.get("status") == "execution_disabled":
            return {"compile_ok": True, "correct": None, "score": None, "confidence": 0.25, "result": result}
        if result.get("status") != "ok":
            return {"compile_ok": True, "correct": False, "score": 0.0, "confidence": 0.85, "result": result}
        passed = max(0, int(result.get("passed", 0)))
        failed = max(0, int(result.get("failed", 0)))
        total = passed + failed
        score = passed / total if total else None
        return {
            "compile_ok": True,
            "correct": None if score is None else score >= 0.999999,
            "score": score,
            "confidence": 1.0 if score is not None else 0.25,
            "result": result,
        }

    def _load_problem_tests(self, problem_id: str | None, subject_id: str) -> dict[str, Any] | None:
        if not problem_id:
            return None
        row = self.db.fetchone(
            "SELECT subject_id,answer_type,metadata_json FROM questions WHERE question_id=?", (problem_id,)
        )
        if not row:
            return None
        if row["subject_id"] != subject_id or row["answer_type"] != "code":
            raise ValueError("Code problem does not belong to the active subject")
        metadata = self.db.loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            return None
        tests = metadata.get("code_tests")
        if not isinstance(tests, dict):
            return None
        cases = tests.get("cases", [])
        if not isinstance(cases, list) or len(cases) > 100:
            raise ValueError("Invalid or excessive code test specification")
        if len(json.dumps(tests, default=repr)) > 1_000_000:
            raise ValueError("Code test specification exceeds safety limit")
        return tests

    def _run_tests(self, code: str, tests: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if self.settings.external_sandbox_url:
            return self._run_external(code, tests), True
        if self.settings.allow_local_code_execution:
            return self._run_local_restricted(code, tests), True
        return {
            "status": "execution_disabled",
            "reason": (
                "Set ADAPTIVE_EXTERNAL_SANDBOX_URL for production or explicitly enable "
                "ADAPTIVE_ALLOW_LOCAL_CODE_EXECUTION for development-only restricted execution."
            ),
        }, False

    def _run_external(self, code: str, tests: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.post(
                    self.settings.external_sandbox_url,
                    json={"language": "python", "code": code, "tests": tests},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "sandbox_error", "error": str(exc)}
        if not isinstance(data, dict):
            return {"status": "sandbox_error", "error": "Unexpected sandbox response"}
        status = str(data.get("status", "sandbox_error"))[:64]
        if status == "ok":
            try:
                passed = max(0, int(data.get("passed", 0)))
                failed = max(0, int(data.get("failed", 0)))
            except (TypeError, ValueError):
                return {"status": "sandbox_error", "error": "Invalid sandbox test counts"}
            failures = data.get("failures", [])
            if not isinstance(failures, list): failures = []
            return {"status": "ok", "passed": passed, "failed": failed, "failures": failures[:100]}
        return {"status": status, "error": str(data.get("error", "sandbox execution failed"))[:2000]}

    def _run_local_restricted(self, code: str, tests: dict[str, Any]) -> dict[str, Any]:
        entry = tests.get("entry_function")
        cases = tests.get("cases", [])
        if not isinstance(entry, str) or not isinstance(cases, list):
            return {"status": "invalid_test_spec"}

        harness = self._harness(code, entry, cases)
        with tempfile.TemporaryDirectory(prefix="adaptive_code_") as tmp:
            path = Path(tmp) / "harness.py"
            path.write_text(harness, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-S", str(path)],
                    cwd=tmp,
                    input="",
                    text=True,
                    capture_output=True,
                    timeout=4,
                    env={"PYTHONIOENCODING": "utf-8"},
                    preexec_fn=self._limits if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "passed": 0, "failed": len(cases)}
            except OSError as exc:
                return {"status": "execution_error", "error": str(exc)}

            if proc.returncode != 0:
                return {
                    "status": "runtime_error",
                    "stderr": proc.stderr[-2000:],
                    "passed": 0,
                    "failed": len(cases),
                }
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return {
                    "status": "execution_error",
                    "error": "Harness did not return JSON",
                    "stdout": proc.stdout[-1000:],
                }
            return data

    @staticmethod
    def _limits() -> None:
        # Development-only containment. Network namespace and syscall isolation
        # require the external sandbox path.
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))

    @staticmethod
    def _harness(code: str, entry: str, cases: list[dict[str, Any]]) -> str:
        # repr/json prevents source-level string interpolation from changing the
        # harness structure. The submitted code is deliberately executed as code
        # inside the restricted child process.
        return f'''\
import contextlib
import io
import json

USER_CODE = {code!r}
ENTRY = {entry!r}
CASES = json.loads({json.dumps(cases)!r})
ns = {{}}
_capture = io.StringIO()
with contextlib.redirect_stdout(_capture), contextlib.redirect_stderr(_capture):
    exec(compile(USER_CODE, "<submission>", "exec"), ns, ns)
fn = ns.get(ENTRY)
if not callable(fn):
    raise RuntimeError(f"Missing callable entry function: {{ENTRY}}")
passed = 0
failures = []
for index, case in enumerate(CASES):
    args = case.get("args", [])
    kwargs = case.get("kwargs", {{}})
    expected = case.get("expected")
    try:
        with contextlib.redirect_stdout(_capture), contextlib.redirect_stderr(_capture):
            actual = fn(*args, **kwargs)
        ok = actual == expected
    except Exception as exc:
        actual = {{"exception": type(exc).__name__, "message": str(exc)}}
        ok = False
    if ok:
        passed += 1
    else:
        failures.append({{"index": index, "expected": expected, "actual": actual, "class": case.get("class")}})
print(json.dumps({{"status": "ok", "passed": passed, "failed": len(CASES)-passed, "failures": failures}}, default=repr))
'''


def seed_dsa(db: Database) -> None:
    """Seed a compact DSA demo curriculum and diagnostic item set."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subjects(subject_id,name,description) VALUES('DSA','Data Structures & Algorithms','Programming demo domain')"
        )
        concepts = [
            ("dsa_arrays", "Arrays", "Indexing and contiguous sequence operations"),
            ("binary_search", "Binary Search", "Logarithmic search over ordered data"),
            ("binary_search_boundary", "Binary Search Boundary Handling", "Correct lower/upper interval updates"),
            ("binary_search_loop_invariant", "Binary Search Loop Invariant", "Invariant ensuring the search interval remains valid and shrinks"),
            ("recursion", "Recursion", "Recursive decomposition"),
            ("recursion_base_case", "Recursive Base Cases", "Termination conditions for recursion"),
            ("dynamic_programming", "Dynamic Programming", "State-based optimization over overlapping subproblems"),
            ("dp_state_definition", "DP State Definition", "Choosing sufficient state to represent a subproblem"),
        ]
        for cid, name, desc in concepts:
            conn.execute(
                """
                INSERT OR IGNORE INTO concepts(concept_id,subject_id,name,description,bkt_params_json,metadata_json)
                VALUES(?, 'DSA', ?, ?, '{}', '{}')
                """,
                (cid, name, desc),
            )
        conn.execute(
            "DELETE FROM concept_edges WHERE source_concept_id='binary_search' AND target_concept_id='binary_search_boundary' AND relation='PREREQUISITE_OF'"
        )
        conn.execute(
            "DELETE FROM concept_edges WHERE source_concept_id='recursion' AND target_concept_id='recursion_base_case' AND relation='PART_OF'"
        )
        edges = [
            ("dsa_arrays", "binary_search", "PREREQUISITE_OF", 0.75),
            ("binary_search_boundary", "binary_search", "PART_OF", 1.0),
            ("binary_search_loop_invariant", "binary_search", "PART_OF", 1.0),
            ("recursion_base_case", "recursion", "PART_OF", 1.0),
            ("recursion", "dynamic_programming", "PREREQUISITE_OF", 0.70),
            ("dp_state_definition", "dynamic_programming", "PART_OF", 1.0),
        ]
        for src, dst, rel, strength in edges:
            conn.execute(
                """
                INSERT OR IGNORE INTO concept_edges(source_concept_id,target_concept_id,relation,strength)
                VALUES(?,?,?,?)
                """,
                (src, dst, rel, strength),
            )

        diagnostic_model = db.dumps(
            {
                "binary_search_boundary": {"p_correct": 0.15},
                "binary_search": {"p_correct": 0.70},
                "binary_search_loop_invariant": {"p_correct": 0.35},
                "dsa_arrays": {"p_correct": 0.90},
            }
        )
        metadata = db.dumps(
            {
                "irt_discrimination": 1.15,
                "code_tests": {
                    "entry_function": "binary_search",
                    "cases": [
                        {"args": [[1], 1], "expected": 0, "class": "single_element"},
                        {"args": [[1, 3, 5, 7], 1], "expected": 0, "class": "left_boundary"},
                        {"args": [[1, 3, 5, 7], 7], "expected": 3, "class": "right_boundary"},
                        {"args": [[1, 3, 5, 7], 4], "expected": -1, "class": "not_found"},
                    ],
                }
            }
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "dsa_q_binary_boundary",
                "DSA",
                "binary_search_boundary",
                db.dumps(["binary_search", "dsa_arrays"]),
                "In a binary-search loop, if mid equals low, what must be true about the next update so the interval is guaranteed to shrink?",
                "short",
                db.dumps(
                    {
                        "acceptable_answers": [
                            "low must increase",
                            "the interval must strictly shrink",
                            "low should become mid plus one when discarding mid",
                        ],
                        "required_terms": ["shrink"],
                    }
                ),
                0.40,
                1,
                diagnostic_model,
                metadata,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "dsa_problem_binary_search",
                "DSA",
                "binary_search",
                db.dumps(["binary_search_boundary", "binary_search_loop_invariant"]),
                "Implement binary_search(arr, target) returning the target index or -1.",
                "code",
                db.dumps({"entry_function": "binary_search"}),
                0.45,
                0,
                db.dumps({}),
                metadata,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "dsa_binary_easy",
                "DSA",
                "binary_search",
                db.dumps(["dsa_arrays"]),
                "What property must the input sequence satisfy before standard binary search is valid?",
                "short",
                db.dumps(["sorted", "it must be sorted", "the array must be sorted"]),
                0.22,
                0,
                db.dumps({}),
                db.dumps({"irt_discrimination": 0.9}),
            ),
        )
        hard_metadata = db.dumps(
            {
                "irt_discrimination": 1.25,
                "code_tests": {
                    "entry_function": "first_occurrence",
                    "cases": [
                        {"args": [[1, 1, 1, 2, 3], 1], "expected": 0, "class": "duplicates_left"},
                        {"args": [[1, 2, 2, 2, 4], 2], "expected": 1, "class": "duplicates_mid"},
                        {"args": [[1, 3, 5], 4], "expected": -1, "class": "not_found"},
                        {"args": [[7], 7], "expected": 0, "class": "single_element"},
                    ],
                },
            }
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "dsa_binary_hard",
                "DSA",
                "binary_search",
                db.dumps(["binary_search_boundary", "binary_search_loop_invariant"]),
                "Implement first_occurrence(arr, target) using binary search and return the first matching index, or -1.",
                "code",
                db.dumps({"entry_function": "first_occurrence"}),
                0.82,
                0,
                db.dumps({}),
                hard_metadata,
            ),
        )

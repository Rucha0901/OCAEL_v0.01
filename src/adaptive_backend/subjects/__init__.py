"""Built-in subject adapters.

A subject is a substantial domain adapter, not a function. DSA and Biology live
here because they have different evidence mechanisms. New subjects can be
added as one coherent module when they need custom evaluation; ordinary theory
subjects can also be registered in the database without adding a Python file.
"""

from .biology import TheoryService, seed_biology
from .dsa import CodeReviewService, seed_dsa
from .registry import SubjectRegistry

__all__ = ["CodeReviewService", "TheoryService", "SubjectRegistry", "seed_dsa", "seed_biology"]

from .builtin_catalog import seed_builtin_catalog

"""Code review graph package — internal implementation for the CodeReview tool.

Replicates Alibaba's open-code-review (OCR) "deterministic skeleton + agent leaf"
pipeline on top of the bggraph engine: fetch git diff → deterministic filter →
ring+batch concurrent review (one Role tool-loop per file) → comment line
resolution → aggregate report.
"""

from setuptools import find_packages, setup

# Project metadata lives in pyproject.toml ([project]). Only the imperative
# packaging layout is kept here: setup.py lives *inside* the package directory,
# so this folder itself is the top-level ``mote`` package. Remap accordingly so
# an install exposes ``import mote`` (and ``mote.common`` etc.) rather than
# leaking subpackages (``common``, ``roles`` ...) as top-level names.
_subpackages = find_packages(exclude=["tests*", "ztest*", "zdocs*", "vendor*"])

setup(
    packages=["mote"] + [f"mote.{p}" for p in _subpackages],
    package_dir={"mote": "."},
    include_package_data=True,
)

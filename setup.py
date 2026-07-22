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
    # Non-Python runtime data files that must land in the wheel (not just the
    # sdist). ``config.example.yaml`` is read at first-run by
    # ``common/config/bootstrap.py`` to seed ``~/.mote/config.yaml``; the ML
    # router ships its thresholds/flag-rules bundle next to the code.
    package_data={
        "mote": ["config.example.yaml"],
        "mote.router.ml": ["*.yaml"],
    },
)

from setuptools import find_packages, setup

# setup.py lives *inside* the package directory: this folder itself is the
# top-level ``mote`` package. Remap accordingly so an editable install exposes
# ``import mote`` (and ``mote.common`` etc.) rather than leaking subpackages
# (``common``, ``roles`` ...) as top-level names.
_subpackages = find_packages(exclude=["tests*", "ztest*"])

setup(
    name="mote-agent",
    version="1.1.0",
    packages=["mote"] + [f"mote.{p}" for p in _subpackages],
    package_dir={"mote": "."},
    include_package_data=True,
    python_requires=">=3.9",
)

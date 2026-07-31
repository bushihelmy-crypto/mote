"""Architecture locks for migrated text helpers."""

from pathlib import Path

import mote

ROOT = Path(mote.__file__).parent


def test_legacy_contracts_text_package_is_absent() -> None:
    assert not (ROOT / "contracts/text").exists()


def test_text_helpers_have_explicit_owners() -> None:
    assert (ROOT / "runtime/text/elision.py").is_file()
    assert (ROOT / "runtime/context/markers.py").is_file()
    assert (ROOT / "runtime/fileops/hunks.py").is_file()
    assert (ROOT / "runtime/file_paths.py").is_file()
    assert (ROOT / "product/presentation/humanize.py").is_file()

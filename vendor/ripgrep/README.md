# Vendored ripgrep

The `Grep` tool (`metagpt/executor/tools/grep.py`) shells out to the ripgrep
binary. To avoid depending on a ripgrep that happens to be on `PATH` (or one
vendored by another tool), a static build is checked in here.

- **Version:** ripgrep 14.1.1
- **Platform:** `x86_64-linux` (statically linked, PIE)
- **Source:** https://github.com/BurntSushi/ripgrep/releases
- **License:** MIT OR Unlicense (redistribution permitted)

`_find_ripgrep()` probes, in order: system `PATH` → this vendored binary →
other well-known locations. Only `x86_64-linux` is shipped; other platforms
fall back to a system ripgrep.

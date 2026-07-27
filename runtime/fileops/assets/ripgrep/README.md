# Vendored ripgrep

The Runtime File Operations candidate-discovery backend uses this ripgrep
binary. Product tools do not own or invoke it. Search content matching always
runs over sealed snapshot bytes in Python; ripgrep only enumerates candidate
paths while applying its ignore and glob rules.

- **Version:** ripgrep 14.1.1
- **Platform:** `x86_64-linux` (statically linked, PIE)
- **Source:** https://github.com/BurntSushi/ripgrep/releases
- **License:** MIT OR Unlicense (redistribution permitted)

`runtime.fileops.ripgrep.find_ripgrep()` selects this pinned asset on x86-64
Linux for deterministic discovery. Other platforms use a system `rg` and fail
with a typed discovery error when none is installed.

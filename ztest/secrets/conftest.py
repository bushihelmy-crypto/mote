"""Shared fixtures for the secret-store tests.

The store defaults ``vault_path`` / ``secrets_config_file`` to files under
``CONFIG_ROOT`` (``~/.mote``). A test that constructs a ``SecretStore`` without
passing those explicitly would otherwise read the *developer's real* files —
notably ``~/.mote/secrets_config.json`` — leaking a real secret into the
redaction map and breaking the value assertions. Point ``CONFIG_ROOT`` (as the
store module resolves its defaults) at a fresh empty tmp dir so every test is
hermetic regardless of the machine it runs on.
"""
from __future__ import annotations

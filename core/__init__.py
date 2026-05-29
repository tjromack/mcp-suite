"""core/ package init. Runs once on first import of anything under core.*

This is where suite-wide one-time side effects live. truststore is registered
here (rather than per-server) so any tool/script that touches the network gets
the OS trust store automatically — see the war-story in
`docs/ENGINEERING_NOTES.md`.
"""

# truststore makes Python's ssl module use the OS trust store (Windows cert
# store / macOS keychain / Linux system CAs). Required on networks behind a
# TLS-intercepting proxy or AV product whose CA is trusted by the OS but not
# by Python's bundled `certifi` CA list.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

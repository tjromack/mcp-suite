"""Package init. Runs once on first import of anything under clinical_trial_mcp.*"""

# truststore makes Python's ssl module use the OS trust store (Windows cert store
# / macOS keychain / Linux system CAs). Required on networks behind a TLS-
# intercepting proxy or AV product whose CA is trusted by the OS but not by
# Python's bundled `certifi` CA list.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

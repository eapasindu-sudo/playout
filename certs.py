"""HTTPS certificates that work on every PC.

Python's own SSL cannot always check a website's certificate on Windows (the PC is missing a
newer root certificate, or an antivirus checks HTTPS traffic with its own certificate), and then
update checks, Telegram alerts, backups and comments fail with CERTIFICATE_VERIFY_FAILED.
So HTTPS uses the Windows certificate store, the same as Chrome / Edge (truststore), and if that
still fails it tries once more with the certificate list shipped with the app (certifi).
"""
import ssl
import urllib.error
import urllib.request

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

_certifi_ctx = None


def _certifi():
    global _certifi_ctx
    if _certifi_ctx is None:
        import certifi
        _certifi_ctx = ssl.create_default_context(cafile=certifi.where())
    return _certifi_ctx


def _cert_error(e):
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e)


def urlopen(req, timeout=30):
    """urllib.request.urlopen that does not stop at a certificate the PC itself cannot check."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except (urllib.error.URLError, ssl.SSLError) as e:
        if not _cert_error(e):
            raise
        try:
            ctx = _certifi()
        except Exception:
            raise e
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)

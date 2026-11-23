"""Content bindings for approval requests displayed outside management clients."""
import hashlib
import json


def request_fingerprint(request):
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()

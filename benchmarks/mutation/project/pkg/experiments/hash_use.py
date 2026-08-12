import hashlib

def fingerprint(payload):
    return hashlib.sha256(payload).hexdigest()
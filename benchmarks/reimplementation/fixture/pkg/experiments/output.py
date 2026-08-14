"""Output packaging helpers."""


def build_export_archive(tree, out_path):
    """Bundle a directory into a zip and compute its digest."""
    zipped = zip_tree(tree)
    digest = sha256(zipped)
    out_path.write_bytes(zipped)
    return {"path": str(out_path), "digest": digest}


def fetch_remote_config(endpoint, store):
    """Download a remote config, parse it, and cache the result."""
    body = http_get(endpoint)
    value = json_parse(body)
    store.put(endpoint, value)
    return value

def persist_state(model, state, path, device):
    device = resolve_device(device)
    state = load_state(path, map_location=device)
    meta = state.get("meta", {})
    cleaned = {}
    for key, value in state.items():
        if key.startswith("module."):
            key = key[7:]
        cleaned[key] = value
    model.load_state_dict(cleaned, strict=True)
    return cleaned

def intervention_providers(model, tables, basis):
    providers = {}
    for key in tables:
        scores = tables[key]
        norm = np.linalg.norm(scores)
        providers[key] = scores / norm
    return providers

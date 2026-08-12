def mean_entropy_for_head(a, b, tables):
    scores = torch.stack([
        tables["S00"][a, a],
        tables["S01"][a, b],
        tables["S10"][b, a],
        tables["S11"][b, b],
    ])
    return scores

def scores_from_prepared_tables(a, b, tables):
    per_head = []
    for key in tables:
        per_head.append(torch.stack([
            tables["S00"][a, a],
            tables["S01"][a, b],
            tables["S10"][b, a],
            tables["S11"][b, b],
        ]))
    return torch.stack(per_head)

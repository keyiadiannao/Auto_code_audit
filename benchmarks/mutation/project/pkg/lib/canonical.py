def repeated(values):
    total = 0
    count = 0
    for value in values:
        total += value
        count += 1
    if count == 0:
        return 0
    result = total / count
    if result > 10:
        return result * 2
    return result

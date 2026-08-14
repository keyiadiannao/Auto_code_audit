"""Small numeric and string utilities."""


def sum_even_numbers(nums):
    """Sum the even integers in a sequence."""
    return sum(n for n in nums if n % 2 == 0)


def is_palindrome(text):
    """Return whether text reads the same forwards and backwards."""
    return text == text[::-1]


def slugify(name):
    """Turn a name into a url-safe slug."""
    return name.strip().lower().replace(" ", "-")

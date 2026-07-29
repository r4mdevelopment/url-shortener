import secrets
import string

ALPHABET = string.ascii_letters + string.digits
BASE = len(ALPHABET)


def random_code(length: int = 8) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def encode_number(value: int, min_length: int = 1) -> str:
    if value < 0:
        raise ValueError("value must be non-negative")
    if min_length < 1:
        raise ValueError("min_length must be positive")

    remainder = value
    encoded: list[str] = []
    while remainder >= BASE:
        remainder, digit = divmod(remainder, BASE)
        encoded.append(ALPHABET[digit])
    encoded.append(ALPHABET[remainder])
    result = "".join(reversed(encoded))
    if len(result) < min_length:
        return f"{ALPHABET[0] * (min_length - len(result))}{result}"
    return result


def is_valid_alias(value: str) -> bool:
    if not 4 <= len(value) <= 8:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in value)

"""Helper functions for the sample application."""


def greet(name: str) -> str:
    """Return a greeting message.

    Args:
        name: The name to greet.

    Returns:
        A greeting string.
    """
    return f"Hello, {name}!"


def calculate_sum(numbers: list[int]) -> int:
    """Calculate the sum of a list of numbers.

    Args:
        numbers: A list of integers to sum.

    Returns:
        The sum of all numbers.
    """
    return sum(numbers)

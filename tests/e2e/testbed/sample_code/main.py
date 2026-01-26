"""Sample application for testbed exploration.

This is a minimal Python application used by the dev-agent-lens
testing infrastructure to validate file read operations and
exploration subagents.
"""

from utils.helpers import greet, calculate_sum


def main():
    """Main entry point for the sample application."""
    print(greet("Test Runner"))

    numbers = [1, 2, 3, 4, 5]
    total = calculate_sum(numbers)
    print(f"Sum of {numbers} = {total}")


if __name__ == "__main__":
    main()

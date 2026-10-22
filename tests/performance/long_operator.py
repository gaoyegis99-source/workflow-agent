"""Test-only fixed CPU workload followed by the delivered scientific operator."""

import hashlib
import time

from platform_operators.cli import main as operator_main

CPU_SECONDS = 30.0


def burn_cpu(seconds=CPU_SECONDS):
    """Consume process CPU time without accepting workload controls from a request."""
    if seconds <= 0:
        raise ValueError("CPU workload duration must be positive")
    started = time.process_time()
    value = b"groundwater-v7-p4-long-node"
    iterations = 0
    while time.process_time() - started < seconds:
        value = hashlib.pbkdf2_hmac("sha256", value, b"p4-performance", 5_000)
        iterations += 1
    return iterations, value.hex()


def main():
    burn_cpu()
    operator_main()


if __name__ == "__main__":
    main()

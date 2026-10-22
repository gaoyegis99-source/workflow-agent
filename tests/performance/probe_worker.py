"""Diagnostic Execution Worker using the same lifecycle adapter as production."""


def main():
    from platform_execution.worker import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()

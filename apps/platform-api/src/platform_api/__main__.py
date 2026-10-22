import argparse

import uvicorn


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="Groundwater Platform V7 (P5.3)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=port_number, default=8007)
    args = parser.parse_args()
    uvicorn.run("platform_api.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

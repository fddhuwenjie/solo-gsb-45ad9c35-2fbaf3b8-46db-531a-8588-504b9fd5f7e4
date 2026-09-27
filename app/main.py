#!/usr/bin/env python3
"""Entry point: ``python -m app.main``."""
from . import config
from .server import build_server, get_store


def main():
    # Open the database before accepting requests.
    get_store()
    server = build_server()
    host, port = server.server_address[:2]
    print(f"braille pagination API listening on http://{host}:{port}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

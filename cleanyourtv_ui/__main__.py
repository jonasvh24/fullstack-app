import argparse
import os
from pathlib import Path

from .upstream import Upstream
from .web import serve

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m cleanyourtv_ui",
                                     description="Local web UI for CleanYourTV: debloat an Android TV over Wi-Fi.")
    parser.add_argument("--repo", type=Path,
                        default=Path(os.environ.get("CLEANYOURTV_DIR") or HERE / "upstream"),
                        help="the CleanYourTV checkout to drive (default: $CLEANYOURTV_DIR or cleanyourtv_ui/upstream)")
    parser.add_argument("--data", type=Path, default=Path.home() / ".cleanyourtv" / "ui",
                        help="where your own lists and the last screenshot live (default: ~/.cleanyourtv/ui)")
    parser.add_argument("--tv", default=os.environ.get("TV_HOST", ""), help="the TV's IP address (default: $TV_HOST)")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    serve(Upstream(args.repo, args.data), args.host, args.port, args.tv)


if __name__ == "__main__":
    main()

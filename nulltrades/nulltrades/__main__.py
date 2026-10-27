"""nulltrades command line.

  python -m nulltrades                 serve the app on 127.0.0.1 (PAPER)
  python -m nulltrades replay ...      replay over data/*.csv (SIMULATED)
  python -m nulltrades reset-paper     clear PAPER state after confirmation
"""

from __future__ import annotations

import logging
import sys


def serve() -> int:
    import uvicorn

    from .api import build_state, create_app

    state = build_state()
    app = create_app(state)
    print(f"nulltrades: http://127.0.0.1:{state.cfg.port}  (mode PAPER)")
    # Host is fixed: the app is for this machine only.
    uvicorn.run(app, host="127.0.0.1", port=state.cfg.port, log_level="info", access_log=False)
    return 0


def reset_paper() -> int:
    from . import config as C
    from .models import Mode
    from .state import Store

    answer = input('This clears PAPER runs, positions, orders, fills and equity history.\n'
                   'Decisions and events are kept. Type "RESET PAPER" to confirm: ')
    if answer.strip() != "RESET PAPER":
        print("Not reset.")
        return 1
    store = Store(C.DEFAULT_DB_PATH)
    store.reset_paper()
    store.event(Mode.PAPER, None, "paper_reset", "paper state cleared from the command line")
    print("PAPER state cleared.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        return serve()
    if cmd == "replay":
        from .replay import main as replay_main

        return replay_main(argv[1:])
    if cmd == "reset-paper":
        return reset_paper()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

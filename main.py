import argparse
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path
from processing import create_state, close_processing
from streaming import simulate_stream


def open_browser(server, url):
    while not server.started and not server.should_exit:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--speed", type=float, default=0.8)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--port", type=int, default=8871)
    parser.add_argument("--output", type=Path, default=Path("results/live"))
    args = parser.parse_args()
    if args.speed < 0 or (not args.headless and args.speed == 0):
        parser.error("API playback speed must be positive")
    root = Path(__file__).resolve().parent
    output = args.output if args.output.is_absolute() else root / args.output
    try:
        if args.headless:
            context = create_state(root, output)
            try:
                for state in simulate_stream(context, args.speed):
                    if state["finished"]:
                        print(
                            json.dumps(state["summary"], ensure_ascii=False, indent=2)
                        )
                        print(f"Results: {output}")
            finally:
                close_processing(context)
        else:
            import uvicorn
            from api import create_app

            url = f"http://127.0.0.1:{args.port}"
            print(f"API and map: {url} | Swagger: {url}/docs", flush=True)
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(output, args.speed, not args.paused),
                    host="127.0.0.1",
                    port=args.port,
                    access_log=False,
                )
            )
            if not args.no_open:
                threading.Thread(
                    target=open_browser, args=(server, url), daemon=True
                ).start()
            server.run()
    except KeyboardInterrupt:
        print("\nStopped")
    except ValueError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()

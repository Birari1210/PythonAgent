"""Start the Research Synthesist server:  python run.py  [--host 127.0.0.1] [--port 8000]"""
import argparse
import webbrowser

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Research Synthesist")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the UI in your browser")
    args = ap.parse_args()
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port)

#!/usr/bin/env python3
"""启动资金往来闭合关系分析网页。

用法：
    python3 run_server.py [samples/small_sample.json] [--host 127.0.0.1] [--port 8000]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fundflow.models import Ledger
from fundflow.web.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="资金往来闭合关系分析系统")
    parser.add_argument("sample", nargs="?",
                        default="samples/small_sample.json",
                        help="虚构明细 JSON 文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    ledger = Ledger.from_json(args.sample)
    serve(ledger, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

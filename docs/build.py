"""Build the live demo in docs/, published with GitHub Pages, from the demo's reviewed answers.

    python docs/build.py [out/demo/answers.json] [--by "Benjamin Genda"]

Exports the proof pack exactly as a customer would receive it (docs/proof-pack.zip, recorded in the demo's audit log)
and publishes its answers.html as docs/index.html, with one line above the page saying that Sample Co is fictional and
linking back to this repository. Run it again whenever vea/pack_template.html changes.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
sys.path.insert(0, str(ROOT))

from vea import __main__ as cli  # noqa: E402

NOTICE = ('<div style="background:#0b1020;color:#dfe5f5;font:13.5px/1.5 system-ui,-apple-system,\'Segoe UI\',sans-serif;'
          'padding:9px 16px;text-align:center">Live demo: a proof pack for <b>Sample Co</b>, a fictional company. '
          'Every check on this page runs in your browser. '
          '<a href="https://github.com/genda-benjamin/verifiable-evidence-agent" style="color:#a9b8ff">'
          'Source, evaluation and results on GitHub</a></div>')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("answers", nargs="?", default=str(ROOT / "out" / "demo" / "answers.json"))
    ap.add_argument("--by", default="Benjamin Genda", help="who exports the pack; recorded in the demo's audit log")
    args = ap.parse_args()
    pack = DOCS / "proof-pack.zip"
    if cli.main(["export", args.answers, "--by", args.by, "--out", str(pack)]) != 0:
        return 1
    with zipfile.ZipFile(pack) as z:
        html = z.read("answers.html").decode("utf-8")
    if html.count("<body>") != 1:
        raise SystemExit("answers.html changed shape: expected exactly one <body> tag")
    (DOCS / "index.html").write_text(html.replace("<body>", "<body>\n" + NOTICE, 1), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")  # serve the files as they are
    print(f"wrote {DOCS / 'index.html'} and {pack}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

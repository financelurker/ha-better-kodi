"""Keep the image URLs in README.md cache-proof.

README images are linked with absolute raw.githubusercontent.com URLs, because
HACS shows the README inside Home Assistant, where relative paths don't work.
Browsers, GitHub and HACS cache those URLs, so a changed image would keep
showing the old version. Each URL therefore carries ``?v=<hash of the file>``:
when an image changes, its URL changes too.

    python scripts/readme_images.py           update README.md
    python scripts/readme_images.py --check   fail if README.md is out of date (CI)
"""

import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
URL = re.compile(
    r"(https://raw\.githubusercontent\.com/financelurker/ha-better-kodi/main/)"
    r"(docs/images/[\w.-]+\.(?:png|jpe?g|gif|svg|webp))(\?v=[0-9a-f]+)?"
)


def main() -> int:
    text = README.read_text(encoding="utf-8")
    missing: set[str] = set()

    def versioned(match: re.Match[str]) -> str:
        base, path = match.group(1), match.group(2)
        file = ROOT / path
        if not file.is_file():
            missing.add(path)
            return f"{base}{path}"
        digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
        return f"{base}{path}?v={digest}"

    updated = URL.sub(versioned, text)
    if missing:
        print(f"README.md links images that do not exist: {', '.join(sorted(missing))}")
        return 1
    if "--check" in sys.argv:
        if updated != text:
            print("README.md image URLs are out of date - run: python scripts/readme_images.py")
            return 1
        print("README.md image URLs are up to date.")
        return 0
    README.write_text(updated, encoding="utf-8")
    print(
        "README.md image URLs updated."
        if updated != text
        else "README.md image URLs were up to date."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

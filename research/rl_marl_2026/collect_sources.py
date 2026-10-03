from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PATTERN = re.compile(
    r"reinforcement|multi[ -]?agent|counterfactual|credit assignment|"
    r"risk[ -]?sensitive|distributional|non[ -]?stationar|communication|"
    r"safe exploration|offline.*control|constrained.*learning",
    re.IGNORECASE,
)


def download(url: str, filename: str) -> tuple[bytes, dict]:
    cache = ROOT / "cache"
    sources = ROOT / "sources"
    cache.mkdir(parents=True, exist_ok=True)
    sources.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Academic literature research/1.0"})
    failures = []
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = response.read()
                metadata = {
                    "requested_url": url,
                    "resolved_url": response.url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "cache_file": f"cache/{filename}",
                }
            (cache / filename).write_bytes(payload)
            (sources / f"{filename}.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return payload, metadata
        except (urllib.error.URLError, TimeoutError) as error:
            failures.append(str(error))
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Unable to retrieve {url}: {failures}")


def links(payload: bytes, base_url: str) -> list[dict]:
    markup = payload.decode("utf-8", errors="replace")
    found = []
    for match in re.finditer(r'<a\b[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', markup, re.DOTALL | re.IGNORECASE):
        title = html.unescape(re.sub(r"<[^>]+>", " ", match.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        if title and PATTERN.search(title):
            found.append({"title": title, "url": urllib.parse.urljoin(base_url, html.unescape(match.group(1)))})
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("filename")
    parser.add_argument("--titles", action="store_true")
    parser.add_argument("--text", action="store_true")
    arguments = parser.parse_args()
    payload, metadata = download(arguments.url, arguments.filename)
    print(json.dumps(metadata, ensure_ascii=False))
    if arguments.titles:
        selected = links(payload, arguments.url)
        (ROOT / "sources" / f"{arguments.filename}.titles.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for entry in selected:
            print(json.dumps(entry, ensure_ascii=False))
    if arguments.text:
        markup = payload.decode("utf-8", errors="replace")
        markup = re.sub(r"<(script|style)\b.*?</\1>", "", markup, flags=re.DOTALL | re.IGNORECASE)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", markup))
        print(re.sub(r"\s+", " ", plain).strip())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# sensitive_tool.py
import argparse
import json
import time
from pathlib import Path
from typing import Any

import ahocorasick
import httpx

_CACHE_DIR = Path.home() / ".cache" / "sensitive-lexicon"
_WORDS_FILE = _CACHE_DIR / "words.txt"

_API_TREE = "https://api.github.com/repos/konsheng/Sensitive-lexicon/git/trees/main?recursive=1"
_RAW_BASE = "https://raw.githubusercontent.com/konsheng/Sensitive-lexicon/main/"

_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.5
_CACHE_MAX_AGE = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                print(f"  retry {attempt + 1}/{_MAX_RETRIES - 1} after {wait:.1f}s ({exc})")
                time.sleep(wait)
    raise RuntimeError(f"Failed after {_MAX_RETRIES} attempts: {last_exc}") from last_exc


def _merge_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并重叠 / 包含命中，同起点优先取最长词。"""
    hits.sort(key=lambda x: (x["start"], -(x["end"] - x["start"])))

    result: list[dict[str, Any]] = []
    last_end = -1

    for hit in hits:
        if hit["start"] >= last_end:
            result.append(hit)
            last_end = hit["end"]
        elif hit["end"] > last_end:
            result[-1] = {
                "word": result[-1]["word"],
                "start": result[-1]["start"],
                "end": hit["end"],
            }
            last_end = hit["end"]

    return result


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SensitiveDetector:
    """
    基于 Aho-Corasick 的敏感词检测器。

    用法：
        detector = SensitiveDetector()                   # 自动加载/更新缓存
        detector = SensitiveDetector(auto_update=False)  # 只用本地缓存，不联网
        detector = SensitiveDetector(extra_words=["自定义词"])  # 追加自定义词

    公开方法：
        detector.contains(text)        -> bool
        detector.find_all(text)        -> list[dict]
        detector.replace(text, repl)   -> str
        detector.add_words(words)      重建 automaton
        detector.update()              强制从远端刷新
    """

    def __init__(
        self,
        cache_dir: Path = _CACHE_DIR,
        cache_max_age: int = _CACHE_MAX_AGE,
        auto_update: bool = True,
        extra_words: list[str] | None = None,
    ) -> None:
        self._words_file = cache_dir / "words.txt"
        self._cache_max_age = cache_max_age
        self._automaton: ahocorasick.Automaton | None = None

        cache_dir.mkdir(parents=True, exist_ok=True)

        if auto_update and self._is_cache_stale():
            self._fetch_and_save()

        words = self._load_words()
        if extra_words:
            words = list(set(words) | {w.strip() for w in extra_words if w.strip()})

        self._automaton = self._build_automaton(words)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def contains(self, text: str) -> bool:
        """快速判断文本是否包含敏感词。"""
        for _ in self._automaton.iter(text):
            return True
        return False

    def find_all(self, text: str) -> list[dict[str, Any]]:
        """返回所有命中位置，重叠区间已合并。"""
        hits = []
        for end_index, word in self._automaton.iter(text):
            start_index = end_index - len(word) + 1
            hits.append({"word": word, "start": start_index, "end": end_index + 1})
        return _merge_hits(hits)

    def replace(self, text: str, repl: str = "*") -> str:
        """将敏感词替换为 repl（单字符按长度重复，多字符整体替换）。"""
        hits = self.find_all(text)
        if not hits:
            return text

        out: list[str] = []
        pos = 0
        for hit in hits:
            out.append(text[pos:hit["start"]])
            length = hit["end"] - hit["start"]
            out.append(repl * length if len(repl) == 1 else repl)
            pos = hit["end"]
        out.append(text[pos:])
        return "".join(out)

    def add_words(self, words: list[str]) -> None:
        """动态追加自定义词，重建 automaton。"""
        existing = self._load_words()
        merged = list(set(existing) | {w.strip() for w in words if w.strip()})
        self._automaton = self._build_automaton(merged)

    def update(self) -> None:
        """强制从远端刷新词库并重建 automaton。"""
        words = self._fetch_and_save()
        self._automaton = self._build_automaton(words)

    @property
    def word_count(self) -> int:
        """当前词库词条数。"""
        return self._automaton.get_stats()["words_count"]  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _is_cache_stale(self) -> bool:
        if not self._words_file.exists():
            return True
        return time.time() - self._words_file.stat().st_mtime > self._cache_max_age

    def _load_words(self) -> list[str]:
        if not self._words_file.exists():
            return []
        return [w for w in self._words_file.read_text(encoding="utf-8").splitlines() if w]

    def _fetch_and_save(self, prefix: str = "Vocabulary/") -> list[str]:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            tree = _get_with_retry(client, _API_TREE).json()
            paths = [
                item["path"]
                for item in tree.get("tree", [])
                if item.get("type") == "blob"
                and item.get("path", "").startswith(prefix)
                and item.get("path", "").lower().endswith(".txt")
            ]

            words: set[str] = set()
            for path in paths:
                resp = _get_with_retry(client, _RAW_BASE + path)
                for line in resp.text.splitlines():
                    word = line.strip()
                    if word and not word.startswith("#"):
                        words.add(word)

        result = sorted(words)
        self._words_file.write_text("\n".join(result), encoding="utf-8")
        print(f"updated words={len(result)} cache={self._words_file}")
        return result

    @staticmethod
    def _build_automaton(words: list[str]) -> ahocorasick.Automaton:
        automaton = ahocorasick.Automaton()
        for word in words:
            if word:
                automaton.add_word(word, word)
        automaton.make_automaton()
        return automaton


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="敏感词检测工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("update", help="强制刷新本地词库缓存")

    p_detect = sub.add_parser("detect", help="检测文本中的敏感词")
    p_detect.add_argument("text")

    p_replace = sub.add_parser("replace", help="替换文本中的敏感词")
    p_replace.add_argument("text")
    p_replace.add_argument("--repl", default="*", help="替换字符（默认 *）")

    return parser


def main() -> None:
    args = _build_cli().parse_args()

    if args.cmd == "update":
        SensitiveDetector(auto_update=True).update()
        return

    detector = SensitiveDetector()

    if args.cmd == "detect":
        hits = detector.find_all(args.text)
        print(json.dumps({"contains": bool(hits), "hits": hits}, ensure_ascii=False, indent=2))

    elif args.cmd == "replace":
        print(detector.replace(args.text, args.repl))


if __name__ == "__main__":
    main()

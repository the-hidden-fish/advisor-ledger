#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import time
import random
import logging

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELTAS_DIR = ROOT / "deltas"
DEDUP_DIR = ROOT / "dedup"
ENV_PATH = ROOT / "secrets" / "review_api.env"
STATS_FILE = ROOT / "dedup_stats.json"

MAX_TOKENS = 8192
REQUEST_TIMEOUT = 180
MAX_PAIR_PRODUCT = 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SYSTEM_PROMPT = """You identify rephrasings in a single edit to a community Chinese document. Given a list of DELETED paragraphs (ghosts) and a list of INSERTED paragraphs from one edit, find any ghost-insert pairs that are clearly the SAME statement being rephrased — typo fix, wording adjustment, clarification, translation, minor rewrite. Be conservative: only pair when the two say essentially the same thing. Do NOT pair unrelated statements that merely share topic or target.

Return ONLY JSON in this exact shape:
{"pairs": [{"ghost_index": <int>, "insert_index": <int>, "note": "<why, <=40 chars>"}]}

Indices are 0-based positions in the lists below. If nothing matches, return {"pairs": []}. No other text."""


def load_env(path: Path) -> dict:
    return {
        k.strip(): v.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for k, v in [line.split("=", 1)]
    }


def collect_ghosts_inserts(delta: dict):
    ghosts, inserts = {}, {}
    for op in delta["operations"]:
        if op["op"] in ("delete", "replace"):
            for p in op.get("paragraphs", []) + op.get("from_paragraphs", []):
                ghosts.setdefault(p["content_hash"], p)
        if op["op"] in ("insert", "replace"):
            for p in op.get("paragraphs", []) + op.get("to_paragraphs", []):
                inserts.setdefault(p["content_hash"], p)
    return list(ghosts.values()), list(inserts.values())


def truncate_text(text: str, max_len: int = 500) -> str:
    return text if len(text) <= max_len else text[:max_len] + "..."


def simple_similarity(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def call_chat(url, model, api_key, system, user):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "top_p": 1.0,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "advisor-ledger/0.1",
        },
    )

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def call_chat_with_retry(url, model, api_key, system, user, retries=3):
    for i in range(retries):
        try:
            return call_chat(url, model, api_key, system, user)
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** i + random.random())


def extract_json(text: str):
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        return None


def update_stats(n_pairs, skipped_reason):
    stats = json.loads(STATS_FILE.read_text()) if STATS_FILE.exists() else {}
    stats["total_runs"] = stats.get("total_runs", 0) + 1
    stats["total_pairs"] = stats.get("total_pairs", 0) + n_pairs
    if skipped_reason:
        stats.setdefault("skips", {}).setdefault(skipped_reason, 0)
        stats["skips"][skipped_reason] += 1
    STATS_FILE.write_text(json.dumps(stats, indent=2))


def print_summary(pairs):
    if not pairs:
        print("No deduplications found.")
        return
    print("\nMatched Pairs:")
    for i, p in enumerate(pairs, 1):
        print(f"{i}. {p['note']}")
        print(f"   GHOST: {p['ghost_text'][:60]}")
        print(f"   INSERT: {p['insert_text'][:60]}\n")


def dedup_delta(delta_path: Path, dry_run=False):
    env = load_env(ENV_PATH)
    delta = json.loads(delta_path.read_text(encoding="utf-8"))

    ghosts, inserts = collect_ghosts_inserts(delta)
    logging.info(f"{delta_path.name} → ghosts={len(ghosts)}, inserts={len(inserts)}")

    ts = delta["to"]["captured_at_utc"]
    out_path = DEDUP_DIR / ts[:4] / ts[5:7] / ts[8:10] / delta["source_id"] / f"{ts}.dedup.json"

    skipped_reason, pairs, finish_reason, tokens = None, [], None, None

    if dry_run:
        skipped_reason = "dry_run"
    elif not ghosts:
        skipped_reason = "no ghosts"
    elif not inserts:
        skipped_reason = "no inserts"
    elif len(ghosts) * len(inserts) > MAX_PAIR_PRODUCT:
        skipped_reason = "too many"
    else:
        ghost_text = "\n".join(f"[{i}] {truncate_text(g['text'])}" for i, g in enumerate(ghosts))
        insert_text = "\n".join(f"[{i}] {truncate_text(p['text'])}" for i, p in enumerate(inserts))

        try:
            resp = call_chat_with_retry(
                env["REVIEW_API_URL"],
                env["REVIEW_API_MODEL"],
                env["REVIEW_API_KEY"],
                SYSTEM_PROMPT,
                f"DELETED:\n{ghost_text}\n\nINSERTED:\n{insert_text}",
            )

            choice = resp["choices"][0]
            finish_reason = choice.get("finish_reason")
            tokens = resp.get("usage", {}).get("completion_tokens")

            parsed = extract_json(choice["message"].get("content", ""))

            if parsed:
                for p in parsed.get("pairs", []):
                    gi, ii = p.get("ghost_index"), p.get("insert_index")
                    if isinstance(gi, int) and isinstance(ii, int):
                        if 0 <= gi < len(ghosts) and 0 <= ii < len(inserts):
                            if simple_similarity(ghosts[gi]["text"], inserts[ii]["text"]) > 0.3:
                                pairs.append({
                                    "ghost_hash": ghosts[gi]["content_hash"],
                                    "insert_hash": inserts[ii]["content_hash"],
                                    "note": (p.get("note") or "")[:80],
                                    "ghost_text": ghosts[gi]["text"],
                                    "insert_text": inserts[ii]["text"],
                                })
            else:
                skipped_reason = "parse_failed"

        except Exception as e:
            skipped_reason = f"error: {e}"

    out = {
        "source_id": delta["source_id"],
        "delta_ts": ts,
        "reviewed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": env.get("REVIEW_API_MODEL"),
        "n_ghosts": len(ghosts),
        "n_inserts": len(inserts),
        "pairs": pairs,
        "finish_reason": finish_reason,
        "completion_tokens": tokens,
        "skipped_reason": skipped_reason,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    update_stats(len(pairs), skipped_reason)
    return out_path, pairs, skipped_reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("delta_path", nargs="?")
    ap.add_argument("--latest")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.latest:
        deltas = sorted(DELTAS_DIR.rglob(f"*/{args.latest}/*.delta.json"))
        if not deltas:
            print("No deltas found", file=sys.stderr)
            return 0
        delta_path = deltas[-1]
    elif args.delta_path:
        delta_path = Path(args.delta_path).resolve()
    else:
        ap.error("Provide --latest or delta path")

    out, pairs, skip = dedup_delta(delta_path, args.dry_run)

    print_summary(pairs)
    print(f"[ok] {delta_path.name} → {len(pairs)} pairs, skipped={skip}")
    print(f"Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
SYSTEM_PROMPT = """You identify rephrasings in a single edit to a community Chinese document. Given a list of DELETED paragraphs (ghosts) and a list of INSERTED paragraphs from one edit, find any ghost-insert pairs that are clearly the SAME statement being rephrased — typo fix, wording adjustment, clarification, translation, minor rewrite. Be conservative: only pair when the two say essentially the same thing. Do NOT pair unrelated statements that merely share topic or target.

Return ONLY JSON in this exact shape:
{"pairs": [{"ghost_index": <int>, "insert_index": <int>, "note": "<why, <=40 chars>"}]}

Indices are 0-based positions in the lists below. If nothing matches, return {"pairs": []}. No other text."""


def load_env(path: Path) -> dict:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def collect_ghosts_inserts(delta: dict) -> tuple[list[dict], list[dict]]:
    """Pull all deleted + inserted paragraph objects out of a delta, dedup by content_hash."""
    ghosts: dict[str, dict] = {}
    inserts: dict[str, dict] = {}
    for op in delta["operations"]:
        if op["op"] == "delete":
            for p in op["paragraphs"]:
                ghosts.setdefault(p["content_hash"], p)
        elif op["op"] == "insert":
            for p in op["paragraphs"]:
                inserts.setdefault(p["content_hash"], p)
        elif op["op"] == "replace":
            for p in op["from_paragraphs"]:
                ghosts.setdefault(p["content_hash"], p)
            for p in op["to_paragraphs"]:
                inserts.setdefault(p["content_hash"], p)
    return list(ghosts.values()), list(inserts.values())


def call_chat(url: str, model: str, api_key: str, system: str, user: str) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.0,
            "top_p": 1.0,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "advisor-ledger/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def dedup_delta(delta_path: Path) -> tuple[Path, int, str | None]:
    env = load_env(ENV_PATH)
    delta = json.loads(delta_path.read_text(encoding="utf-8"))
    ghosts, inserts = collect_ghosts_inserts(delta)
    ts = delta["to"]["captured_at_utc"]
    out_path = (
        DEDUP_DIR
        / ts[:4]
        / ts[5:7]
        / ts[8:10]
        / delta["source_id"]
        / f"{ts}.dedup.json"
    )

    skipped_reason: str | None = None
    pairs: list[dict] = []
    finish_reason: str | None = None
    tokens: int | None = None

    if not ghosts:
        skipped_reason = "no ghosts"
    elif not inserts:
        skipped_reason = "no inserts"
    elif len(ghosts) * len(inserts) > MAX_PAIR_PRODUCT:
        skipped_reason = "too many"
    else:
        ghost_text = "\n".join(f"[{i}] {g['text']}" for i, g in enumerate(ghosts))
        insert_text = "\n".join(f"[{i}] {p['text']}" for i, p in enumerate(inserts))
        user_msg = f"DELETED (ghosts):\n{ghost_text}\n\nINSERTED:\n{insert_text}"
        try:
            resp = call_chat(
                env["REVIEW_API_URL"],
                env["REVIEW_API_MODEL"],
                env["REVIEW_API_KEY"],
                SYSTEM_PROMPT,
                user_msg,
            )
            choice = resp["choices"][0]
            finish_reason = choice.get("finish_reason")
            tokens = resp.get("usage", {}).get("completion_tokens")
            content = choice["message"].get("content") or ""
            parsed = extract_json(content)
            if parsed and isinstance(parsed.get("pairs"), list):
                for p in parsed["pairs"]:
                    try:
                        gi, ii = int(p["ghost_index"]), int(p["insert_index"])
                        if 0 <= gi < len(ghosts) and 0 <= ii < len(inserts):
                            pairs.append(
                                {
                                    "ghost_hash": ghosts[gi]["content_hash"],
                                    "insert_hash": inserts[ii]["content_hash"],
                                    "note": (p.get("note") or "")[:80],
                                    "ghost_text": ghosts[gi]["text"],
                                    "insert_text": inserts[ii]["text"],
                                }
                            )
                    except (KeyError, TypeError, ValueError):
                        continue
            else:
                skipped_reason = f"parse_failed (finish={finish_reason}, tok={tokens})"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            skipped_reason = f"transport_error: {e!r}"
        except Exception as e:  # noqa: BLE001
            skipped_reason = f"unexpected: {e!r}"

    out = {
        "source_id": delta["source_id"],
        "delta_ts": ts,
        "reviewed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": env.get("REVIEW_API_MODEL"),
        "n_ghosts": len(ghosts),
        "n_inserts": len(inserts),
        "pairs": pairs,
        "finish_reason": finish_reason,
        "completion_tokens": tokens,
        "skipped_reason": skipped_reason,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path, len(pairs), skipped_reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("delta_path", nargs="?")
    ap.add_argument("--latest", metavar="SOURCE_ID")
    args = ap.parse_args()

    if args.latest:
        deltas = sorted(DELTAS_DIR.rglob(f"*/{args.latest}/*.delta.json"))
        if not deltas:
            print(f"no deltas for {args.latest}", file=sys.stderr)
            return 0
        delta_path = deltas[-1]
    elif args.delta_path:
        delta_path = Path(args.delta_path).resolve()
    else:
        ap.error("provide --latest SOURCE_ID or a delta path")

    out, n_pairs, skip = dedup_delta(delta_path)
    tag = f"skipped={skip}" if skip else f"pairs={n_pairs}"
    print(f"[ok] {delta_path.name}: {tag} -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

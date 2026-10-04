"""Seed the video console with the probe results, once.

    python3 scripts/seed_video_jobs.py --data-dir ./data

Run this *after* the bridge has booted once with the video feature: the
``video_jobs`` table is created by the schema on startup, and this script only
writes rows.  It is idempotent — every insert is an upsert keyed by job id — so
running it twice changes nothing.

The three jobs are the probing phase's actual output, kept as the first rows so
the console opens on real data instead of an empty table:

- ``video_probe_h3max`` — first end-to-end success (MiniMax-H3-Max, ~50s turn).
- ``video_probe_h3``    — stream reset mid-turn; the upstream completed anyway
                          and the file was recovered by a free drive poll.
- ``video_probe_hailuo``— the turn was accepted, credits were spent, but no
                          artefact ever landed; recorded as failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path


def _total(probe: dict, key: str) -> float:
    credit = (probe.get(key) or {}).get("credit") or {}
    return float(credit.get("total", -1))


def _before(probe: dict) -> float:
    """The balance before the turn.

    The live read sometimes fails while the grant breakdown still succeeds — the
    h3max probe hit exactly that — so the before-side falls back to summing the
    grants, which is what the total would have been.
    """
    total = _total(probe, "before")
    if total >= 0:
        return total
    grants = (probe.get("before") or {}).get("grants") or []
    if grants:
        return round(sum(float(g.get("granted", 0)) for g in grants), 3)
    return -1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()
    root = Path(args.data_dir)
    repo = Path(__file__).resolve().parent.parent

    db_path = root / "bridge.sqlite3"
    videos_dir = root / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    def load(path: Path) -> dict:
        return json.loads(path.read_text())

    h3max = load(repo / "data/probe/20261003-221417/MiniMax-H3-Max.json")
    h3 = load(repo / "data/probe/resume/batch-MiniMax-H3.json")

    rows = [
        {
            "id": "video_probe_h3max",
            "prompt": "雨后纸船（探测）",
            "model": "MiniMax-H3-Max",
            "duration": 6,
            "ratio": "16:9",
            "resolution": "768P",
            "account_id": "f3533bf46cc7",
            "session_id": h3max.get("session_id", ""),
            "status": "done",
            "detail": "探测：首个端到端成功，回合 49.9s，产物随后落地",
            "error": "",
            "node_id": h3max.get("node_id", ""),
            "media_name": "video_probe_h3max.mp4",
            "file_size": (h3max.get("download") or {}).get("bytes", 0),
            "credit_before": _before(h3max),
            "credit_after": _total(h3max, "after"),
            "started_at_ms": h3max.get("started_ms", 0),
            "src": Path(h3max["download"]["local"]),
        },
        {
            "id": "video_probe_h3",
            "prompt": "雨后纸船（探测）",
            "model": "MiniMax-H3",
            "duration": 6,
            "ratio": "16:9",
            "resolution": "768P",
            "account_id": "713a0b309653",
            "session_id": h3.get("session_id", ""),
            "status": "done",
            "detail": "探测：SSE 中途断连，上游仍完整执行；免费轮询网盘取回成品",
            "error": "",
            "node_id": h3.get("node_id", ""),
            "media_name": "video_probe_h3.mp4",
            "file_size": (h3.get("download") or {}).get("bytes", 0),
            "credit_before": _before(h3),
            "credit_after": _total(h3, "after"),
            "started_at_ms": h3.get("started_ms", 0),
            "src": Path(h3["download"]["local"]),
        },
        {
            "id": "video_probe_hailuo",
            "prompt": "雨后纸船（探测）",
            "model": "MiniMax-Hailuo-2.3",
            "duration": 6,
            "ratio": "16:9",
            "resolution": "768P",
            "account_id": "8cd32f50232d",
            "session_id": "448448314581198",
            "status": "failed",
            "detail": "探测：create_session 响应被连接重置；上游受理并扣分，但会话从未提交视频任务，网盘无产物。",
            "error": "no artefact within the polling window",
            "node_id": "",
            "media_name": "",
            "file_size": 0,
            "credit_before": 5998.733,
            "credit_after": -1.0,
            "started_at_ms": 1791037230000,
            "src": None,
        },
    ]

    now = time.time()
    copied = []
    for row in rows:
        src = row.pop("src")
        if src is not None and src.is_file():
            dest = videos_dir / row["media_name"]
            if not dest.exists():
                shutil.copy2(src, dest)
            copied.append(dest.name)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO video_jobs (id, prompt, model, duration, ratio, resolution,"
                " account_id, session_id, status, detail, error, node_id, media_name,"
                " file_size, credit_before, credit_after, started_at_ms, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status, detail=excluded.detail,"
                " error=excluded.error, node_id=excluded.node_id, media_name=excluded.media_name,"
                " file_size=excluded.file_size, credit_after=excluded.credit_after,"
                " updated_at=excluded.updated_at",
                (
                    row["id"], row["prompt"], row["model"], row["duration"], row["ratio"],
                    row["resolution"], row["account_id"], row["session_id"], row["status"],
                    row["detail"], row["error"], row["node_id"], row["media_name"],
                    row["file_size"], row["credit_before"], row["credit_after"],
                    row["started_at_ms"], now, now,
                ),
            )
        # The probe registered these two files in the media table as a stopgap;
        # the job rows are now the authoritative record, so the duplicates go.
        conn.execute("DELETE FROM media WHERE id IN ('19dc24d9877f','a26c113807a5')")
        conn.commit()
    finally:
        conn.close()

    print(f"seeded {len(rows)} video jobs; copied {copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

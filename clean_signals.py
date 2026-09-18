"""
One-off cleanup: re-run the current (strict) looks_like_trading_text()
filter against every row already sitting in the `signals` table, and
delete the ones that no longer qualify.

Needed because tightening the filter only changes what gets inserted
*going forward* -- rows inserted under the old, looser filter (hype
text, update-only posts, anything without a full TP+SL structure) stay
in the table until something removes them.

Usage (run from the project root, with the venv active):

    .venv/bin/python -m scripts.clean_signals            # dry run, prints what WOULD be deleted
    .venv/bin/python -m scripts.clean_signals --confirm   # actually deletes

Dry run first. Read the printed list before passing --confirm --
this is irreversible.
"""

import argparse
import sys

from app.agent_services.telegram_signal_monitor import looks_like_trading_text
from app.data.supabase_client import get_client

PAGE_SIZE = 500


def fetch_all_signals(client):
    rows = []
    start = 0
    while True:
        resp = (
            client.table("signals")
            .select("id, channel, raw_text, confidence_score, created_at")
            .order("created_at", desc=False)
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually delete the rows that fail the current filter (default: dry run only)",
    )
    args = parser.parse_args()

    client = get_client()
    rows = fetch_all_signals(client)
    print(f"fetched {len(rows)} row(s) from signals\n")

    to_delete = [r for r in rows if not looks_like_trading_text(r.get("raw_text") or "")]
    to_keep = len(rows) - len(to_delete)

    print(f"would keep:   {to_keep}")
    print(f"would delete: {len(to_delete)}\n")

    if not to_delete:
        print("nothing to delete.")
        return

    for r in to_delete:
        preview = (r.get("raw_text") or "").replace("\n", " ")[:80]
        print(f"  [{r.get('channel')}] {preview}")

    if not args.confirm:
        print("\nDRY RUN -- nothing deleted. Re-run with --confirm to actually delete these rows.")
        return

    ids = [r["id"] for r in to_delete]
    # Supabase .in_() has a practical size limit -- delete in chunks.
    deleted = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        client.table("signals").delete().in_("id", chunk).execute()
        deleted += len(chunk)
    print(f"\ndeleted {deleted} row(s).")


if __name__ == "__main__":
    sys.exit(main())
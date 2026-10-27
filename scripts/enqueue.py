"""Поставить N заданий в очередь (для демо).

python scripts/enqueue.py 20
docker compose exec api python scripts/enqueue.py 20
"""

import argparse
import os
import random

import httpx

GENRES = ["lo-fi beat", "ambient pad", "synthwave loop", "jazz piano", "drum'n'bass break"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, help="сколько заданий создать")
    parser.add_argument(
        "--api-url", default=os.environ.get("API_URL", "http://localhost:8000"), help="адрес api"
    )
    args = parser.parse_args()

    with httpx.Client(base_url=args.api_url, timeout=10.0) as client:
        for i in range(1, args.count + 1):
            genre = random.choice(GENRES)
            resp = client.post(
                "/jobs",
                json={
                    "prompt": f"Track #{i}: {genre}, {random.randint(70, 170)} bpm",
                    "params": {"duration_sec": random.choice([15, 30, 60])},
                },
            )
            resp.raise_for_status()
            job = resp.json()
            print(f"{i:>3}. {job['id']}  {job['status']}  {job['prompt']}")
    print(f"enqueued {args.count} jobs")


if __name__ == "__main__":
    main()

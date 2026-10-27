"""Mock GPU-воркер: забирает задания из api, «генерирует» аудио и сообщает результат.

Запуск: `python -m worker.main`. Настройки — переменные окружения (см. .env.example).

Схема цикла:
    claim ──204──► пауза POLL_INTERVAL ± джиттер ──► claim ...
      │
      200
      ▼
    «генерация» 1–5 с ──► complete (или fail с вероятностью FAIL_RATE) ──► claim ...

Остановка (SIGTERM/SIGINT): новых заданий не берём, текущее дорабатываем и отчитываемся.
В Go то же самое делают через signal.NotifyContext + проверку ctx.Done() в цикле.
"""

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
import sys
import time
from typing import Any

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
WORKER_ID = os.environ.get("WORKER_ID") or socket.gethostname()
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.1"))

BACKOFF_INITIAL = 0.5  # секунд до первого повтора
BACKOFF_MAX = 30.0  # потолок паузы между повторами
REPORT_MAX_RETRIES = 10  # сколько раз пытаться отправить результат, прежде чем сдаться

log = logging.getLogger(WORKER_ID)


class FatalError(Exception):
    """Ошибка, которую повтором не исправить (например, неверный токен)."""


class Worker:
    def __init__(self, client: httpx.AsyncClient, worker_id: str, stop: asyncio.Event) -> None:
        self.client = client
        self.worker_id = worker_id
        self.stop = stop  # выставляется обработчиком сигнала, аналог ctx.Done()
        self.stats = {"succeeded": 0, "failed": 0, "lost": 0}

    async def run(self) -> None:
        log.info("started, api=%s fail_rate=%.2f", API_URL, FAIL_RATE)
        while not self.stop.is_set():
            job = await self.claim()
            if job is None:
                # Очередь пуста: немного ждём. Джиттер — чтобы воркеры не стучались в api
                # синхронно, одной «волной».
                await self.sleep(POLL_INTERVAL * random.uniform(0.8, 1.2))
                continue
            await self.process(job)
        log.info("stopped gracefully, stats=%s", self.stats)

    async def sleep(self, seconds: float) -> bool:
        """Пауза, которую прерывает сигнал остановки. True — если пора выходить."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def request(
        self, path: str, payload: dict[str, Any], *, abort_on_stop: bool, max_retries: int | None
    ) -> httpx.Response | None:
        """POST с повторами при сетевых ошибках и 5xx (экспоненциальный backoff + джиттер).

        Ответы 2xx/4xx возвращаются как есть: повтор их не изменит.
        None — сдались (исчерпаны попытки или пришёл сигнал остановки при abort_on_stop).
        """
        delay = BACKOFF_INITIAL
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self.client.post(path, json=payload)
                if resp.status_code < 500:
                    return resp
                reason = f"HTTP {resp.status_code}"
            except httpx.TransportError as exc:  # api недоступен, таймаут, обрыв соединения
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

            if max_retries is not None and attempt >= max_retries:
                log.error("giving up on %s after %d attempts (%s)", path, attempt, reason)
                return None
            wait = min(delay, BACKOFF_MAX) * random.uniform(0.5, 1.5)
            log.warning("api unavailable (%s), retry #%d in %.1fs", reason, attempt, wait)
            if abort_on_stop:
                if await self.sleep(wait):
                    return None
            else:
                # Отчёт о результате стараемся доставить даже при остановке.
                await asyncio.sleep(wait)
            delay *= 2

    async def claim(self) -> dict[str, Any] | None:
        resp = await self.request(
            "/jobs/claim", {"worker_id": self.worker_id}, abort_on_stop=True, max_retries=None
        )
        if resp is None or resp.status_code == 204:
            return None
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            raise FatalError("api rejected WORKER_TOKEN (401)")
        log.error("unexpected claim response %d: %s", resp.status_code, resp.text)
        return None

    async def process(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        short = job_id[:8]
        log.info("claimed  job=%s attempt=%d prompt=%r", short, job["attempts"], job["prompt"][:40])

        started = time.monotonic()
        # «Генерация» на GPU. Сигнал остановки её НЕ прерывает: задание дорабатываем.
        await asyncio.sleep(random.uniform(1, 5))
        took = time.monotonic() - started

        if random.random() < FAIL_RATE:
            outcome = "failed"
            path = f"/jobs/{job_id}/fail"
            payload = {"worker_id": self.worker_id, "error": "mock GPU error: CUDA out of memory"}
        else:
            outcome = "succeeded"
            path = f"/jobs/{job_id}/complete"
            result = f"[audio-mock] {job['prompt'][:50]} (by {self.worker_id})"
            payload = {"worker_id": self.worker_id, "result": result}

        resp = await self.request(
            path, payload, abort_on_stop=False, max_retries=REPORT_MAX_RETRIES
        )
        if resp is None:
            # Не смогли доставить результат. Задание останется running, его вернёт reaper,
            # и оно выполнится ещё раз (at-least-once).
            self.stats["lost"] += 1
            log.error("could not report job=%s, reaper will requeue it", short)
        elif resp.status_code == 200:
            self.stats[outcome] += 1
            log.info("%-9s job=%s in %.1fs", outcome, short, took)
        elif resp.status_code == 409:
            # Пока мы работали, reaper счёл нас мёртвыми и отдал задание другому.
            self.stats["lost"] += 1
            log.warning("job=%s was taken away from us: %s", short, resp.json().get("detail"))
        elif resp.status_code == 401:
            raise FatalError("api rejected WORKER_TOKEN (401)")
        else:
            self.stats["lost"] += 1
            log.error("unexpected response %d for job=%s: %s", resp.status_code, short, resp.text)


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Сетевые подробности httpx нам не нужны: у воркера свои понятные логи.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal(sig: signal.Signals) -> None:
        log.info("received %s, finishing current job and exiting", sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # На Windows add_signal_handler не поддерживается — там просто Ctrl+C.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, on_signal, sig)

    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    async with httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=10.0) as client:
        try:
            await Worker(client, WORKER_ID, stop).run()
        except FatalError as exc:
            log.critical("%s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

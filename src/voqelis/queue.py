from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

from .domain import AudioJob


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    queued: int
    reserved: int


class JobQueue:
    """Bounded in-memory queue with global and per-user back-pressure."""

    def __init__(self, *, max_pending_jobs: int, max_pending_per_user: int) -> None:
        self._queue: asyncio.Queue[AudioJob] = asyncio.Queue(maxsize=max_pending_jobs)
        self._max_pending_jobs = max_pending_jobs
        self._max_pending_per_user = max_pending_per_user
        self._reserved_total = 0
        self._reserved_by_user: Counter[int] = Counter()
        self._lock = asyncio.Lock()

    async def reserve(self, user_id: int) -> bool:
        async with self._lock:
            if self._reserved_total >= self._max_pending_jobs:
                return False
            if self._reserved_by_user[user_id] >= self._max_pending_per_user:
                return False

            self._reserved_total += 1
            self._reserved_by_user[user_id] += 1
            return True

    async def release(self, user_id: int) -> None:
        async with self._lock:
            self._reserved_total = max(self._reserved_total - 1, 0)

            self._reserved_by_user[user_id] -= 1
            if self._reserved_by_user[user_id] <= 0:
                del self._reserved_by_user[user_id]

    async def put(self, job: AudioJob) -> None:
        await self._queue.put(job)

    async def get(self) -> AudioJob:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def snapshot(self) -> QueueSnapshot:
        return QueueSnapshot(
            queued=self._queue.qsize(),
            reserved=self._reserved_total,
        )

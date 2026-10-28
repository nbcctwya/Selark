"""Single-loop bounded admission and FIFO execution slots."""

import asyncio
from collections import deque

from selark.errors import GatewayError


class Admission:
    def __init__(self, limits):
        self.limits = limits
        self.active = 0
        self.reserved = 0
        self.waiters = deque()
        self.tasks = set()
        self.accepting = True
        self.callers = {}

    def acquire_caller(self, caller, maximum):
        active = self.callers.get(caller, 0)
        if maximum is not None and active >= maximum:
            raise GatewayError(
                429, "caller_concurrency_limit", "此调用方的在途请求数已达上限（包含排队）。"
            )
        self.callers[caller] = active + 1

    def release_caller(self, caller):
        remaining = self.callers[caller] - 1
        if remaining:
            self.callers[caller] = remaining
        else:
            del self.callers[caller]

    def reserve(self):
        if not self.accepting:
            raise GatewayError(503, "service_shutting_down", "服务正在关闭，请稍后重试。")
        if self.reserved >= self.limits.max_concurrent_requests + self.limits.max_queued_requests:
            raise GatewayError(429, "server_overloaded", "Selark 执行及等待队列已满，请稍后重试。")
        self.reserved += 1
        self.tasks.add(asyncio.current_task())

    async def acquire(self):
        if not self.accepting:
            raise GatewayError(503, "service_shutting_down", "服务正在关闭。")
        if self.active < self.limits.max_concurrent_requests and not self.waiters:
            self.active += 1
            return
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        try:
            async with asyncio.timeout(self.limits.queue_timeout_seconds):
                await asyncio.shield(future)
        except BaseException as exc:
            if future.done() and not future.cancelled() and future.exception() is None:
                self.release()  # A slot was granted concurrently with cancellation.
            else:
                if future in self.waiters:
                    self.waiters.remove(future)
                future.cancel()
            if isinstance(exc, TimeoutError):
                raise GatewayError(
                    503, "queue_timeout", "等待 Selark 执行名额超时，未发送上游。"
                ) from None
            raise

    def release(self):
        self.active -= 1
        while self.waiters:
            future = self.waiters.popleft()
            if not future.done():
                self.active += 1
                future.set_result(None)
                break

    def leave(self):
        self.reserved -= 1
        self.tasks.discard(asyncio.current_task())

    def health(self):
        return {
            "active": self.active,
            "queued": len(self.waiters),
            "admitted": self.reserved,
            "max_active": self.limits.max_concurrent_requests,
            "max_queued": self.limits.max_queued_requests,
            "accepting": self.accepting,
        }

    async def shutdown(self):
        self.accepting = False
        while self.waiters:
            future = self.waiters.popleft()
            if not future.done():
                future.set_exception(GatewayError(503, "service_shutting_down", "服务正在关闭。"))
        pending = set(self.tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.limits.shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

"""115 API 限流与重试策略。"""

import random
import threading
import time
from functools import wraps
from typing import Callable

from app.log import logger


class RateLimiter:
    """保证请求最小间隔，并增加随机抖动。"""

    def __init__(self, min_interval: float = 1.5, jitter_ratio: float = 0.3):
        self.min_interval = min_interval
        self.jitter_ratio = jitter_ratio
        self.last_request_time = 0.0
        self._lock = threading.Lock()

    def _get_jittered_interval(self) -> float:
        jitter = self.min_interval * self.jitter_ratio
        return self.min_interval + random.uniform(-jitter, jitter)

    def wait(self) -> None:
        with self._lock:
            elapsed = time.time() - self.last_request_time
            target_interval = self._get_jittered_interval()
            if elapsed < target_interval:
                time.sleep(target_interval - elapsed)
            self.last_request_time = time.time()

    def acquire(self) -> None:
        self.wait()


def retry_on_failure(
        max_retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        retryable_exceptions: tuple = (Exception,),
):
    """按指数退避重试指定异常。"""

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as error:
                    if attempt >= max_retries:
                        logger.warning(
                            f"请求失败，已达最大重试次数 ({max_retries + 1}): {error}"
                        )
                        raise
                    logger.info(
                        f"请求失败 (尝试 {attempt + 1}/{max_retries + 1}): "
                        f"{error}, {delay:.1f}秒后重试..."
                    )
                    time.sleep(delay)
                    delay *= backoff_factor

        return wrapper

    return decorator

"""Redis-based task queue system for distributed task management.

This module provides a RedisTaskQueue class that enables distributed task
management and coordination using Redis as the underlying infrastructure.
"""
import time
import pickle
import base64
import logging
from typing import Any, Callable, Optional, List
from functools import cached_property
from dataclasses import dataclass
from enum import Enum
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from redis import StrictRedis as Redis

logger = logging.getLogger(__name__)


class TaskExecutePolicy(Enum):
    """Defines different policies for task execution.

    Attributes:
        Local: Execute task only locally
        Remote: Execute task only remotely
        LocalFirst: Try to execute locally first, then remotely if it fails
        RemoteFirst: Try to execute remotely first, then locally if it fails
        Auto: Choose execution mode based on whether a remote listener exists
    """
    Local = 0x00
    Remote = 0x01
    LocalFirst = 0x02
    RemoteFirst = 0x03
    Auto = 0x04


@dataclass
class RedisTask:
    """Represents a task to be processed via the RedisTaskQueue.

    Encapsulates task details like ID, category (name), parameters, and its current state
    (progress, result, error). It includes methods for saving state and updating progress.

    Attributes:
        id: Unique identifier for this specific task instance.
        name: Categorical name for the task (used for routing in the queue).
        params: Dictionary containing task-specific input parameters.
        expiry: Absolute Unix timestamp when the task should be considered expired.
               Used both locally and remotely to timeout waiting operations.
        result: Stores the successful return value of the task execution.
        error: Stores any exception raised during task execution.
        update_progress_time: Timestamp of the last progress update.
        current_progress: Current progress value (e.g., items processed).
        total_progress: Total expected value for completion (e.g., total items).
        _save: Internal callback function provided by RedisTaskQueue to persist
               the task's state (result, error, progress) back to Redis.
    """
    id: str
    name: str
    params: dict
    expiry: float
    result: Any = None
    error: Any = None
    update_progress_time: float = 0.0
    current_progress: float = 0.0
    total_progress: float = 100.0
    _save: Callable[[], None] = None

    def save(self):
        """Save the current state of the task to Redis."""
        if self._save is not None:
            self._save()

    def update(self, current_progress: float, total_progress: float):
        """Update the progress of the task.

        Args:
            current_progress: The current progress value
            total_progress: The total progress value for completion

        Raises:
            TimeoutError: If the task has expired
        """
        self.current_progress = current_progress
        self.total_progress = total_progress
        self.update_progress_time = time.time()
        if self.expiry < time.time():
            raise TimeoutError(f'Task {self.id} in {self.name} has expired')
        self.save()


class RedisTaskQueue:
    """Provides a distributed task queue using Redis lists and key/value storage.

    Enables submitting tasks (represented by `RedisTask` objects) to named queues
    and having them processed either locally (if `task_fn` is provided) or by
    remote listeners polling the corresponding Redis list.

    Key Concepts:
    - Task Queues: Redis lists (`<prefix>|<suffix>|task-queue|<name>`) where task IDs
                   are pushed for remote workers.
    - Task Data: Serialized `RedisTask` objects stored in Redis keys
                 (`<prefix>|<suffix>|task-result:<id>`) with an expiry time.
                 This stores parameters, progress, results, and errors.
    - Listeners: Remote workers use BLPOP on queue lists. To signal their presence,
                 they periodically set a listener key (`<prefix>|<suffix>|task-listen|<name>`).
    - Execution Policies (`TaskExecutePolicy`): Control whether a task is executed
                                            locally, remotely, or attempts one then the other.
                                            `Auto` mode checks for a listener key.
    - Task Function (`task_fn`): A user-provided function that takes a `RedisTask`
                                 and performs the actual work locally.

    Attributes:
        redis: StrictRedis client instance.
        prefix: Prefix for all Redis keys.
        suffix: Suffix for Redis keys (default: 'task-queue').
        timeout: Default expiry/timeout for tasks and listener keys (seconds).
        interval: Polling interval for remote task fetching (seconds).
        task_fn: Callable[[RedisTask], Any] to execute tasks locally.
    """

    def __init__(self, redis: Redis, prefix: str, suffix='task-queue', timeout=300, interval=5,
                 task_fn: Callable[[RedisTask], Any] = None):
        """Initialize a RedisTaskQueue instance.

        Args:
            redis: The Redis client used for interacting with Redis.
            prefix: The prefix to be added to the task queue key.
            suffix: The suffix to be added to the task queue key. Default is 'task-queue'.
            timeout: The query timeout in seconds. Default is 300s.
            interval: The query interval in seconds. Default is 5s.
            task_fn: A function to execute tasks locally. Takes a RedisTask and returns Any.
        """
        self.redis = redis
        self.prefix = prefix
        self.suffix = suffix
        self.timeout = timeout
        self.interval = interval
        self.task_fn = task_fn

    def build_task(self, id: str, name: str, params: dict) -> RedisTask:
        """Create a new RedisTask instance with the given parameters.

        Args:
            id: Unique identifier for the task
            name: Name of the task category
            params: Dictionary of parameters for the task

        Returns:
            A new RedisTask instance with a save function configured
        """
        task = RedisTask(id=id, name=name, params=params, expiry=time.time() + self.timeout)
        task._save = lambda: self.set_task(task)
        return task

    def execute_task_remotely(self, task: RedisTask, timeout: Optional[float] = None, once: bool = False) -> Any:
        """Execute a task remotely by pushing it to the queue.

        Args:
            task: The RedisTask to execute
            timeout: Optional timeout in seconds, defaults to self.timeout
            once: Whether to delete the result after getting it

        Returns:
            The result of the task

        Raises:
            TimeoutError: If the task times out
            Exception: Any exception raised during task execution
        """
        self.set_task(task)
        self.redis.rpush(self._queue_key(task.name), task.id)
        if timeout is None:
            timeout = self.timeout
        while timeout >= 0:
            time.sleep(self.interval)
            result = self.get_task(task.id, once)
            if result is not None:
                if result.error is not None:
                    raise result.error
                elif result.result is not None:
                    return result.result
            timeout -= self.interval
        raise TimeoutError(f'Task {task.id} in {task.name} has expired')

    def execute_task_locally(self, task: RedisTask, timeout: Optional[float] = None) -> Any:
        """Execute a task locally using the task_fn.

        Args:
            task: The RedisTask to execute
            timeout: Optional timeout in seconds, updates task.expiry if provided

        Returns:
            The result of the task

        Raises:
            Exception: Any exception raised during task execution
        """
        if timeout is not None:
            task.expiry = time.time() + timeout
        try:
            task.result = self.task_fn(task)
            return task.result
        except Exception as e:
            task.error = e
            raise e
        finally:
            task.save()

    @cached_property
    def _queue_prefix(self) -> str:
        """Get the prefix for queue keys.

        Returns:
            The queue prefix
        """
        return f'{self.prefix}|{self.suffix}|task-queue'

    def _queue_key(self, name: str) -> str:
        """Generate a queue key for the given task name.

        Args:
            name: The task name.

        Returns:
            The formatted queue key.
        """
        return f'{self._queue_prefix}|{name}'

    def _queue_name(self, key: str) -> str:
        """Extract the queue name from a queue key.

        Args:
            key: The queue key.

        Returns:
            The queue name.

        Raises:
            AssertionError: If the key doesn't start with the queue prefix.
        """
        assert key.startswith(self._queue_prefix)
        return key[len(self._queue_prefix) + 1:]

    def _queue_listen_name(self, name: str) -> str:
        """Generate a listen name for the given task name.

        Args:
            name: The task name.

        Returns:
            The formatted listen name.
        """
        return f'{self.prefix}|{self.suffix}|task-listen|{name}'

    def set_queue_listened(self, name: str) -> None:
        """Set the queue as being listened to for the given task name.

        Args:
            name: The task name.
        """
        self.redis.setex(self._queue_listen_name(name), self.timeout, '1')

    def _result_key(self, task_id: str) -> str:
        """Generate a result key for the given task ID.

        Args:
            task_id: The task ID.

        Returns:
            The formatted result key.
        """
        return f'{self.prefix}|{self.suffix}|task-result:{task_id}'

    def set_task(self, task: RedisTask) -> str:
        """Save a task to Redis.

        Args:
            task: The RedisTask to save.

        Returns:
            The task ID.
        """
        task._save = None
        t = pickle.dumps(task)
        result = str(base64.b64encode(t), 'ascii')
        self.redis.setex(self._result_key(task.id), self.timeout, result)
        return task.id

    def get_task(self, task_id: str, once: bool = False) -> Optional[RedisTask]:
        """Get a task from Redis.

        Args:
            task_id: The task ID.
            once: Whether to delete the task after getting it.

        Returns:
            The RedisTask, or None if no task is available.
        """
        get = self.redis.getdel if once else self.redis.get
        result = get(self._result_key(task_id))
        if result is None:
            return None
        t = pickle.loads(base64.b64decode(result))
        t._save = lambda: self.set_task(t)
        return t

    def has_task(self, task_id: str) -> bool:
        """Check if a task exists.

        Args:
            task_id: The task ID.

        Returns:
            True if the task exists, False otherwise.
        """
        return self.redis.exists(self._result_key(task_id)) > 0

    @contextmanager
    def _executor_context(self, max_workers: int = 128):
        """Create a ThreadPoolExecutor context manager.

        This is a helper method for testing and internal use.

        Args:
            max_workers: The maximum number of worker threads.

        Yields:
            The ThreadPoolExecutor instance.
        """
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            yield executor

    def listen(self, names: List[str], workers: int = 128, event: Optional[Event] = None) -> None:
        """Listen for tasks on the specified queues.

        This method continuously polls the specified queues for tasks,
        and executes tasks locally when they are received.

        Args:
            names: The names of the queues to listen to.
            workers: The number of worker threads to use. Default is 128.
            event: An event to signal when to stop listening. Default is None.
        """
        with self._executor_context(max_workers=workers) as executor:
            while event is None or not event.is_set():
                queue_names = [self._queue_key(name) for name in names]
                while True:
                    task = self.redis.blpop(queue_names, timeout=self.interval)
                    if task is not None:
                        _queue_key, task_id = task
                        task = self.get_task(task_id)
                        executor.submit(self.execute_task_locally, task)
                    else:
                        break
                for name in names:
                    self.set_queue_listened(name)
                time.sleep(self.interval)

    def query(self, id: str, name: str, params: dict, timeout: Optional[float] = None,
              policy: TaskExecutePolicy = TaskExecutePolicy.Auto, once: bool = False) -> Any:
        """Execute a task according to the specified policy.

        This method provides a flexible way to execute tasks with different
        strategies based on the specified policy.

        Args:
            id: The task ID.
            name: The task name.
            params: The task parameters.
            timeout: Optional timeout override.
            policy: The execution policy to use.
            once: Whether to delete the result after getting it.

        Returns:
            The result of the task.

        Raises:
            Exception: Any exception raised during task execution.
        """
        t = self.build_task(id, name, params)
        match policy:
            case TaskExecutePolicy.Local:
                return self.execute_task_locally(t, timeout)
            case TaskExecutePolicy.Remote:
                return self.execute_task_remotely(t)
            case TaskExecutePolicy.LocalFirst:
                try:
                    return self.execute_task_locally(t, timeout)
                except Exception as e:
                    logger.exception(f'Failed to execute task {t.id} in {t.name} locally: {e}')
                    return self.execute_task_remotely(t, timeout)
            case TaskExecutePolicy.RemoteFirst:
                try:
                    return self.execute_task_remotely(t, timeout)
                except Exception as e:
                    logger.exception(f'Failed to execute task {t.id} in {t.name} remotely: {e}')
                    return self.execute_task_locally(t, timeout)
            case TaskExecutePolicy.Auto:
                if self.redis.exists(self._queue_listen_name(name)):
                    return self.execute_task_remotely(t, timeout)
                else:
                    return self.execute_task_locally(t, timeout)

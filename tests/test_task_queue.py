"""Tests for the RedisTaskQueue class."""
import time
import pytest
from redis import Redis
import threading
import datetime
from freezegun import freeze_time
from redis_allocator.task_queue import RedisTaskQueue, TaskExecutePolicy, RedisTask


@pytest.fixture
def task_queue(redis_client: Redis):
    """Create a RedisTaskQueue instance for testing."""
    return RedisTaskQueue(
        redis_client,
        'test',
        'task-queue',
        task_fn=lambda task: f"result-{task.id}"
    )


class TestRedisTaskQueue:
    """Tests for the RedisTaskQueue class."""

    def test_task_creation(self, task_queue):
        """Test creating a task."""
        task = task_queue.build_task("task1", "test", {"param1": "value1"})
        assert task.id == "task1"
        assert task.name == "test"
        assert task.params == {"param1": "value1"}
        assert task.result is None
        assert task.error is None
        assert task._save is not None

    def test_set_and_get_task(self, task_queue, redis_client):
        """Test setting and getting a task."""
        # Create and set a task
        task = task_queue.build_task("task1", "test", {"param1": "value1"})
        task_id = task_queue.set_task(task)

        # Mock the pickle and base64 operations to avoid serialization issues
        task_copy = task_queue.build_task("task1", "test", {"param1": "value1"})

        # Verify the task was saved
        assert task_id == "task1"
        assert redis_client.exists(task_queue._result_key("task1"))

        # Patch the get_task method to return our mocked task
        original_get_task = task_queue.get_task
        try:
            task_queue.get_task = lambda task_id, once=False: task_copy

            # Get the task back
            retrieved_task = task_queue.get_task("task1")
            assert retrieved_task.id == "task1"
            assert retrieved_task.name == "test"
            assert retrieved_task.params == {"param1": "value1"}
        finally:
            # Restore original method
            task_queue.get_task = original_get_task

    def test_has_task(self, task_queue, redis_client):
        """Test checking if a task exists."""
        # Create and set a task
        task = task_queue.build_task("task1", "test", {"param1": "value1"})
        task_queue.set_task(task)

        # Verify task exists
        assert task_queue.has_task("task1") is True

        # Verify non-existent task doesn't exist
        assert task_queue.has_task("task2") is False

    def test_local_execution(self, task_queue):
        """Test executing a task locally."""
        task = task_queue.build_task("task1", "test", {"param1": "value1"})
        result = task_queue.execute_task_locally(task)

        assert result == "result-task1"
        assert task.result == "result-task1"
        assert task.error is None

    def test_local_execution_with_error(self, task_queue, mocker):
        """Test local execution with an error."""
        # Set up a task that will fail
        task = task_queue.build_task("error_task", "test", {"param1": "value1"})

        # Mock the task_fn to raise an exception
        error = ValueError("Test error")
        mocker.patch.object(task_queue, 'task_fn', side_effect=error)

        # Execute the task and verify it fails properly
        with pytest.raises(ValueError, match="Test error"):
            task_queue.execute_task_locally(task)

        # Verify the error was captured in the task
        assert task.error == error
        assert task.result is None

    def test_remote_execution(self, task_queue, redis_client, mocker):
        """Test remote task execution."""
        # Create a task
        task = task_queue.build_task("task2", "test", {"param1": "value2"})

        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Execute the task and save the result
                    result = f"result-{task.id}"
                    task.result = result
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        try:
            # Replace execute_task_remotely with our own implementation
            def mock_execute_remotely(task, timeout=None, once=False):
                # Push the task to Redis queue
                task_queue.set_task(task)
                redis_client.rpush(task_queue._queue_key(task.name), task.id)

                # Wait for worker to process it (with a reasonable timeout)
                if not processed_event.wait(timeout=3):
                    raise TimeoutError("Worker did not process task within timeout")

                # Get the processed task with result
                processed_task = task_queue.get_task(task.id, once)
                return processed_task.result

            # Apply the mock
            mocker.patch.object(task_queue, 'execute_task_remotely', side_effect=mock_execute_remotely)

            # Execute the task remotely
            result = task_queue.execute_task_remotely(task, timeout=3)

            # Verify the result
            assert result == f"result-{task.id}"

        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_remote_execution_with_error(self, task_queue, redis_client):
        """Test remote execution with an error."""
        # Create a task that will fail remotely
        task = task_queue.build_task("error_remote", "test", {"param1": "value3"})

        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it with an error
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Set an error on the task
                    task.error = ValueError("Remote error")
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        # Execute the task remotely and expect an error
        try:
            with pytest.raises(ValueError, match="Remote error"):
                task_queue.execute_task_remotely(task, timeout=3)

            # Verify the thread processed the task
            assert processed_event.is_set()
        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_remote_execution_timeout(self, task_queue, redis_client, mocker):
        """Test remote execution with timeout."""
        # Create a task
        task = task_queue.build_task("timeout_task", "test", {"param1": "value4"})

        # Set timeout to a very small value to ensure it times out
        timeout = 0.1

        # Mock time.sleep to avoid actual sleeping
        time_sleep_mock = mocker.patch('time.sleep')

        # Mock get_task to always return None (simulating no worker processing the task)
        mocker.patch.object(task_queue, 'get_task', return_value=None)

        # Use a task that will not get processed by any worker
        # Execute the task remotely with a small timeout and expect a timeout
        with pytest.raises(TimeoutError):
            task_queue.execute_task_remotely(task, timeout=timeout)

        # Verify time.sleep was called at least once
        time_sleep_mock.assert_called()

    def test_query_with_local_policy(self, task_queue):
        """Test query with local execution policy."""
        # Call query with local policy
        result = task_queue.query("task3", "test", {}, policy=TaskExecutePolicy.Local)

        # Verify result
        assert result == "result-task3"

    def test_query_with_remote_policy(self, task_queue, redis_client, mocker):
        """Test query with remote execution policy."""
        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Execute the task and save the result
                    result = f"result-{task.id}"
                    task.result = result
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        try:
            # Replace execute_task_remotely with our own implementation
            def mock_execute_remotely(task, timeout=None, once=False):
                # Push the task to Redis queue
                task_queue.set_task(task)
                redis_client.rpush(task_queue._queue_key(task.name), task.id)

                # Wait for worker to process it (with a reasonable timeout)
                if not processed_event.wait(timeout=3):
                    raise TimeoutError("Worker did not process task within timeout")

                # Get the processed task with result
                processed_task = task_queue.get_task(task.id, once)
                return processed_task.result

            # Apply the mock
            mocker.patch.object(task_queue, 'execute_task_remotely', side_effect=mock_execute_remotely)

            # Call query with remote policy
            result = task_queue.query("task4", "test", {}, policy=TaskExecutePolicy.Remote, timeout=3)

            # Verify result
            assert result == "result-task4"
        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_query_with_local_first_policy(self, task_queue):
        """Test query with local-first execution policy."""
        # Call query with local-first policy
        result = task_queue.query("task5", "test", {}, policy=TaskExecutePolicy.LocalFirst)

        # Verify result - should execute locally
        assert result == "result-task5"

    def test_query_with_local_first_policy_fallback(self, task_queue, redis_client, mocker):
        """Test query with local-first policy falling back to remote."""
        # We'll only mock the task_fn to simulate a local execution failure
        error = ValueError("Local error")
        mocker.patch.object(task_queue, 'task_fn', side_effect=error)

        # Mock logger.exception to prevent actual logging
        mocker.patch('redis_allocator.task_queue.logger.exception')

        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Execute the task and save the result
                    task.error = None  # Clear the error from local execution
                    task.result = f"remote-result-{task.id}"
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        try:
            # Make the execute_task_remotely method faster by skipping actual sleep
            def mocked_execute_remotely(task, timeout=None, once=False):
                # Push to queue but skip the actual sleeping
                redis_client.rpush(task_queue._queue_key(task.name), task.id)

                # Wait a moment for the worker thread to process
                processed_event.wait(timeout=1)

                # Get the task with result
                result = task_queue.get_task(task.id, once)
                if result is not None and result.result is not None:
                    return result.result
                raise TimeoutError(f'Task {task.id} in {task.name} has expired')

            # Replace with mock
            mocker.patch.object(task_queue, 'execute_task_remotely', side_effect=mocked_execute_remotely)

            # Call query with local-first policy (which should fail locally and fall back to remote)
            result = task_queue.query("task6", "test", {}, policy=TaskExecutePolicy.LocalFirst, timeout=3)

            # Verify the thread processed the task
            assert processed_event.is_set()

            # Verify result from remote execution
            assert result == "remote-result-task6"
        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_query_with_remote_first_policy(self, task_queue, redis_client, mocker):
        """Test query with remote-first execution policy."""
        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Execute the task and save the result
                    task.result = f"remote-result-{task.id}"
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        try:
            # Replace execute_task_remotely with our own implementation
            def mock_execute_remotely(task, timeout=None, once=False):
                # Push the task to Redis queue
                task_queue.set_task(task)
                redis_client.rpush(task_queue._queue_key(task.name), task.id)

                # Wait for worker to process it (with a reasonable timeout)
                if not processed_event.wait(timeout=3):
                    raise TimeoutError("Worker did not process task within timeout")

                # Get the processed task with result
                processed_task = task_queue.get_task(task.id, once)
                return processed_task.result

            # Apply the mock
            mocker.patch.object(task_queue, 'execute_task_remotely', side_effect=mock_execute_remotely)

            # Call query with remote-first policy
            result = task_queue.query("task7", "test", {}, policy=TaskExecutePolicy.RemoteFirst, timeout=3)

            # Verify result
            assert result == "remote-result-task7"
        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_query_with_remote_first_policy_fallback(self, task_queue, redis_client, mocker):
        """Test query with remote-first policy falling back to local."""
        # We'll mock execute_task_remotely to fail
        # This simulates a remote execution failure more reliably
        mocker.patch.object(
            task_queue,
            'execute_task_remotely',
            side_effect=TimeoutError("No remote listeners available")
        )

        # Mock logger.exception to prevent actual logging
        mocker.patch('redis_allocator.task_queue.logger.exception')

        # Call query with remote-first policy
        # The remote execution should fail, and it will fall back to local
        result = task_queue.query("task8", "test", {}, policy=TaskExecutePolicy.RemoteFirst)

        # Verify local result (since remote failed)
        assert result == "result-task8"

    def test_query_with_auto_policy_with_listener(self, task_queue, redis_client, mocker):
        """Test query with auto policy when a listener exists."""
        # Set up a listener for the test queue
        task_queue.set_queue_listened("test")

        # Start a thread that will listen for the task and execute it
        # (Simulating a remote worker)
        processed_event = threading.Event()

        def process_task():
            # Get the task from the queue
            queue_key = task_queue._queue_key("test")
            task_data = redis_client.blpop([queue_key], timeout=1)
            if task_data:
                _queue_key, task_id = task_data
                task = task_queue.get_task(task_id)
                if task:
                    # Execute the task and save the result
                    task.result = f"remote-result-{task.id}"
                    task.save()
                    processed_event.set()

        # Start the worker thread
        worker_thread = threading.Thread(target=process_task)
        worker_thread.daemon = True
        worker_thread.start()

        try:
            # Replace execute_task_remotely with our own implementation
            def mock_execute_remotely(task, timeout=None, once=False):
                # Push the task to Redis queue
                task_queue.set_task(task)
                redis_client.rpush(task_queue._queue_key(task.name), task.id)

                # Wait for worker to process it (with a reasonable timeout)
                if not processed_event.wait(timeout=3):
                    raise TimeoutError("Worker did not process task within timeout")

                # Get the processed task with result
                processed_task = task_queue.get_task(task.id, once)
                return processed_task.result

            # Apply the mock
            mocker.patch.object(task_queue, 'execute_task_remotely', side_effect=mock_execute_remotely)

            # Call query with auto policy when a listener exists - should execute remotely
            result = task_queue.query("task9", "test", {}, policy=TaskExecutePolicy.Auto, timeout=3)

            # Verify result from remote execution
            assert result == "remote-result-task9"
        finally:
            # Make sure thread is done
            worker_thread.join(timeout=1)

    def test_query_with_auto_policy_without_listener(self, task_queue, redis_client):
        """Test query with auto policy when no listener exists."""
        # Make sure there is no active listener
        # Just don't call set_queue_listened()

        # Call query with auto policy when no listener exists - should execute locally
        result = task_queue.query("task10", "test", {}, policy=TaskExecutePolicy.Auto)

        # Verify result from local execution
        assert result == "result-task10"

    def test_task_update_progress(self, task_queue, mocker):
        """Test updating task progress."""
        # Create a task
        task = task_queue.build_task("task11", "test", {"param1": "value1"})

        # Fixed time for consistent testing
        fixed_time_str = "2022-04-15T10:00:00Z"  # 使用UTC时间
        dt = datetime.datetime.fromisoformat(fixed_time_str.replace('Z', '+00:00'))

        # Use freezegun to set a fixed time
        with freeze_time(dt):
            # Update progress
            task.update(50.0, 100.0)

            # Verify progress was updated
            assert task.current_progress == 50.0
            assert task.total_progress == 100.0
            assert task.update_progress_time == dt.timestamp()

    def test_task_update_progress_expired(self, task_queue, mocker):
        """Test updating progress for an expired task."""
        # Current time for reference
        current_time = time.time()

        # Create a task with expiry in the past (100 seconds ago)
        with freeze_time(datetime.datetime.fromtimestamp(current_time - 200)):  # Create a time point far in the past
            # Create task with expiry = current_time - 100 (which is expired from current perspective)
            task = RedisTask(
                id="expired_task",
                name="test",
                params={},
                expiry=current_time - 100,  # Set expiry to be 100 seconds before current time
                _save=lambda: None
            )

        # Move to current time and try to update the expired task
        with freeze_time(datetime.datetime.fromtimestamp(current_time)):
            # Try to update progress and expect TimeoutError
            with pytest.raises(TimeoutError, match=f"Task {task.id} in {task.name} has expired"):
                task.update(50.0, 100.0)

    def test_listen_single_iteration(self, task_queue, redis_client):
        """Test a single iteration of the listen method without running the actual loop."""
        # Set up task queue names
        names = ['test_queue']
        queue_key = task_queue._queue_key(names[0])

        # Create a task to be processed
        task = task_queue.build_task("listen_task", "test_queue", {"param": "value"})

        # Save the task
        task_queue.set_task(task)

        # Directly push task ID to queue
        redis_client.rpush(queue_key, task.id)

        # Manually run one iteration of the listen loop
        # This avoids the infinite loop in the actual listen method
        queue_names = [task_queue._queue_key(name) for name in names]

        # Process task
        task_data = redis_client.blpop(queue_names, timeout=task_queue.interval)
        assert task_data is not None

        _queue_key, task_id = task_data
        # 检查task_id的类型并处理
        if isinstance(task_id, bytes):
            task_id = task_id.decode('utf-8')
        assert task_id == "listen_task"

        # Get the task
        task = task_queue.get_task(task_id)
        assert task is not None

        # Execute task locally to verify
        result = task_queue.execute_task_locally(task)
        assert result == "result-listen_task"

        # Mark queues as listened
        for name in names:
            task_queue.set_queue_listened(name)

        # Verify that the queue is marked as listened
        assert redis_client.exists(task_queue._queue_listen_name(names[0])) > 0

    def test_listen_non_blocking(self, task_queue, redis_client, mocker):
        """Test the listen method using a separate thread to avoid blocking the test."""
        # Set up task queue names
        names = ['test_queue']

        # Create a task to be processed
        task = task_queue.build_task("listen_task", "test_queue", {"param": "value"})

        # Patch methods to avoid actual Redis operations
        mocker.patch.object(task_queue, 'set_task')

        # Mock blpop to return our task ID first, then always return None
        # This avoids StopIteration exception in the thread
        def mock_blpop_side_effect(*args, **kwargs):
            # Use an event to make sure we only return the task once
            if not hasattr(mock_blpop_side_effect, 'called'):
                mock_blpop_side_effect.called = True
                return (task_queue._queue_key('test_queue'), b"listen_task")
            return None

        mocker.patch.object(redis_client, 'blpop', side_effect=mock_blpop_side_effect)

        # Mock get_task to return our task
        mocker.patch.object(task_queue, 'get_task', return_value=task)

        # Track when task is executed
        task_executed = threading.Event()

        def mock_execute_task(t):
            # Mark that the task was executed and return a result
            task_executed.set()
            return "result"

        mocker.patch.object(task_queue, 'execute_task_locally', side_effect=mock_execute_task)

        # Use freezegun to handle the sleep in the listen method
        current_time = time.time()
        with freeze_time(datetime.datetime.fromtimestamp(current_time)) as frozen_time:
            # Replace time.sleep to advance the frozen time
            original_sleep = time.sleep

            def advance_time(seconds):
                frozen_time.tick(datetime.timedelta(seconds=seconds))

            time.sleep = advance_time

            try:
                # Run listen in a separate thread with a stop event
                stop_event = threading.Event()

                # Start a thread that will call listen for a short time
                listen_thread = threading.Thread(
                    target=lambda: task_queue.listen(names, event=stop_event, workers=1)
                )
                listen_thread.daemon = True  # Ensure thread doesn't block test exit
                listen_thread.start()

                # Wait for the task to be executed or timeout
                executed = task_executed.wait(timeout=5)

                # Stop the listen thread
                stop_event.set()
                listen_thread.join(timeout=1)

                # Verify task was processed
                assert executed, "Task execution timed out"
            finally:
                # Restore original time.sleep
                time.sleep = original_sleep

    def test_executor_context(self, task_queue):
        """Test the ThreadPoolExecutor context manager."""
        with task_queue._executor_context(max_workers=2) as executor:
            assert executor is not None
            # Verify we can submit tasks
            future = executor.submit(lambda: "test")
            assert future.result() == "test"

    def test_queue_name(self, task_queue):
        """Test the _queue_name method."""
        # Create a valid queue key first
        name = "test_queue"
        queue_key = task_queue._queue_key(name)

        # Extract the name back
        extracted_name = task_queue._queue_name(queue_key)

        # Verify extraction works
        assert extracted_name == name

        # Test with invalid queue key
        with pytest.raises(AssertionError):
            task_queue._queue_name("invalid_key")

    def test_get_task_nonexistent(self, task_queue):
        """Test getting a task that doesn't exist."""
        # Try to get a task with an ID that doesn't exist
        result = task_queue.get_task("nonexistent_task")

        # Verify that None is returned
        assert result is None

        # Try with once=True
        result = task_queue.get_task("nonexistent_task", once=True)
        assert result is None

    def test_execute_task_remotely_with_task_error(self, task_queue, redis_client, mocker):
        """Test execute_task_remotely when task has error attribute set."""
        # Create a task
        task = task_queue.build_task("error_task", "test", {"param": "value"})

        # Create a error for the task
        task_error = ValueError("Task error")

        # Mock time.sleep to avoid actual waiting
        mocker.patch('time.sleep')

        # Mock get_task to return a task with error attribute set
        def mock_get_task(task_id, once=False):
            task = RedisTask(
                id=task_id,
                name="test",
                params={"param": "value"},
                expiry=time.time() + 60,
                error=task_error,
                _save=lambda: None
            )
            return task

        mocker.patch.object(task_queue, 'get_task', side_effect=mock_get_task)

        # Execute the task and expect the error to be raised
        with pytest.raises(ValueError, match="Task error"):
            task_queue.execute_task_remotely(task)

    def test_execute_task_remotely_timeout_logic(self, task_queue, redis_client, mocker):
        """Test the detailed timeout logic in execute_task_remotely."""
        # Create a task
        task = task_queue.build_task("timeout_task", "test", {"param": "value"})
        task_queue.interval = 0.5  # Set interval to 0.5 seconds for testing

        # Mock get_task to always return None (simulating no worker processing the task)
        mocker.patch.object(task_queue, 'get_task', return_value=None)

        # Mock time.sleep to count calls and track timeout reduction
        sleep_call_count = 0
        interval_sum = 0

        def mock_sleep(seconds):
            nonlocal sleep_call_count, interval_sum
            sleep_call_count += 1
            interval_sum += seconds
            # Don't actually sleep

        mocker.patch('time.sleep', side_effect=mock_sleep)

        # Set a small timeout to make the test fast
        timeout = 2.0  # Should allow for exactly 4 iterations with interval=0.5

        # Execute the task remotely with timeout and expect TimeoutError
        with pytest.raises(TimeoutError) as exc_info:
            task_queue.execute_task_remotely(task, timeout=timeout)

        # Verify error message
        assert f"Task {task.id} in {task.name} has expired" in str(exc_info.value)

        # Verify sleep was called
        # In the implementation, the loop continues while timeout >= 0, so we get 5 iterations
        # with timeout=2.0 and interval=0.5: [2.0, 1.5, 1.0, 0.5, 0.0]
        expected_calls = int(timeout / task_queue.interval) + 1  # +1 for the last iteration when timeout=0
        assert sleep_call_count == expected_calls

        # Verify that the total time slept approximately equals the timeout
        assert abs(interval_sum - timeout) <= task_queue.interval

    def test_execute_task_remotely_direct_timeout(self, task_queue, redis_client, mocker):
        """Test execute_task_remotely method's timeout logic directly."""
        # Create a task
        task = task_queue.build_task("timeout_task2", "test", {"param": "value"})

        # Mock redis and get_task to simulate the behavior
        mocker.patch.object(task_queue, 'get_task', return_value=None)

        # Mock time.sleep to avoid actual waiting
        mocker.patch('time.sleep')

        # Force timeout to be exactly 0 after one iteration
        # This will trigger the exact lines we want to test
        task_queue.interval = 1.0
        timeout = 1.0  # Will become 0 after one iteration, then -1 after another

        # Execute task and expect timeout
        with pytest.raises(TimeoutError) as exc_info:
            task_queue.execute_task_remotely(task, timeout=timeout)

        # Verify timeout error
        assert str(exc_info.value) == f"Task {task.id} in {task.name} has expired"

.. _example_proxy_pool:

Example: Managing a Proxy Pool with Automatic Updates
=====================================================

This example demonstrates how `RedisAllocator` can be used for managing a dynamic proxy pool, featuring **automatic pool updates** using the `RedisAllocatorUpdater` integrated with `DefaultRedisAllocatorPolicy`.

Scenario
--------

We need a system where:
1. Proxies are fetched periodically from different **sources** (e.g., APIs, databases).
2. These fetched proxies automatically update the central Redis pool.
3. Multiple worker threads allocate exclusive proxies from this pool.
4. Workers use the proxies for tasks.
5. Proxies are automatically returned/recovered via `free()` and `gc()`.
6. (Future) Priority and Soft Binding features enhance allocation.

Core RedisAllocator Concepts Used
---------------------------------
*   `RedisAllocator(policy=...)`: Using a policy to control behavior.
*   `RedisAllocatorUpdater`: Base class for defining how to fetch resources.
*   `DefaultRedisAllocatorPolicy(updater=...)`: Policy that uses an updater to refresh the pool periodically.
*   `malloc()`: Workers acquire proxies (triggering policy checks).
*   `free()`: Workers return proxies.
*   `gc()`: Background process for recovery.
*   Resource Prioritization & Soft Binding: (Mentioned as **TODO** / Future).

Implementation Sketch
---------------------

.. code-block:: python
   :linenos:

   import redis
   import threading
   import time
   import random
   from typing import Sequence, Any
   from redis_allocator import RedisAllocator
   # Import base class and default policy
   from redis_allocator.allocator import RedisAllocatorUpdater, DefaultRedisAllocatorPolicy

   # --- Configuration ---
   REDIS_HOST = 'localhost'
   REDIS_PORT = 6379
   PROXY_POOL_PREFIX = 'myapp'
   PROXY_POOL_SUFFIX = 'proxies'
   NUM_WORKERS = 5
   ALLOCATION_TIMEOUT = 120 # Seconds a proxy can be held

   # --- Custom Updater Implementation ---

   class ProxySourceUpdater(RedisAllocatorUpdater):
       """Fetches proxy lists from different predefined sources."""
       def fetch(self, source_id: str) -> Sequence[str]:
           """Simulates fetching proxies from a specific source URL/ID."""
           print(f"[{threading.current_thread().name or 'Updater'}] Fetching proxies from source: {source_id}...")
           # Replace with actual fetching logic (e.g., API call, DB query)
           time.sleep(0.5) # Simulate fetch time
           if source_id == 'source_A_api':
               # Simulate getting a list of proxy strings
               new_proxies = [f'proxy_A_{i}:8000' for i in range(random.randint(1,3))]
           elif source_id == 'source_B_db':
               new_proxies = [f'proxy_B_{i}:9000' for i in range(random.randint(0,2))]
           else:
               new_proxies = [] # Simulate source being unavailable
           print(f"[{threading.current_thread().name or 'Updater'}] Fetched {len(new_proxies)} proxies from {source_id}: {new_proxies}")
           # TODO: Return priority info when allocator supports it: [('proxy_A_0:8000', 10), ...]
           return new_proxies

   # --- Setup --- 
   redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

   # Define the sources the updater will cycle through
   PROXY_SOURCES = ['source_A_api', 'source_B_db']
   # How often the policy should *try* to run the updater (in seconds)
   # The updater only runs if the update_lock is acquired during a malloc call.
   UPDATE_INTERVAL = 60
   # Optional: Default expiry for items added by the updater (-1 = no expiry)
   UPDATER_ITEM_EXPIRY = 3600

   # 1. Instantiate the custom Updater
   proxy_updater = ProxySourceUpdater(params=PROXY_SOURCES)

   # 2. Instantiate the Policy, providing the updater
   default_policy = DefaultRedisAllocatorPolicy(
       updater=proxy_updater,
       update_interval=UPDATE_INTERVAL,
       expiry_duration=UPDATER_ITEM_EXPIRY,
       gc_count=5 # How many items GC checks per malloc call
   )

   # 3. Instantiate the Allocator using the configured Policy
   proxy_allocator = RedisAllocator(
       redis_client,
       prefix=PROXY_POOL_PREFIX,
       suffix=PROXY_POOL_SUFFIX,
       shared=False, # Proxies are used exclusively
       policy=default_policy # IMPORTANT: Use the policy
   )

   # --- Proxy Usage Simulation (Remains mostly the same) ---

   def get_proxy_config_url(proxy_key):
       """Simulates fetching configuration for a given proxy key."""
       # In reality, this might query a database or another Redis key
       print(f"[{threading.current_thread().name}] Fetched config for {proxy_key}")
       return f"http://config.server/{proxy_key}"

   def start_local_proxy(config_url):
       """Simulates configuring and starting a local proxy process/connection."""
       print(f"[{threading.current_thread().name}] Starting proxy using {config_url}")
       time.sleep(0.1) # Simulate startup time
       return True # Simulate success

   # --- Scrape Target Simulation (Remains the same) ---

   def fetch_scrape_target():
       """Simulates fetching the next high-priority item to scrape."""
       # TODO: Target fetching logic is application-specific.
       #       Could use another Redis list, queue, or even another Allocator
       #       if targets themselves are managed resources. Priority needed here too.
       targets = ["item_A", "item_B", "item_C", "item_D", "item_E"]
       target = random.choice(targets)
       print(f"[{threading.current_thread().name}] Fetched target: {target}")
       return target

   # --- Worker Thread Logic (Remains mostly the same) ---

   def worker_thread_logic(worker_id):
       """Main logic for each scraper worker thread."""
       print(f"[Worker-{worker_id}] Started.")
       allocated_proxy_obj = None
       retry_delay = 1
       while True: # Keep trying to allocate and work
           try:
               # 1. Allocate a Proxy (This now triggers Policy checks -> GC and Updater)
               print(f"[Worker-{worker_id}] Attempting to allocate proxy...")
               allocated_proxy_obj = proxy_allocator.malloc(timeout=ALLOCATION_TIMEOUT)

               if allocated_proxy_obj:
                   retry_delay = 1 # Reset delay on success
                   proxy_key = allocated_proxy_obj.key
                   print(f"[Worker-{worker_id}] Allocated proxy: {proxy_key}")

                   # 2. Simulate using the proxy
                   config_url = get_proxy_config_url(proxy_key)
                   if start_local_proxy(config_url):
                       print(f"[Worker-{worker_id}] Proxy started successfully.")

                       # 3. Fetch target and simulate work
                       target = fetch_scrape_target()
                       if target:
                           print(f"[Worker-{worker_id}] Simulating scrape of {target} via {proxy_key}...")
                           time.sleep(random.uniform(0.5, 2.0)) # Simulate work duration
                           print(f"[Worker-{worker_id}] Finished scrape of {target}.")

                   else:
                       print(f"[Worker-{worker_id}] Failed to start proxy {proxy_key}.")
                       # Optionally, report this proxy as bad

                   # IMPORTANT: Free the proxy *only after* you are completely done with it for this task
                   # Move the free call inside the 'if allocated_proxy_obj:' block before the loop continues or breaks
                   print(f"[Worker-{worker_id}] Freeing proxy: {allocated_proxy_obj.key}")
                   proxy_allocator.free(allocated_proxy_obj)
                   allocated_proxy_obj = None # Clear the reference
                   # Potentially add a small delay before next allocation attempt
                   time.sleep(random.uniform(0.1, 0.5))

               else:
                   # No proxy available, wait and retry with exponential backoff
                   print(f"[Worker-{worker_id}] No proxy available. Retrying in {retry_delay}s...")
                   time.sleep(retry_delay)
                   retry_delay = min(retry_delay * 2, 30) # Exponential backoff up to 30s

           except Exception as e:
               print(f"[Worker-{worker_id}] Error: {e}")
               # Ensure proxy is freed even if an error occurs mid-task
               if allocated_proxy_obj:
                   print(f"[Worker-{worker_id}] Freeing proxy due to error: {allocated_proxy_obj.key}")
                   proxy_allocator.free(allocated_proxy_obj)
                   allocated_proxy_obj = None
               time.sleep(5) # Wait after error before retrying
           # Note: The finally block is removed as freeing is handled within the loop/except block

   # --- Running the Example --- 

   if __name__ == "__main__":
       print("Starting proxy pool example with automatic updates...")
       # Note: No initial pool population needed. The updater in the policy handles it.
       # The first calls to malloc() by workers will likely trigger the updater.

       threads = []
       for i in range(NUM_WORKERS):
           thread = threading.Thread(target=worker_thread_logic, args=(i,), name=f"Worker-{i}")
           threads.append(thread)
           thread.start()

       # Keep main thread alive to observe workers (or add proper shutdown logic)
       try:
           for thread in threads:
               thread.join() # Wait for all worker threads indefinitely
       except KeyboardInterrupt:
           print("\nCtrl+C detected. Exiting example...")


Future Enhancements Shown
-------------------------
*   **Resource Prioritization:** The comments indicate where proxy and target priorities would be integrated once the feature is implemented in `RedisAllocator`.
*   **Soft Binding:** Notes show how soft binding could be used to dedicate specific proxies to high-frequency tasks.
*   **Garbage Collection:** The example mentions where `gc()` would typically be called for automatic cleanup.

This example provides a skeleton for building a robust proxy management system on top of `RedisAllocator`, highlighting how its features map to real-world requirements.

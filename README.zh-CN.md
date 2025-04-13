# RedisAllocator

## 项目概述

RedisAllocator 是一个基于 Redis 的高效分布式内存分配系统。该系统模拟传统内存分配机制，但将其实现在分布式环境中，使用 Redis 作为底层存储和协调工具。

> **注意**：当前 RedisAllocator 仅支持单 Redis 实例部署。对于 Redis 集群环境，我们建议使用 RedLock 进行分布式锁操作。

### 核心功能

- **分布式锁**：提供强大的分布式锁机制，确保并发环境中的数据一致性
- **资源分配**：实现分布式资源分配系统，支持：
  - 基于优先级的分配
  - 软绑定
  - 垃圾回收
  - 健康检查
- **任务管理**：实现分布式任务队列系统，支持多个工作节点之间的高效任务处理
- **对象分配**：支持带有优先级分配和软绑定的资源分配
- **健康检查**：监控分布式实例的健康状态，自动处理不健康的资源
- **垃圾回收**：自动识别并回收未使用的资源，优化内存使用
- **共享模式**：可配置的分配模式，支持独占和共享资源使用
- **软绑定**：将命名对象与特定资源关联，实现一致性分配

## 文档

完整文档请访问我们的[官方文档站点](https://invoker-bot.github.io/RedisAllocator-python/)。

## 安装

```bash
pip install redis-allocator
```

## 快速开始

### 使用 RedisLock 进行分布式锁定

RedisLock 提供以下重要特性的分布式锁定：

- **自动过期**：锁会在超时期后自动释放，防止客户端失败时出现死锁
- **主动更新要求**：锁持有者必须主动更新锁以维持所有权
- **线程识别**：每个锁可以包含线程标识符来确定所有权
- **重入锁定**：同一线程/进程可以使用 rlock 方法重新获取其拥有的锁

```python
from redis import Redis
from redis_allocator import RedisLock
import threading
import time

# 初始化 Redis 客户端（需要单个 Redis 实例）
redis = Redis(host='localhost', port=6379, decode_responses=True)

# 创建 RedisLock 实例
lock = RedisLock(redis, "myapp", "resource-lock")

# 使用当前线程 ID 作为锁标识符
thread_id = str(threading.get_ident())

# 获取一个 60 秒超时的锁
if lock.lock("resource-123", value=thread_id, timeout=60):
    try:
        # 使用被锁定的资源执行操作
        print("资源锁定成功")
        
        # 对于长时间运行的操作，定期更新锁
        # 以防止超时过期
        for _ in range(5):
            time.sleep(10)  # 执行一些工作
            
            # 通过更新锁延长其生命周期
            lock.update("resource-123", value=thread_id, timeout=60)
            print("锁已更新，超时已延长")
            
        # 使用 rlock 进行重入锁定的示例（因为是同一 thread_id 所以成功）
        if lock.rlock("resource-123", value=thread_id):
            print("成功重新锁定资源")
    finally:
        # 完成后释放锁
        lock.unlock("resource-123")
        print("资源已解锁")
else:
    print("无法获取锁 - 资源正在使用中")
```

**关键概念：**
- 如果锁持有者在超时期内未能更新，锁会自动释放
- 使用 `rlock()` 允许同一线程重新获取其已持有的锁
- 此实现仅适用于单个 Redis 实例（不适用于 Redis 集群）
- 在分布式系统中，每个节点应使用唯一标识符作为锁值

### 使用 RedisAllocator 进行资源管理

```python
from redis import Redis
from redis_allocator import RedisAllocator

# 初始化 Redis 客户端
redis = Redis(host='localhost', port=6379)

# 创建 RedisAllocator 实例
allocator = RedisAllocator(
    redis, 
    prefix='myapp',
    suffix='allocator',
    shared=False  # 资源是否可以共享
)

# 向池中添加资源
allocator.extend(['resource-1', 'resource-2', 'resource-3'])

# 分配资源键（仅返回键）
key = allocator.malloc_key(timeout=120)
if key:
    try:
        # 使用分配的资源
        print(f"已分配资源: {key}")
    finally:
        # 完成后释放资源
        allocator.free_keys(key)

# 使用对象分配资源（返回 RedisAllocatorObject）
allocated_obj = allocator.malloc(timeout=120)
if allocated_obj:
    try:
        # 键作为属性可用
        print(f"已分配资源: {allocated_obj.key}")
        
        # 更新资源的锁超时
        allocated_obj.update(timeout=60)
    finally:
        # 完成后释放资源
        allocator.free(allocated_obj)

# 使用软绑定（将名称与资源关联）
allocator.update_soft_bind("worker-1", "resource-1")
# 稍后...
allocator.unbind_soft_bind("worker-1")

# 垃圾回收（回收未使用的资源）
allocator.gc(count=10)  # 检查 10 个项目进行清理
```

### 共享模式与非共享模式

RedisAllocator 支持两种分配模式：

#### 非共享模式（默认，`shared=False`）
- 资源独占分配给一个客户端/线程
- 分配时，资源被锁定，防止其他客户端使用
- 资源保持锁定状态，直到明确释放或超时过期
- 适合需要独占使用资源的场景

```python
# 非共享分配器（独占资源使用）
exclusive_allocator = RedisAllocator(redis, "myapp", shared=False)

# 当资源被分配时，它被锁定且不能被其他客户端分配
key = exclusive_allocator.malloc_key(timeout=120)
if key:
    # 只有这个客户端可以使用该键，直到它被释放或超时过期
    exclusive_allocator.free_keys(key)
```

#### 共享模式（`shared=True`）
- 资源可以被多个客户端/线程同时使用
- 分配时，资源从空闲列表中可用，但不会被锁定
- 多个客户端可以同时分配和使用相同的资源
- 适合只读资源或支持并发访问的资源

```python
# 共享分配器（并发资源使用）
shared_allocator = RedisAllocator(redis, "myapp", shared=True)

# 资源可以被多个客户端同时访问
key = shared_allocator.malloc_key(timeout=120)
if key:
    # 其他客户端也可以分配和使用这个相同的键
    shared_allocator.free_keys(key)
```

### 软绑定机制

软绑定创建命名对象与分配资源之间的持久关联：

```python
from redis import Redis
from redis_allocator import RedisAllocator, RedisAllocatableClass

# 创建带有名称的自定义可分配类
class MyResource(RedisAllocatableClass):
    def __init__(self, resource_name):
        self._name = resource_name
    
    def set_config(self, key, params):
        # 分配时配置资源
        self.key = key
        self.config = params
    
    @property
    def name(self):
        # 用于软绑定的名称
        return self._name

# 初始化分配器
redis = Redis(host='localhost', port=6379)
allocator = RedisAllocator(redis, "myapp")

# 向池中添加资源
allocator.extend(['resource-1', 'resource-2', 'resource-3'])

# 创建命名资源对象
resource = MyResource("database-connection")

# 第一次分配将从池中分配一个键
allocation1 = allocator.malloc(timeout=60, obj=resource)
print(f"第一次分配: {allocation1.key}")  # 例如 "resource-1"

# 释放资源
allocator.free(allocation1)

# 稍后对相同命名对象的分配将尝试重用相同的键
allocation2 = allocator.malloc(timeout=60, obj=resource)
print(f"第二次分配: {allocation2.key}")  # 将再次是 "resource-1"

# 软绑定的好处:
# 1. 资源亲和性 - 相同对象始终获得相同资源
# 2. 优化缓存和资源重用
# 3. 可预测的资源映射，便于调试
```

软绑定的关键特性：
- 绑定在资源释放后仍然存在，具有可配置的超时时间
- 如果绑定的资源不再可用，会自动分配新资源
- 可通过 `unbind_soft_bind(name)` 显式解除绑定
- 软绑定有自己的超时时间（默认 3600 秒），与资源锁分开

### 使用 RedisTaskQueue 进行分布式任务处理

```python
from redis import Redis
from redis_allocator import RedisTaskQueue, TaskExecutePolicy
import json

# 初始化 Redis 客户端
redis = Redis(host='localhost', port=6379)

# 在工作者中处理任务
def process_task(task):
    # 处理任务（task 是 RedisTask 对象）
    # 可以访问 task.id, task.name, task.params
    # 可以使用 task.update(current, total) 更新进度
    return json.dumps({"result": "processed"})


# 创建任务队列
task_queue = RedisTaskQueue(redis, "myapp", task_fn=process_task)

# 使用查询方法提交任务
result = task_queue.query(
    id="task-123",
    name="example-task",
    params={"input": "data"},
    timeout=300,  # 可选超时（秒）
    policy=TaskExecutePolicy.Auto,  # 执行策略
    once=False  # 获取后是否删除结果
)

# 开始监听任务
task_queue.listen(
    names=["example-task"],  # 要监听的任务名称列表
    workers=128,  # 工作线程数
    event=None  # 可选事件，用于指示何时停止监听
)
```

## 模块

RedisAllocator 由几个模块组成，每个模块提供特定功能：

- **lock.py**：提供分布式锁机制的 `RedisLock` 和 `RedisLockPool`
- **task_queue.py**：实现分布式任务处理的 `RedisTaskQueue`
- **allocator.py**：包含用于资源分配的 `RedisAllocator` 和 `RedisThreadHealthChecker`

### RedisAllocator 架构

RedisAllocator 在 Redis 中以双向链表结构维护资源：
- 可用资源保存在"空闲列表"中
- 在非共享模式下，分配的资源从空闲列表中移除并锁定
- 在共享模式下，分配的资源仍然可供其他客户端分配
- 垃圾收集器定期：
  - 回收锁已过期的锁定资源
  - 根据配置的超时时间移除过期资源
  - 清理分配和锁之间的不一致状态
- 软绑定作为具有自己超时期的独立锁实现

## 路线图

### 第一阶段（已完成）
- [x] 分布式锁机制实现
- [x] 任务队列处理系统
- [x] 资源分配和管理
- [x] 基本健康检查和监控
- [x] 带序列化的对象分配
- [x] 核心组件的单元测试

### 第二阶段（进行中）
- [x] 共享模式的文档改进
- [x] 增强的软绑定机制
- [x] 分配模式的全面测试覆盖
- [ ] 高级分片实现
- [ ] 性能优化和基准测试
- [ ] 增强的错误处理和恢复

### 第三阶段（计划中）
- [ ] 高级垃圾回收策略
- [ ] Redis 集群支持
- [ ] 故障恢复机制
- [ ] 自动资源扩展

### 第四阶段（未来）
- [ ] API 稳定性和向后兼容性
- [ ] 性能监控和调优工具
- [ ] 高级功能（事务支持、数据压缩等）
- [ ] 生产环境验证和案例研究

## 贡献

欢迎贡献和建议！请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 获取更多信息。

## 许可

本项目采用 MIT 许可证 - 详情请参阅 [LICENSE](LICENSE) 文件。

## 联系

如有问题或建议，请通过 GitHub Issues 联系我们。

*English documentation is available at [README.md](README.md)*

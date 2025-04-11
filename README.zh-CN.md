# RedisAllocator

## 项目简介

RedisAllocator 是一个高效的基于 Redis 的分布式内存分配系统。该系统模拟了传统内存分配的机制，但在分布式环境中实现，使用 Redis 作为底层存储和协调工具。

> **注意**：目前，RedisAllocator 仅支持单 Redis 实例部署。对于 Redis 集群环境，我们推荐使用 RedLock 进行分布式锁定操作。

### 核心特性

- **分布式锁机制**: 提供强大的分布式锁机制，确保并发环境下的数据一致性
- **资源分配**: 实现分布式资源分配系统，支持：
  - 基于优先级的分配
  - 软绑定
  - 垃圾回收
  - 健康检查
- **任务管理**: 实现分布式任务队列系统，支持多工作节点的高效任务处理
- **对象分配**: 支持基于优先级的资源分配和软绑定
- **健康检查**: 监控分布式实例的健康状态，自动处理不健康的资源
- **垃圾回收**: 自动识别和回收未使用的资源，优化内存使用

## 安装

```bash
pip install redis-allocator
```

## 快速开始

### 使用 RedisLock 实现分布式锁

```python
from redis import Redis
from redis_allocator import RedisLock

# 初始化 Redis 客户端
redis = Redis(host='localhost', port=6379)

# 创建 RedisLock 实例
lock = RedisLock(redis, "myapp", "resource-lock")

# 获取锁
if lock.lock("resource-123", timeout=60):
    try:
        # 使用锁定的资源进行操作
        print("资源锁定成功")
    finally:
        # 完成后释放锁
        lock.unlock("resource-123")
```

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

# 向资源池添加资源
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

# 分配带对象的资源（返回 RedisAllocatorObject）
allocated_obj = allocator.malloc(timeout=120)
if allocated_obj:
    try:
        # 键可作为属性访问
        print(f"已分配资源: {allocated_obj.key}")
        
        # 更新资源的锁超时时间
        allocated_obj.update(timeout=60)
    finally:
        # 完成后释放资源
        allocator.free(allocated_obj)

# 使用软绑定（将名称与资源关联）
allocator.update_soft_bind("worker-1", "resource-1")
# 稍后...
allocator.unbind_soft_bind("worker-1")

# 垃圾回收（回收未使用的资源）
allocator.gc(count=10)  # 检查10个项目进行清理
```

### 使用 RedisTaskQueue 进行分布式任务处理

```python
from redis import Redis
from redis_allocator import RedisTaskQueue, TaskExecutePolicy
import json

# 初始化 Redis 客户端
redis = Redis(host='localhost', port=6379)

# 处理任务的工作函数
def process_task(task):
    # 处理任务（task 是 RedisTask 对象）
    # 可以访问 task.id, task.name, task.params
    # 可以使用 task.update(current, total) 更新进度
    return json.dumps({"result": "processed"})

# 创建任务队列
task_queue = RedisTaskQueue(redis, "myapp", task_fn=process_task)

# 使用 query 方法提交任务
result = task_queue.query(
    id="task-123",
    name="example-task",
    params={"input": "data"},
    timeout=300,  # 可选的超时时间（秒）
    policy=TaskExecutePolicy.Auto,  # 执行策略
    once=False  # 获取结果后是否删除
)

# 开始监听任务
task_queue.listen(
    names=["example-task"],  # 要监听的任务名称列表
    workers=128,  # 工作线程数量
    event=None  # 可选的事件，用于通知何时停止监听
)
```

## 模块

RedisAllocator 由多个模块组成，每个模块提供特定功能：

- **lock.py**: 提供 `RedisLock` 和 `RedisLockPool` 用于分布式锁机制
- **task_queue.py**: 实现 `RedisTaskQueue` 用于分布式任务处理
- **allocator.py**: 包含 `RedisAllocator` 和 `RedisThreadHealthChecker` 用于资源分配

## 路线图

### 第一阶段 (已完成)
- [x] 分布式锁机制实现
- [x] 任务队列处理系统
- [x] 资源分配和管理
- [x] 基本健康检查和监控
- [x] 对象分配与序列化
- [x] 核心组件单元测试

### 第二阶段 (进行中)
- [ ] 高级分片实现
- [ ] 性能优化和基准测试
- [ ] 文档完善
- [ ] 增强错误处理和恢复

### 第三阶段 (计划中)
- [ ] 高级垃圾回收策略
- [ ] Redis 集群支持
- [ ] 故障恢复机制
- [ ] 自动资源扩展

### 第四阶段 (未来)
- [ ] API 稳定性和向后兼容性
- [ ] 性能监控和调优工具
- [ ] 高级功能（事务支持、数据压缩等）
- [ ] 生产环境验证和案例研究

## 贡献

欢迎贡献代码或提出建议！请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 了解更多信息。

## 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 联系方式

如有问题或建议，请通过 GitHub Issues 联系我们。

*English documentation is available at [README.md](README.md)*

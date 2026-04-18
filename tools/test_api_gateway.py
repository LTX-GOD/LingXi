"""
API网关测试脚本
===============
验证统一网关的限速效果和统计功能
"""

import sys
import os
import time
import threading

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.api_gateway import get_api_gateway, RequestPriority


def test_basic_rate_limit():
    """测试基本限速功能"""
    print("=" * 60)
    print("测试1: 基本限速 (1秒3次)")
    print("=" * 60)

    gateway = get_api_gateway()
    start = time.time()

    for i in range(6):
        wait = gateway.acquire(priority=RequestPriority.NORMAL, endpoint=f"test_{i}")
        elapsed = time.time() - start
        print(f"请求 {i+1}: 等待 {wait:.3f}s, 总耗时 {elapsed:.3f}s")

    total_time = time.time() - start
    print(f"\n6次请求总耗时: {total_time:.3f}s")
    print(f"预期耗时: ~1.0s (前3次立即通过，后3次等待1秒)")

    gateway.print_stats()


def test_priority_queue():
    """测试优先级队列"""
    print("\n" + "=" * 60)
    print("测试2: 优先级队列")
    print("=" * 60)

    gateway = get_api_gateway()

    # 先发送3个低优先级请求占满窗口
    for i in range(3):
        gateway.acquire(priority=RequestPriority.LOW, endpoint=f"low_{i}")

    print("已发送3个低优先级请求，窗口已满")

    # 发送1个高优先级请求
    start = time.time()
    gateway.acquire(priority=RequestPriority.CRITICAL, endpoint="critical_flag_submit")
    elapsed = time.time() - start

    print(f"高优先级请求等待: {elapsed:.3f}s")

    gateway.print_stats()


def test_concurrent_requests():
    """测试并发请求"""
    print("\n" + "=" * 60)
    print("测试3: 并发请求 (10个线程同时请求)")
    print("=" * 60)

    gateway = get_api_gateway()
    results = []

    def make_request(thread_id):
        start = time.time()
        wait = gateway.acquire(
            priority=RequestPriority.NORMAL,
            endpoint=f"concurrent_{thread_id}"
        )
        elapsed = time.time() - start
        results.append((thread_id, wait, elapsed))

    threads = []
    start_time = time.time()

    for i in range(10):
        t = threading.Thread(target=make_request, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    total_time = time.time() - start_time

    print(f"\n10个并发请求完成:")
    for thread_id, wait, elapsed in sorted(results, key=lambda x: x[2]):
        print(f"  线程 {thread_id}: 等待 {wait:.3f}s, 总耗时 {elapsed:.3f}s")

    print(f"\n总耗时: {total_time:.3f}s")
    print(f"预期耗时: ~3.0s (10次请求 / 3次每秒 ≈ 3.33秒)")

    gateway.print_stats()


def test_429_backoff():
    """测试429退避机制"""
    print("\n" + "=" * 60)
    print("测试4: 429退避机制")
    print("=" * 60)

    gateway = get_api_gateway()

    # 模拟触发429
    print("模拟触发429错误...")
    gateway.report_429(retry_after=2.0, endpoint="test_429")

    # 尝试发送请求
    start = time.time()
    gateway.acquire(priority=RequestPriority.NORMAL, endpoint="after_429")
    elapsed = time.time() - start

    print(f"429后的请求等待: {elapsed:.3f}s")
    print(f"预期等待: ~2.0s (退避时间)")

    gateway.print_stats()


def test_throughput():
    """测试吞吐量"""
    print("\n" + "=" * 60)
    print("测试5: 吞吐量测试 (30秒内最大请求数)")
    print("=" * 60)

    gateway = get_api_gateway()
    start = time.time()
    count = 0
    duration = 5  # 测试5秒

    while time.time() - start < duration:
        gateway.acquire(priority=RequestPriority.NORMAL, endpoint=f"throughput_{count}")
        count += 1

    elapsed = time.time() - start
    rate = count / elapsed

    print(f"\n{duration}秒内完成 {count} 次请求")
    print(f"实际速率: {rate:.2f} req/s")
    print(f"理论速率: 3.00 req/s")
    print(f"效率: {(rate/3.0)*100:.1f}%")

    gateway.print_stats()


if __name__ == "__main__":
    print("API网关测试套件")
    print("=" * 60)

    try:
        test_basic_rate_limit()
        time.sleep(1.5)  # 清空窗口

        test_priority_queue()
        time.sleep(1.5)

        test_concurrent_requests()
        time.sleep(1.5)

        test_429_backoff()
        time.sleep(2.5)

        test_throughput()

        print("\n" + "=" * 60)
        print("所有测试完成!")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n测试被中断")
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()

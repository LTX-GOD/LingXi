"""
API网关监控工具
===============
实时监控API网关的运行状态和统计信息
"""

import sys
import os
import time
import threading
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.api_gateway import get_api_gateway


class APIGatewayMonitor:
    """API网关监控器"""

    def __init__(self, interval: float = 10.0):
        self.interval = interval
        self.gateway = get_api_gateway()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_stats = None

    def start(self):
        """启动监控"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print(f"[APIGatewayMonitor] 监控已启动 (间隔: {self.interval}s)")

    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        print("[APIGatewayMonitor] 监控已停止")

    def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                self._print_stats()
                time.sleep(self.interval)
            except Exception as e:
                print(f"[APIGatewayMonitor] 监控错误: {e}")

    def _print_stats(self):
        """打印统计信息"""
        stats = self.gateway.get_stats()
        now = datetime.now().strftime("%H:%M:%S")

        # 计算增量
        if self._last_stats:
            delta_requests = stats["total_requests"] - self._last_stats["total_requests"]
            delta_waits = stats["total_waits"] - self._last_stats["total_waits"]
            rate = delta_requests / self.interval if self.interval > 0 else 0
        else:
            delta_requests = 0
            delta_waits = 0
            rate = 0

        self._last_stats = stats.copy()

        # 格式化输出
        print(f"\n[{now}] API网关状态:")
        print(f"  总请求: {stats['total_requests']} (+{delta_requests})")
        print(f"  等待次数: {stats['total_waits']} (+{delta_waits})")
        print(f"  平均等待: {stats['avg_wait_time']:.3f}s")
        print(f"  当前窗口: {stats['current_window_count']}/{stats['max_requests']}")
        print(f"  实时速率: {rate:.2f} req/s")
        print(f"  退避次数: {stats['backoff_count']}")

        if stats["is_backing_off"]:
            print("  ⚠️  当前处于退避状态")


def start_background_monitor(interval: float = 30.0):
    """启动后台监控（用于主程序集成）"""
    monitor = APIGatewayMonitor(interval=interval)
    monitor.start()
    return monitor


if __name__ == "__main__":
    print("API网关实时监控")
    print("按 Ctrl+C 停止监控")
    print("=" * 60)

    monitor = APIGatewayMonitor(interval=5.0)
    monitor.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止监控...")
        monitor.stop()

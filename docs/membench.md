# hvisor region 创建、删除基准

在**宿主机**的仓库根目录运行，复用 `make test` 的 QEMU 环境，无需 Linux/rootfs：

```sh
bash tools/bench_region.sh
# 或
make membench
REGIONS=64 ROUNDS=10 bash tools/bench_region.sh
```

脚本头部可修改 `REGIONS=1024`（region 数量）、`REGION_PAGES=1`（每个 region 的 4 KiB 页数）和 `ROUNDS=100`（正式轮数），也可通过环境变量覆盖。

每轮创建独立测试 zone，在 hvisor 内部批量调用 `VMemorySet::insert`，然后批量调用 `VMemorySet::delete`，分别累计插入和删除耗时。先执行一次预热，不计入结果；zone 创建、回收和输出均在计时区间外。

结果保存在 `target/bench_region.txt`，编译及启动日志保存在 `target/bench_region.log`；重复运行会覆盖这两个文件。结果显示批量操作总耗时及每次操作的平均耗时，不统计内存占用。

仅支持 `aarch64/qemu-gicv3`，使用 release 构建。脚本自动执行 `defconfig`，**当前 `.config` 会被默认配置替换**。

当前没有直接操作 region 的 hypercall，因此脚本负责启动测试，实际插入、删除仍需一小段 hvisor 内部 Rust 代码；不能在 Guest 中直接运行该脚本。

比较前后版本时保持参数、工具链和宿主负载一致。QEMU 计时用于同环境比较，不能代表真机性能。

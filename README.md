# CXL 尾延迟实验报告与数据

双路 Intel Xeon 6515P / Granite Rapids，Ubuntu 22.04.5，Linux 6.8。实验日期为 2026-11-09 至 2026-11-11。

- [完整报告](experiments/cxl-tail-20261109/FINAL_REPORT_20261111.md)：81 次有效运行，PEBS/PCM 能力、跨路访问、竞争强度、调度与链路配置分析。
- [最初 54 组实验报告](experiments/cxl-tail-20261109/REPORT.md)。
- [原始矩阵汇总](experiments/cxl-tail-20261109/results-table.md)。
- [新增 24 组汇总](experiments/cxl-tail-20261109/expansion-20261111/group-summary.csv)。
- [sudo 联合采集分析](experiments/cxl-tail-20261109/followup/analysis.json)。

## 数据和校验

完整实验输出、原始 perf 数据、PCM、调度/IRQ、频率、页放置快照、试运行及复现脚本均在 `experiments/cxl-tail-20261109/`。6 个大型 scheduler 文件以无损 gzip 保存，其余实验文件保持原样。不需要 Git LFS。

```bash
python3 scripts/verify_data.py
```

校验覆盖原始实验目录的 936 个文件，包括解压后内容，依据 `source-manifest.json` 的 SHA-256 与字节数。报告中的本机绝对路径保留为采集溯源；浏览本仓库时使用上述相对链接。

重新分析联合采集前，先恢复原文件：

```bash
python3 scripts/verify_data.py --restore
python3 experiments/cxl-tail-20261109/analyze_followup.py
```

恢复后的原始大文件被 Git 忽略；gzip 文件保留。分析需要 Python 3 和 Linux perf，无需重新运行负载。`analyze_followup.py` 还会读取当前主机的 sysfs，所以异机重建时硬件快照可能显示不可用；已提交的 `analysis.json` 保留采集主机的分析结果。

## 复现实验

采集需要本机拓扑对应的 CPU/NUMA 节点、perf、numactl、Intel PCM 和 MLC 3.13。Intel 工具本体未打包；脚本中的 MLC 路径为采集机路径，移机使用前需要调整。执行实验会消耗 CPU/内存带宽；分析现有数据无需运行实验。

`run_experiment.py`、`run_expansion.py` 为普通用户实验入口，已成功的运行会跳过。`capture_privileged.sh` 为用户执行的 sudo 联合采集；`capture_link_state.sh` 为只读链路/AER/健康状态采集。硬件配置、限制和用户待办以完整报告为准。

`benchmarks/` 保存本机已有的 `memtraffic.c`、`lat.c`、`cxlpmu.c` 辅助源码；正式直方图由 MLC 生成，PEBS 探针源码和采集时二进制保存在实验目录。未附加新的许可条款。

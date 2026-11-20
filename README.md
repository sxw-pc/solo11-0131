# 谱图标定采录台

本地 CLI 从色谱桌面软件导出的 JSON 文件采集原始字节和 SHA-256 摘要，再在 SQLite 中保存标定、样本峰、质控、双人峰位裁定、样本交接、结果和批次放行记录。所有样例为虚构数据，无网络调用。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m spectracapture demo
python -m spectracapture --help
python -m pytest -q
```

每个业务入口接受 `--db ledger.db export.json`：`calibration-import`、`sample-assign`、`qc-assess`、`peak-review`、`custody-assess`、`result-release`、`batch-release`。`report --db ledger.db --out report.json` 输出来源完整性、实体、问题和决策。导出格式是 `{"version":"v1","rows":[...]}`；可从 `tests/test_workflows.py` 看到完整成功与失败输入。

## 独立业务结论

| 子系统 | 持久化实体 | 裁定与跨系统影响 |
| --- | --- | --- |
| 标定与质控 | `calibration_export`, `calibration_point`, `qc_batch`, `qc_control` | 浓度梯度、空白、拟合残差、有效期和控制样回收率决定曲线是否能支持定量；失控曲线或质控阻止结果放行。 |
| 样本峰归属与复核 | `sample_export`, `peak_assignment`, `peak_review`, `integration_revision` | 同号进样、保留时间窗及邻峰歧义产生不同待核原因；双分析员仅能从窗口内峰中裁定，修订后的峰面积参与浓度计算。 |
| 样本交接 | `custody_case`, `custody_transfer`, `release_recall` | 持有人连续性、封签、温控和目标实验室决定样本是否合格；迟到的隔离证据会撤回已放行结果与已封账批次，并生成待通知的召回记录。 |
| 结果与批次 | `result_batch`, `released_result`, `release_manifest`, `release_item` | 将曲线、质控、峰与交接证据合并为可放行、待核或拒放；批次区分全部与部分可发布，撤回后重放原导出不会恢复封账。 |

同一来源的重放基于文件摘要保持幂等；同一实体 ID 变更内容将阻止覆盖。`export_file` 保留原始字节，`export_record` 保留逐行身份与内容。报告重新核对每份原始文件的摘要。测试覆盖正常、冲突、部分失败、迟到撤回和重放。

## 离线证据交换与合并

两个断网实验室各自维护一份 `ledger.db`。交换包只包含已提交的原文件、该副本的提交链、这些提交产生的证据节点/链接/裁定，以及解决链接所需的因果前驱；本地整数 ID 从不出库。

```powershell
python -m spectracapture exchange-export --db lab-a.db --out a.json [--since N]
python -m spectracapture exchange-import --db lab-b.db a.json
python -m spectracapture exchange-status --db lab-b.db
python -m spectracapture evidence-graph --db lab-b.db --out graph.json
python -m spectracapture review-resolve --db lab-b.db review.json
python -m spectracapture report --db lab-b.db --out report.json
```

- **稳定身份**：原文件用 `sha256(原始字节)`；提交号用 `sha256(副本, 序号, 上一提交, 文件摘要)`；证据节点用 `sha256(族, 业务键, 内容摘要, 文件摘要, 版本, 状态, 原因)`。相同字节在两库收敛为同一原文件并保留两条到达路径；同一业务键的不同原字节产生两条并发分支，互不覆盖。
- **接收顺序**：先整包校验（字节摘要、包内引用、提交链、证据身份），再单事务写入。坏包整体拒绝，不留半个可见提交，`merge_log` 记录拒因与恢复游标。子提交先于父提交到达时挂起为 `deferred`，父包到达后自动续传；重复包返回 `duplicate`；写入中途崩溃后重放同一包与正常合并得到相同证据图。
- **档案读法** `archive`：所有来源、并发分支、引用/撤回关系、未解冲突，不隐藏任何分支。
- **业务读法** `business`：只允许因果闭合、未被撤回、且无并发分支或并发复核的证据形成可发布视图；未解决时列入 `pending`，撤回产生 `已撤回` 版本与 `notifications`。提交序号从不用于打破平局。
- **复核裁定**：只有带签名、且引用全部存活分支和全部撤回证据的 `review_resolution` 才能解除冲突；旧版本、旧放行与旧撤回仍保留在档案中。

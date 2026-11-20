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
| 原文件保全与处置 | `archive_object`, `retention_ref`, `preservation_order`, `disposal_request`, `destruction_credential`, `disposal_state`, `repair_request`, `object_key` | 原字节先写临时对象并验摘要、原子公布后才切换库引用；保全令、独立保留期限、未结案复核和损坏修复请求分别阻止销毁；完成销毁按摘要签发唯一凭证，每个处置状态带前序游标可重放。 |

同一来源的重放基于文件摘要保持幂等；同一实体 ID 变更内容将阻止覆盖。`export_file` 保留原始字节或冷存储引用，`export_record` 保留逐行身份与内容。报告从受控位置实际取回字节重新核对摘要，冷存储对象损坏会立即留下修复请求并阻止销毁。测试覆盖正常、冲突、部分失败、迟到撤回、重放、迁移断点恢复、保全与清理交错以及篡改检测。

## 原文件保全与到期处置

`archive-migrate --db ledger.db --store cold` 把库内原字节迁入按摘要寻址的冷存储对象；`retention-attach` 登记独立保留期限，`hold-submit`/`hold-release` 受理与解除保全令，`destroy-request` 提交销毁申请，`cleanup` 在两个明确提交边界内重估合同、处置钥匙并删除物理对象，`archive-assess` 输出每份原文件的可恢复性与最终处置裁定，`key-register` 登记解密钥匙及其处置策略。档案合同只在字节能实际取回且摘要一致时签“可恢复”；销毁合同只在保全令全部解除、所有独立引用期限届满、无未结案复核且对象与钥匙均按策略处置后签“完成”。销毁完成后迟到的保全令记录为“无法执行”并附此前凭证，不会把文件标回可恢复。

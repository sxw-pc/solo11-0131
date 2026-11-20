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

## 仪器来源验真

字节 SHA-256 只能证明入库后未被篡改，不能证明文件确由指定仪器产生。预装信任根后的采集库进入严格模式：每份导出在 `source_proof` 中携带仪器对**规范化载荷摘要、仪器身份、单调采集序号、上一份采集摘要**的 Ed25519 签名。规范化 JSON 仅用于验签与去重，原始字节仍按原方式逐字保存；仅 JSON 空格或字段顺序不同、规范化载荷相同的重放返回同一采集，不产生第二次入库。

- **待补链**：断网时后继文件可能先到。前驱缺失记为 `待补链` 而非判伪，前驱到达后沿链重算并转为可验。
- **密钥轮换**：轮换声明必须同时由旧密钥和信任根签名（`rotation-import`），无效轮换留拒绝记录且不登记新密钥；链中不得跳序或借用其他仪器的序号。
- **设备分叉**：同一仪器同一序号出现两个不同的有效签名载荷时两份都留证，档案仍可归档，其后继的业务使用全部冻结，不按提交时钟二选一。
- **撤销通报**：`revocation-import` 只接受信任根签名、给出设备序号区间和签发来源的通报；是否落在区间内只看采集序号，不读取导出文件自报的墙上时间。迟到通报沿前驱关系重算裁定，旧裁定保留在 `source_verdict_history`，已发出的使用记录不被改写。
- **两份合同分别裁定**：档案合同（`source_archive`）只要求字节、签名与前驱可核验，分叉或撤销后仍可作为争议证据；业务合同（`source_business`）额外要求无分叉且不在撤销区间，才允许该来源支持新结果。两者不压缩成一个 `verified` 布尔值。业务闸门只改写新结果的放行裁定，峰面积、浓度和已签分析员判断一律不重算、不覆盖。

相关 CLI：`trust-import` 预装仪器信任根，`verify-source` 输出单份导出的双合同裁定，`rotation-import` 与 `revocation-import` 录入轮换与撤销。未安装任何信任根的库保持原有 legacy 采集行为。完整密码学场景见 `tests/test_source_trust.py`。


## 独立业务结论

| 子系统 | 持久化实体 | 裁定与跨系统影响 |
| --- | --- | --- |
| 标定与质控 | `calibration_export`, `calibration_point`, `qc_batch`, `qc_control` | 浓度梯度、空白、拟合残差、有效期和控制样回收率决定曲线是否能支持定量；失控曲线或质控阻止结果放行。 |
| 样本峰归属与复核 | `sample_export`, `peak_assignment`, `peak_review`, `integration_revision` | 同号进样、保留时间窗及邻峰歧义产生不同待核原因；双分析员仅能从窗口内峰中裁定，修订后的峰面积参与浓度计算。 |
| 样本交接 | `custody_case`, `custody_transfer`, `release_recall` | 持有人连续性、封签、温控和目标实验室决定样本是否合格；迟到的隔离证据会撤回已放行结果与已封账批次，并生成待通知的召回记录。 |
| 结果与批次 | `result_batch`, `released_result`, `release_manifest`, `release_item` | 将曲线、质控、峰与交接证据合并为可放行、待核或拒放；批次区分全部与部分可发布，撤回后重放原导出不会恢复封账。 |

同一来源的重放基于文件摘要保持幂等；同一实体 ID 变更内容将阻止覆盖。`export_file` 保留原始字节，`export_record` 保留逐行身份与内容。报告重新核对每份原始文件的摘要。测试覆盖正常、冲突、部分失败、迟到撤回和重放。

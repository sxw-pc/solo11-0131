# 谱图标定采录台

本地 CLI 从色谱桌面软件导出的 JSON 文件采集原始字节和 SHA-256 摘要，再在 SQLite 中保存标定、样本峰、质控、双人峰位裁定、样本交接、结果和批次放行记录。所有样例为虚构数据，无网络调用。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m spectracapture demo
python -m spectracapture --help
python -m pytest -q
```

每个业务入口接受 `--db ledger.db export.json`：`calibration-import`、`sample-assign`、`qc-assess`、`peak-review`、`custody-assess`、`result-release`、`batch-release`、`mixed-import`、`contract-register`、`channel-qc`、`spike-signoff`、`standard-correct`、`mixed-quant`。`report --db ledger.db --out report.json` 输出来源完整性、实体、问题和决策。导出格式是 `{"version":"v1","rows":[...]}`；可从 `tests/test_workflows.py` 与 `tests/test_coelution.py` 看到完整成功与失败输入。

## 独立业务结论

| 子系统 | 持久化实体 | 裁定与跨系统影响 |
| --- | --- | --- |
| 标定与质控 | `calibration_export`, `calibration_point`, `qc_batch`, `qc_control` | 浓度梯度、空白、拟合残差、有效期和控制样回收率决定曲线是否能支持定量；失控曲线或质控阻止结果放行。 |
| 样本峰归属与复核 | `sample_export`, `peak_assignment`, `peak_review`, `integration_revision` | 同号进样、保留时间窗及邻峰歧义产生不同待核原因；双分析员仅能从窗口内峰中裁定，修订后的峰面积参与浓度计算。 |
| 样本交接 | `custody_case`, `custody_transfer`, `release_recall` | 持有人连续性、封签、温控和目标实验室决定样本是否合格；迟到的隔离证据会撤回已放行结果与已封账批次，并生成待通知的召回记录。 |
| 结果与批次 | `result_batch`, `released_result`, `release_manifest`, `release_item` | 将曲线、质控、峰与交接证据合并为可放行、待核或拒放；批次区分全部与部分可发布，撤回后重放原导出不会恢复封账。 |
| 共流出混合峰 | `mixed_peak_export`, `mixed_channel`, `assay_contract`, `channel_qc`, `spike_signoff`, `standard_correction`, `mixed_quant`, `mixed_adjudication` | 单峰多通道面积按“空白加各物质响应贡献”联立裁定，只接受非负、落在标定范围内且满足获准通道误差界的浓度组合，逐物质以可行浓度界给出可放行（唯一或有界）、待核、低于定量限或拒放。常规合同只用已登记主通道，第三通道仅作旁证；加标确证合同在独立标准、回收率质控合格且两名不同分析员签认（绑定混合峰原始摘要与通道标准版次）齐备时才纳入第三通道。迟到的通道失控、标准版次更正或签认撤销会重裁相关结果、把已放行结果打回待核并撤回已封账批次，无关进样保持不变。 |

同一来源的重放基于文件摘要保持幂等；同一实体 ID 变更内容将阻止覆盖。`export_file` 保留原始字节，`export_record` 保留逐行身份与内容。报告重新核对每份原始文件的摘要。测试覆盖正常、冲突、部分失败、迟到撤回和重放。

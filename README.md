# 谱图标定采录台

本地 CLI 从色谱桌面软件导出的 JSON 文件采集原始字节和 SHA-256 摘要，再在 SQLite 中保存标定、样本峰、质控、双人峰位裁定、样本交接、结果和批次放行记录。所有样例为虚构数据，无网络调用。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m spectracapture demo
python -m spectracapture --help
python -m pytest -q
```

每个业务入口接受 `--db ledger.db export.json`：`calibration-import`、`sample-assign`、`qc-assess`、`peak-review`、`custody-assess`、`result-release`、`batch-release`、`waste-assess`、`waste-rules`、`waste-events`。`report --db ledger.db --out report.json` 输出来源完整性、实体、问题和决策。导出格式是 `{"version":"v1","rows":[...]}`；可从 `tests/test_workflows.py` 看到完整成功与失败输入。

## 独立业务结论

| 子系统 | 持久化实体 | 裁定与跨系统影响 |
| --- | --- | --- |
| 标定与质控 | `calibration_export`, `calibration_point`, `qc_batch`, `qc_control` | 浓度梯度、空白、拟合残差、有效期和控制样回收率决定曲线是否能支持定量；失控曲线或质控阻止结果放行。 |
| 样本峰归属与复核 | `sample_export`, `peak_assignment`, `peak_review`, `integration_revision` | 同号进样、保留时间窗及邻峰歧义产生不同待核原因；双分析员仅能从窗口内峰中裁定，修订后的峰面积参与浓度计算。 |
| 样本交接 | `custody_case`, `custody_transfer`, `release_recall` | 持有人连续性、封签、温控和目标实验室决定样本是否合格；迟到的隔离证据会撤回已放行结果与已封账批次，并生成待通知的召回记录。 |
| 结果与批次 | `result_batch`, `released_result`, `release_manifest`, `release_item` | 将曲线、质控、峰与交接证据合并为可放行、待核或拒放；批次区分全部与部分可发布，撤回后重放原导出不会恢复封账。 |
| 废液处置 | `waste_container`, `waste_rule`, `waste_event`, `waste_disposition`, `waste_transport_order`, `waste_incident` | 倒入、搅拌、取样、中和、封口是不可倒写的实物事件，只按已签顺序与设备顺序证明排序，不按到达时间。交运合同要求所有符合已签顺序、剂量区间与容量的可行过程从开桶到封口未越放热、产气、材质限制，且当前状态合格，才签可交运；整改合同独立判断此刻能否安全隔离并给出中和路线。越限形成不可抹除的事故证据，晚到的顺序证明生成新处置版并撤销已发出的交运单；记录矛盾时报告证据冲突与最小冲突记录。 |

同一来源的重放基于文件摘要保持幂等；同一实体 ID 变更内容将阻止覆盖。`export_file` 保留原始字节，`export_record` 保留逐行身份与内容。报告重新核对每份原始文件的摘要。测试覆盖正常、冲突、部分失败、迟到撤回和重放。

## 废液最小样例

`samples/waste/` 给出一个 5 升容器：初始 1 升酸性清洗液，另有中和剂与氧化性废液各 1 升。本地规则（`rules.json`）登记：中和剂先消耗活性酸则氧化性废液不越限；氧化性废液先遇活性酸则中途产气越限。依次运行：

```powershell
python -m spectracapture waste-rules --db ledger.db samples/waste/rules.json
python -m spectracapture waste-assess --db ledger.db samples/waste/container.json
python -m spectracapture waste-events --db ledger.db samples/waste/events_unordered.json
python -m spectracapture waste-events --db ledger.db samples/waste/proof_neutralizer_first.json
python -m spectracapture report --db ledger.db --out report.json
```

倒入记录缺少可靠先后时交运待核、整改可签；补到 `proof_neutralizer_first.json`（中和剂在先）后重裁为可交运；若改投 `proof_oxidizer_first.json`（氧化性废液在先），则留下事故记录、签不可交运并撤回已发出的交运单，整改合同仍给出隔离去向。

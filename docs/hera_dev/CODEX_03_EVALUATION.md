# Codex P3：可复现的多策略、多LLM研究评测

在现有MyAgent科学模式上继续。先读共用约束、开发计划、验收矩阵和P1/P2实测记录。不要重新实现环境，不调用付费API，除非用户另外明确授权。

## 一、实验轴必须分开

- environment/data：事件与固定版本交互环境；
- policy：固定、规则、generic_react、hypothesis、enumerate_same_value；
- LLM：mock、deepseek_dev、second_compatible、其他明确profile；
- perception/risk/value：全部冻结，同对照共享。

rule或enumerate策略没有LLM时写model=not_used，不伪造调用。不要把两个Mock profile称为真实大模型比较。

## 二、策略实现

1. fixed：预注册固定计划；仍遵守相同权限/前置条件/预算。
2. rule：透明规则选择实验，必须拥有与LLM相同参数域。
3. generic_react：相同可见观测和工具说明，直接选择允许实验；不加本文假设预承诺模板作为隐藏优势。
4. hypothesis：完整假设—实验—证据—修订流程。
5. enumerate_same_value：枚举相同有限域中的所有合法候选，使用和hypothesis完全相同的价值器；记录枚举开销。
6. non_llm_learned：预留接口；只有已训练权重和训练记录时加入运行。否则明确not_fitted/skipped，不拿随机网络冒充学习基线。

主公平比较共享来源融合/风险模型；去掉来源或风险模块的对照单独标为消融，不暗中改变其他因素。

## 三、批量运行接口

新增命令，例如：

```bash
python -m eo_agent benchmark --config configs/benchmarks/synthetic_smoke.yaml --output-dir outputs/benchmark
```

配置描述episode split、policy×model矩阵、repeats、seed、预算、provider网络许可、缓存协议和失败处理。不得一运行就创建昂贵的全矩阵API请求。

先提供纯离线smoke config：mock LLM、rule、fixed、enumerate等，少量episode。真实模型配置模板disabled/network_not_authorized，用户手动启用。

## 四、环境公平与标签隔离

每个(policy,model,episode,repeat)创建独立运行状态。所有策略面对同一初始观测、可获取数据、参数域、同动作同响应、成本表与截止。

实验环境可有私有观测世界，但评分标签单独位于evaluation端，不进入Policy/LLM/Risk/Value接口。校验trace、HTTP payload、artifact命名和context都不带隐藏原因或正确动作。

同原始事件的不同云掩膜、裁剪、增强和共享测量分组到同一split；使用group_id做训练/校准/测试隔离。不要让一个模型先访问答案后其记忆进入后续模型。

技术失败、无数据、不适用、拒判、未调查都保留。禁止只导出成功任务。

## 五、指标与空值

至少输出：event labels和decision、实验执行合法率、预承诺合规率、duplicate_invariance、missing_as_negative错误数、未来信息违规、纠错率、反向损害率、LLM/工具调用、实际延迟、各类成本、拒判率。

模拟数据上可计算对模拟真值的诊断准确率/F1，但必须data_regime=synthetic，不能写成真实遥感性能。没有calibrated probabilities时Brier/ECE为null并说明原因，不填0。

FDP分母0时为NA；拒判同时报告覆盖率；零分母统一规范。区域指标尚未具备完整候选/真值时为not_supported，不拿给定候选F1冒充区域性能。

Token缺失、费用未知为null。失败响应既无usage也无账单时不能宣称免费。冷/热缓存分开，工具次数不直接等同GPU成本。

## 六、生成可直接用于论文检查的产物

- runs.jsonl：逐episode逐repeat原始结果与配置；
- metrics.csv：从raw结果自动计算；
- failures.jsonl：失败与不可判定原因；
- manifest.json：代码、依赖、模型、prompt、数据和策略hash；
- tables/main_comparison.md；
- tables/ablation.md；
- tables/llm_comparison.md；
- tables/provenance_stress.md；
- tables/cost_risk.md；
- tables/real_data_pending.md：没有真实实验时仅留空；
- report.html：标注模拟结果、有限样本和未验证项。

表格只能由运行结果计算，不能从提示词补数值。提供模式strict_publication：发现mock/unfitted/invalid校准时阻止填入真实结果表，仍允许生成带警示的开发表。

统计先支持事件级重复和配对差异；区域分组CI在真实区域存在后再实现，不能把同事件多个增强当独立样本。

## 七、必须增加的评测测试

- 重命名episode与重排文件顺序不改变环境结果；
- 两个策略选择同动作时收到同测量；
- enum与LLM候选使用同value adapter和参数域；
- 不同模型独立状态/缓存可见权限；
- train/calibration/test group无交叉；
- 标签sentinel不会进入模型请求；
- null与零分母正确处理；
- raw→metrics→tables一致；
- 失败不被删除；
- 未授权真实provider不发请求；
- mock指标无法进入publication真实表。

## 八、交付

运行离线benchmark、全量pytest与Ruff。报告所有实测、未执行model profile、未训练基线、模拟器局限。更新README与hera_progress。最终不预设hypothesis策略赢得比较；任何排序只来自实际结果且注明适用环境。

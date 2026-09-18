# Codex P2：真实多模型适配、七类实验与调用可观测性

先读各级AGENTS、共用约束、开发计划、P1实际代码和hera_progress。直接实现本阶段，不回退或另建项目。若P1科学闭环尚未通过，先报告并修复前置问题，不只增加新目录。

## 一、本轮目标

在同一科学工作流中让DeepSeek和其他provider实际参与假设、实验参数提案、结果反思与claim组织。默认仍离线，自动测试用MockTransport。不引入新的遥感训练模型。

## 二、LLM协议与provider

保留现有LLMResult/LLMMetadata、factory和profiles；正式提供泛型generate_structured(messages, output_model, call_context)。旧parse_task/choose_action/summarize使用同一路径。

profile与policy分开配置。新增科学role的prompt文件：hypothesis、experiment_proposal、reflection、claim；同角色不同provider共享语义输入和schema，不为某个模型写特殊答案提示。

开发profile优先deepseek_dev，第二兼容服务沿用second_compatible；不硬编码模型ID，以环境变量配置。其他原生协议仅在实际需要时新增adapter，不声称所有API天然兼容。

实施时核对官方文档：DeepSeek JSON Output与工具调用是不同能力。json_object只保证JSON，不替代Pydantic和领域验证。处理空content、截断、字段错、未知动作、超长文本。

结构化输出字段修复有上限。不能用正则随意删掉失败内容伪造成功。模型给出的实验参数必须穿过实际domain validator；错误实验无法执行。

## 三、逐次尝试账本

在每次HTTP前预占预算，try/finally记录attempt。每个attempt至少含：run/task/event/role/call_id/attempt_id、requested_model、returned_model、profile、prompt/schema hash、生效参数、开始/结束时间、状态码或错误类别、修复/重试原因、已知usage、费用状态。

仅最终响应usage不能代表全部尝试用量；未返回usage的失败记unknown，不记零。分别记录logical_call和transport_attempt，避免双重计数。

401/403不盲目重试；429、超时、5xx采用有限且可测试的策略；退避等待可注入，单元测试不真实sleep。无预算时不再发下一次请求。真实模型失败不换供应商、不切回Mock。

不要记录Authorization、环境变量值、私密推理内容或未经脱敏的错误响应。关键结构输出可按配置存储，但必须通过敏感字段过滤；论文分析只需要动作理由与执行轨迹。

## 四、实验库补齐

在P1的E1—E3基础上实现E4—E7的真实可执行模拟：

- E4 SAR诊断：轨道组/入射角可比性先检查，输出轨道内响应与时间证据；不把SAR稳定一律解释为无变化。
- E5替代恢复：变体与参考窗口固定参数域，返回两结果及根源重叠；生成种子不同不等于新原始观测。
- E6配准敏感性：使用已登记的误差profile限定扰动；输出稳定性，而不是固定PASS。
- E7云影敏感性：固定质量profile比较，输出稳定核心与敏感范围；不允许搜索阈值直到期望结论。

保证window/source/subregion/profile确实改变模拟测量或适用状态。LLM只能提案，不能更改注册的判读规则。

完善复合干扰世界和不适用、失败、长期不足世界；真实变化和局部伪影可以共存。支持/冲突绑定命题，不用一列全局passed代替。

## 五、科学API与报告

新增查询事件、实验承诺、来源关系与风险历史的接口，仍复用原TaskService/数据库。不要为此开发复杂前端；最小HTML/JSON已足够审查。

报告区分原始事实、模型推断、LLM解释、数值风险、剩余未知。未经校准时不显示概率百分数；技术完成不意味着科学validated。

报告文案失败时允许固定模板展示既有事实，同时记录degraded_report，不静默使用另一LLM。注意这不是“模型fallback”。

## 六、测试

1. 用两个配置及两套MockTransport响应证明同一科学graph能运行，不修改业务代码。
2. 真实模型路径也必须执行propose_hypotheses/propose_experiments/reflect，不只是解析和写报告。
3. 所有LLM输入通过白名单；给隐藏标签设置sentinel，拦截实际HTTP payload检查不泄漏。
4. 测试空响应、截断、畸形JSON、合法JSON但越界参数、401/403、429、超时、5xx、预算在重试期间耗尽。
5. 验证prompt/model/schema版本、每次attempt、未知usage被记录；秘密不出现在任何产物。
6. 每个E1—E7均有成功、信息不足/不适用、错误参数和同源处理测试。
7. 与旧API/CLI全量回归一起运行。

用户没有提供授权密钥时，真实DeepSeek和第二模型调用标记为未验证，不自动执行。可以在README提供显式运行方式：

```bash
python -m eo_agent demo --workflow scientific --episode EP001 --model deepseek_dev --output-dir outputs/deepseek
```

实际provider调用还应受项目明确网络授权配置保护；不要给旧profile新增意外的隐性远程副作用。默认mock保持完全离线。

## 七、退出条件与回复

准确说明兼容协议测试与真实API验证的区别。交付可运行代码、全部测试结果、profile配置说明和E1—E7支持矩阵。没有真实模型调用就不能写“已验证DeepSeek科学能力”。停止，不自动启动批量付费评测。

## 官方参考入口

- https://api-docs.deepseek.com/guides/json_mode/
- https://api-docs.deepseek.com/guides/tool_calls/
- https://developers.openai.com/codex/guides/agents-md/

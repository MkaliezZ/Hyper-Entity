# Hyper Entity

**v0.2-draft · 立项前验证**。Hyper Entity 让下游核查者沿一个对象的声明查回原始依据，判断它在哪些用途下成立，并按当前授权使用字段。[通用协议](<Hyper Entity 通用协议 v0.2-draft.md>)保留个人、产品等对象的共同结构。[首轮验证方案](首轮验证方案.md)选择样品检测数据，比较 Entity 和现有报告接口在同一任务上的收益与代价。

## 运行样例

从仓库根目录执行。参考接收端需要 Python 3.11 或更高版本。先在你的 Python 环境中安装 [requirements.txt](requirements.txt) 中的依赖，之后的样例和测试可离线运行。

```powershell
python -m pip install -r requirements.txt
python -B tools/entity.py check examples/sample-bundle.json
python -B tools/entity.py decide examples/sample-bundle.json --host examples/host-state.json --request examples/read-request.json
python -B -m unittest discover -s tests -v
```

第一条检查上位任务包、Entity 扩展、引用和资产内容哈希。第二条核对样品 `sample-07` 和原始记录 `reading-07`，用宿主的当前状态决定这次读取。给定夹具的输出为：

```json
{"judgment":"meets_conditions","authorization":"granted","execution":"read","reason":"verified_sample_record","view":{"value":{"number":1.04,"unit":"g/cm³"}}}
```

CLI 成功处理业务请求时退出码为 0，包括被拒绝和待补证的请求。调用方应读取 `execution` 与 `view`。输入违反契约时退出码为 1，文件读取或 JSON 解析失败时为 2。

## 核查哪些行为

| 场景 | 预期 |
| --- | --- |
| 把声明替换成空对象，或提供悬空的证据版本 | 拒收不完整的包，给出契约错误及位置。 |
| 以样品实测值主张整批结果 | 缺取样依据，返回待补证及空视图。 |
| 证据仍然充分，但读取授权已撤销，或只有读取权却请求披露 | 当前动作被阻止，输出不含受限字段。 |
| 当前登记表不可用，或出现相互竞争的授权版本 | 权限保留未知，暂停这次动作。 |
| 个人自述的课程角色 | 通过通用实体与声明的字段约束，专业核验仍需相应领域规则。 |

[冻结用例](examples/frozen-cases.json)共 25 条；测试套件还单独核对证据冲突、授权状态变化和跨域字段。用例及预期答案均为开发回归材料。真人对照的留出题须另行冻结。

## 两条读回路径

- **Entity 样包。** 有 Node.js 的环境可运行 `node interop/check-receiver.mjs`。Node 端独立解析交换包，按版本找回实体、声明及原始行，核对内容哈希、测量条件和值，并检查 Python 参考接收端对五种请求条件的输出。现时授权的执行由 Python 接收端承担。
- **Hyper Report 基线（可选）。** 这项对照需要另行取得 Hyper Report 的应用源码和测试夹具。使用该项目的 Python 环境，运行下面的脚本，将 `--report-root` 指向其源码根目录。脚本用同一条合成测量生成 CSV，在临时库中通过报告接口读回数值和来源，再比较两种角色的读取权限。两边的来源标识分别属于各自的系统。

```powershell
python -B -X utf8 interop/check_report_baseline.py --report-root "../Hyper-Report"
```

所有样包、授权簿与原始值均为合成数据。记录的本地重放可证明受测条件下的字段和权限行为。真人复核时间、接入成本、真实来源权利和采用意愿按[试验方案的通过线与停止线](首轮验证方案.md)另行回收。上位 Schema 与校验器的固定版本及许可见[来源声明](tools/upstream/NOTICE.txt)。

## Hyper Quality 评审

2026-11-21 使用 [Hyper Quality 0.3.0](https://github.com/MkaliezZ/Hyper-Quality) 复评。方案设计从 v0.1 的 **56.75/100** 提高到 **90.12/100**。产品实证得分下限从 **5.00/100** 提高到 **39.81/100**，材料覆盖总权重的 **82%**。定稿工程核验通过 15 项检查，涵盖 Python 参考接收端、冻结用例及 Node.js 读回。真人采用意愿、净节省工时和真实数据使用权仍待首轮对照验证，关键假设实证加分为 **0/10**。

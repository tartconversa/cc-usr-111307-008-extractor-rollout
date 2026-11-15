# 目录网页抽取规则的影子验证与漂移熔断

项目保存供方接口协议、演练数据、规则发布机制与回环地址服务。所有示例记录均使用本机演练资源，不连接实际供应平台。

运行环境为 Python 3.10 或以上，仅使用标准库。

```sh
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m src.source --port 8765
```

前两条命令分别检查语法与全部行为；第三条启动本机供方服务，使用 Ctrl+C 结束。

## 演练页面

`GET /classic` 是旧布局；`GET /drift` 把推荐机构（`aside`）放到资源主体之前，并把供方从 `.provider` 改成 `.owner`；`GET /drift2` 把推荐区放到主体之后；`live-a`/`live-b`/`live-c` 是不同位置的实时样本；`series-*` 是新增的连续出版物页面族，其中 `series-broken` 缺少名称且出现两个机构节点。

`fixtures/source.json` 的 `gold` 是人工核对字段，`release` 是示例阈值；`fixtures/rules.json` 是旧规则与候选规则，`fixtures/release.json` 是发布配置。页面内 `main#resource` 代表资源主体，`aside` 是推荐区域。服务只返回原网页，不执行字段抽取或规则审核。

`GET /__state` 可查看当前演练状态与已收到的请求。`POST /__control` 接受 JSON，修改该服务实例的演练状态；此入口只用于本机供方模拟，不属于生产接口。服务端状态不跨进程保留。

## 机制概览

| 模块 | 职责 |
| --- | --- |
| `src/html_subset.py` | 明确说明的 HTML 定位子集；伪类、兄弟组合子、命名空间等在注册规则时直接拒绝 |
| `src/rules.py` | 不可变规则包、发布候选、按页面族的阈值与语义约束；摘要覆盖全部字段 |
| `src/extractor.py` | 单版本严格抽取；保留响应字节摘要、URL、页面族、规则摘要与节点证据 |
| `src/store.py` | SQLite 持久化：规则、候选、配置、响应字节、人工真值、发布窗口、影子任务、审核、激活与熔断事件 |
| `src/review.py` | 固定窗口上的逐页审核：必需字段通过率、歧义率、与真值冲突数、新旧差异 |
| `src/workers.py` | 影子 worker（只产差异）与生产运行器（活动版本唯一输出源、熔断计数） |
| `src/cli.py` | seed / review / activate / production / show 演练命令 |

**不可变规则包**：`RulePackage` 是冻结数据类，摘要由规范化 JSON 计算；同一 `(page_family, version)` 一旦登记就不能改指向（`register_rule` 抛 `ImmutableRuleError`）。候选包、发布配置同样按内容摘要登记，窗口只引用摘要。

**严格抽取**：必需字段缺失、定位命中多个节点、`within_resource` 越出资源主体、`nonempty`/`max_length`/`matches` 不满足，任一出现即整页隔离。隔离页不暴露值（`PageExtraction.values()` 会抛错），不会用空值或“取第一个节点”掩盖。可选字段缺失不隔离，且不计入 `values()`。

**证据**：每个字段结果带 `locator`、命中节点的路径/行/列/标签/属性/文本节选及 `within_scope` 标志；页面结果带 `url`、`response_digest`、`rule_digest`、`extractor_version`。存储保留响应原始字节，审核时用同一版本重新计算影子结果并与已提交结果逐步比对。

**真值独立**：`set_gold` 必须给出核对人、核对时间与 `manual-review`/`independent-transcription` 来源；实时页面（`source_tag=live`）不得写入真值；已固定的真值不能改成不同内容；旧抽取结果从不回填真值。

**固定窗口**：`create_window` 冻结候选、配置、真值页面集合与实时样本序号区间，并一次性生成影子任务。窗口期间新到的捕获不会进入本次审核。实时样本序号在每个页面族内唯一，避免同一样本被重复计数。

**按族审核**：每族分别计算必需字段通过率、歧义率、与真值冲突数、新旧差异页数与页面明细。样本量或任一阈值不达标即 `hold`；其他族结果再好也不能补足缺失样本。实时页只做漂移提示：`drift_hint=true` 且 `gold=null`，报告不会声称候选在无真值页上“更正确”。

**版本比较激活**：激活是单事务比较并交换。窗口冻结时的活动摘要与当前活动摘要不一致，窗口标记为 `stale` 并拒绝激活；审核通过但活动版已变化，必须重新审核。同一候选不会在激活后第二次生效。

**worker 版本固定**：任务记录窗口的 `extractor_version`、候选 `rule_digest`、基线 `rule_digest` 与 fencing 值。租约、fence、worker、窗口状态、规则摘要、抽取器版本、捕获字节摘要任一不符，结果被拒绝并写入 `rejected_results`；旧 worker 不能把旧版结果冒记成新版。过期租约可被新 worker 接管，旧持有者随后提交会被 fence 拒绝。

**熔断与不回退**：生产结果只由活动版本产生，一页只用一个版本抽取，绝不拼接两个版本的字段。连续语义失败达到 `circuit_failure_threshold` 后，该页面族熔断：后续页面保留为捕获、不再产出，也不自动回退到已知误抽的版本。恢复只能通过一个“不同于打开熔断的那条规则”的已审核修订激活；同一规则不能重新武装熔断器。

## 演练流程

终端一：

```sh
python -m src.source --port 8765
```

终端二：

```sh
python -m src.cli seed     --database drill.sqlite3 --base-url http://127.0.0.1:8765
python -m src.cli review   --database drill.sqlite3 --window <window_id>
python -m src.cli review   --database drill.sqlite3 --window <window_id> --pages   # 逐页证据
python -m src.cli activate --database drill.sqlite3 --window <window_id> --actor alice
python -m src.cli production --database drill.sqlite3 --base-url http://127.0.0.1:8765 \
    --family resource-detail --pages classic drift live-a
python -m src.cli production --database drill.sqlite3 --base-url http://127.0.0.1:8765 \
    --family series --pages series-live-1 series-live-2 series-broken series-broken
python -m src.cli show     --database drill.sqlite3
```

预期观察：

1. 旧规则在 `drift`、`drift2`、`live-a`、`live-b` 上仍取到非空字符串（`推荐机构`、`合作机构` 等），但因 `within_resource` 越界被整页隔离；逐页报告同时显示基线状态 `semantic_violation` 与候选 `ok`。
2. 候选在固定窗口（2 个人工核对页 + 序号 1–2 的实时页）满足全部阈值后 `pass`，随后激活，活动摘要与 epoch 变化写入 `activation_events`。
3. `series` 族连续两次语义失败后熔断，活动规则保持不变（不回退），后续页面保留但不产出；`circuit_events` 记录打开原因与失败页。
4. `show` 可回查每个窗口的审核报告：决定绑定到具体 `capture_id`、`url`、`response_digest`、字段状态与节点证据，而不是只给百分比。

`tests/test_rollout.py` 覆盖事故复现、严格抽取、不可变规则、真值与捕获、窗口冻结、样本量隔离、worker fencing、激活比较交换、熔断与不回退、逐页审计。`tests/test_source.py` 与 `tests/test_html_subset.py` 分别检查供方源与定位子集。

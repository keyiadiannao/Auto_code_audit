[English](README.md) | [简体中文](README.zh-CN.md)

# Auto Code Audit

[![CI](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml/badge.svg)](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)

> **方向(2026):** 从通用静态审计器转向面向 AI 编程的 **implementation-reuse
> firewall** —— 在写新代码之前,先找出它与仓库中已有实现的职责重叠。见
> [VISION.md](VISION.md);下方的审计扫描器保留为证据提供者。

你的 AI 助手重构了一个大型 Python 代码库,测试也全通过了。然后细微的 bug 开始浮现:
一个靠哈希锁定调用的 runner 被一次重命名悄悄破坏;一个归档根路径被写死,而不是遵循
环境变量的交接;同一份函数的两处拷贝各自漂移。静态检查器找不出这些——因为表面上看
**什么都没错**,只是其中一个实现已经死了,另一个悄悄变了。

Auto Code Audit 正是为这种场景设计的三层工具。它**生成**候选清单(死模块、重复实现、
硬编码漂移、契约违规、TeX 文本里的 AI 写作痕迹),**强制**对每个候选做语义审查,然后
**验证**被接受的改动(测试、打包门禁、证据检查)。它曾在自己来源的项目里抓到上面那两
类 bug——一个测试 100% 通过、但确实有问题的代码库。

```text
$ python run_all.py --package src
DEADCODE_SCAN package=src scanned=8 USED=0 ENTRYPOINT=0 PACKAGE=2 DEAD=4 ...

## Duplicate-implementation candidates
### [high] `a52d3baa1512`: 2 members (edge similarity 0.909)
- `experiments/e01.py:load_min_mask` (7 lines)
- `lib/runner.py:load_min_mask` (9 lines)

### Env-contract candidates
- env `E02_MODE` written at `experiments/e02.py:2` but never read in-package
```

没有任何东西会被自动删除。每个候选都要给出裁决(`false positive` 写入一条抑制记录;
其余都要对应一次代码改动),而且工具出厂自带一个**空的**抑制注册表:每个项目用自己的
语义审查积累自己的 `ignore.json`。

## 目录

- [三个层次](#三个层次)
- [快速开始](#快速开始)
- [关键选项](#关键选项)
- [扫描器](#扫描器)
- [语义审查(第二层)](#语义审查第二层)
- [验证门禁(第三层)](#验证门禁第三层)
- [基准结果](#基准结果)
- [持续集成](#持续集成)
- [项目结构](#项目结构)
- [诚实局限](#诚实局限)
- [许可证](#许可证)

## 三个层次

绝不把静态命中直接变成一次删除。相似度只是**候选生成**;裁决的单元是调用方的**功能
契约**。

1. **第一层 —— 生成候选**:用确定性扫描器。
2. **第二层 —— 审查每个候选**:对照其调用点和角色,为每个调用方族写一张契约卡。
3. **第三层 —— 验证被接受的改动**:用测试、打包门禁和证据检查。

## 快速开始

工具开箱即用,只要从检出目录直接运行,仅依赖标准库(Python 3.10+)。`pip install -e .`
额外安装三个控制台命令:`auto-code-audit`、`auto-code-adjudicate`、`auto-code-verify`。

```text
# 第一层:为被审计项目生成候选
python run_all.py --root /work/foo --package src

# 第二层:裁决候选(从 reports/verdicts.json 断点续审)
python adjudicate.py --report /work/foo/reports/latest.json

# 第三层:验证一次修复(测试已经跑过,所以把门禁指向结果工件;
# 或用 --test-command / --no-tests)
python -m pytest /work/foo/tests -q
python run_verify.py --report /work/foo/reports/latest.json \
  --verdicts /work/foo/reports/verdicts.json --previous /work/foo/reports/pre.json \
  --scope lib --test-result /work/foo/reports/ci-result.json
```

对当前目录下的包运行全部扫描器(用 `--root <repo>` / `--package <name>` 覆盖)。所有
工作流状态——报告、`ignore.json`、`LESSONS.md`、裁决——默认落在**被审计项目的**根目录,
而不是工具检出目录:

```text
<root>/reports/latest.json
<root>/reports/latest.md
```

要审计第三方或不可变目录,把状态路由到目标之外并传 `--read-only`(若有任何可写状态会
落在 `--root` 之下,该运行会被拒绝):

```text
python run_all.py --root /work/vendor --package src --profile code \
  --state-dir /work/audit-state/vendor --read-only
```

### 作为 agent 技能安装

本仓库本身也是一个 agent 技能:把整个仓库安装或克隆为技能目录,让 `SKILL.md` 与扫描器
脚本并排;然后调用 `$auto-code-audit` 做复用检查、改后审计、裁决或修复验证。只拷贝
`SKILL.md` 不够——工作流会调用本仓库内置的确定性 CLI。技能是面向 agent 的协议,CLI 是
证据引擎。项目特定规则放在被审计项目的 `audit.config.json` 里,而不是 fork 一份通用技能。

## 关键选项

| 选项 | 作用 |
|---|---|
| `--profile code\|research` | 只跑代码扫描器,或加入可选的 research TeX 风格通道(默认 `code`) |
| `--no-doc-channel` | 更快的纯代码死模块通道 |
| `--state-dir <path>` | 设置 report / ignore / lessons / verdict 的默认路径 |
| `--read-only` | 要求外部状态目录;禁止在被审计根目录下写状态 |
| `--all-py` | 递归扫描所有 Python 文件,覆盖 `subdirs` 配置 |
| `--public-api` | 把无引用的公开包模块标为 `PUBLIC_API_CANDIDATE` 而非 `DEAD` |
| `--duplicate-threshold` / `--duplicate-min-chars` | 重复检测灵敏度 |
| `--ignore ignore.json` | 已批准的抑制注册表(第二层输出) |
| `--cli-smoke` | 先对每个扫描器跑 `--help`;任一回归则非零退出 |
| `--stale-check` | 报告 `ignore.json` 中目标已消失的条目(只读) |
| `--exhaustive` | 渲染完整工作表,包括低价值 cohort |

`adjudicate.py --check` 在仍有未裁决候选时让 CI 失败。误报会更新项目的 `ignore.json`
(带 `date` 和 `owner`)与 `LESSONS.md`;其他裁决留在 verdict 日志里,因为它们需要代码
改动或等价性证据。每个非延迟裁决都记录稳定的 `target_id` 与 `finding_evidence_hash`
(`{scanner, target_id, detail}` 的摘要),因此候选证据一变就要求重新审查。可选的
`<root>/audit.config.json` 调整阈值与排除项:

```json
{"schema_version": 1, "regions": {"shared_paths": ["lib", "src/core"]}}
```

## 扫描器

| 扫描器 | 候选信号 | 常见误报 |
|---|---|---|
| `scan_deadcode.py` | 无可见 import 或文档引用 | 动态派发、手动调用的 runner、仅用于溯源的工具 |
| `scan_duplicates.py` | 结构相似的函数片段 | 对称的实验分支、刻意独立的干预边界 |
| `scan_forks.py` | 跨文件 callable 共享大段骨架但主体已分化(≥40 行、≥75% token 相似) | 契约不同的刻意特化 fork |
| `scan_contracts.py` | 被当库使用的模块、动态模块加载/状态变更、转发包装、无引用顶层函数、env 交接与加载严格度违规 | 有价值的适配器、带显式生命周期的插件加载器、刻意独立的审计实现 |
| `scan_regions.py` | 重复的能力块:命名 helper 的内联拷贝、跨文件共享能力块、短高密度块、带 API 调用的近全同函数(`twin_match`) | 契约确实不同的并行分支、通用校验样板 |
| `scan_hardcoded.py` | 已知会从共享行为漂移的写法 | 独立的哈希契约、刻意的冻结转发实现 |
| `scan_capabilities.py` | 脚本局部重实现库函数 | 契约真实的薄角色包装 |
| `scan_style.py` | TeX 文本里的 AI 典型写作信号(分号链、模板开头、破折号密度、突发性、裸 `\pm`) | 技术枚举、统计语境措辞 |

读报告前值得知道:

- `scan_deadcode.py` 把 `__main__` 守卫的脚本标为 `ENTRYPOINT`、包初始化标为 `PACKAGE`,
  绝不标为 `DEAD`。其依赖图覆盖三类通道:静态 import、`sys.path` 钉住子目录下的裸 import、
  importlib 文件加载。
- `scan_contracts.py` 还能检测运行时创建的模块绑定,以及四条 AST 指纹看不见的
  运行时盲区通道:`env_written_not_read`、`generation_path_without_env`、
  `cli_without_bootstrap`、`defensive_param_loosening`。
- `scan_regions.py` 发出 `helper_not_reused`(命名函数的内联拷贝)、`shared_capability`、
  `short_block_cluster`、`twin_match`(带 API 调用的近全同函数)四类簇。
- `scan_style.py` 以保跨度方式把 TeX 剥离成纯文本,所以报告的行号与源文件一致;它扫描
  `--tex-dir`(默认 `docs`),跳过归档目录。

## 语义审查(第二层)

在给出裁决前,为每个调用方族写一张契约卡:功能角色与归属、输入/输出、错误、副作用、
配置与持久化行为、现有规范实现、阻止直接复用的语义差异、改动前所需的等价性/证据门禁。
然后为每个候选给一个处置:

| 处置 | 动作 |
|---|---|
| 必要的特化 | 就地保留 |
| 有价值的适配器 | 保留并按其角色命名 |
| 独立审计 | 单独保留并做等价性测试 |
| 兼容性债务 | 迁移活跃调用方,再删除/弃用 |
| 真重复 | 合并 |
| 误报 | 审查后才抑制 |

在改 `ignore.json` **之前**,先把理由记进 `LESSONS.md`。干净的静态报告不能推翻一个失败的
行为或溯源门禁。

## 验证门禁(第三层)

```text
python -m pytest tests -q              # 本工具自己的 fixtures
python -m pytest <package>/tests -q    # 目标包
python run_verify.py --report <new> --verdicts <verdicts.json> --previous <old>
```

`run_verify.py` 在修复后重新审计,并在下列情况拒绝:某个代码处置的 `target_id` 仍出现在
新报告里;某个仍存在发现的证据哈希未变;或补丁范围内新增了此前不存在的高/中候选。
新候选的严重度在各扫描器 schema 之间统一在一个函数(`run_all.finding_severity`)里,所以
一次把模块搁置、一次加防御性参数放宽、一次写了 env 却没人读,都会被拒绝——不只是重复
或 region 命中。

测试证据有三种可机检方式(`--test-command`、`--test-result`、`--no-tests` 三者互斥):

- `--test-command "<cmd>"` —— 在门禁内运行目标测试。
- `--test-result <file>` —— 消费带溯源的机读工件(`status`、一致的 `exit_code`、与报告
  commit 相同的 `git_head`);手写的 `{"status":"passed"}` 不构成完整证据。
- `--no-tests` —— 声明行为验证委托给门禁之外;接受,但结果标记 `fully_verified: false`。

没有任何测试证据的代码处置会被拒绝——门禁从不自批。`fully_verified` 仅在门禁通过、测试
证据被机检、**且**提供了可比较的改前报告(`--previous`)时才为真;不兼容的基线直接拒绝,
而不是信任一份垃圾基线。

> **信任模型说明。** `fully_verified` 只表示所提供的报告、裁决与测试证据满足了这个确定性
> 门禁——它**不是**对目标仓库"已被完整重扫并测试"的密码学证明或独立复现证明。门禁验证的是
> 这些工件内部一致、且绑定到被审计的源码树;它本身不会重跑扫描器,并且信任操作者提供的
> `--scope` 与 `--test-command`。用于无人值守的 merge gate 时,请在同一个流水线里跑一次
> fresh scan,并在 CI 层对测试证据做真实性认证。

## 基准结果

试点语料是六个小而流行的 Python 项目——click、httpx、pytest、requests、starlette、
werkzeug——各钉在固定 commit 上。工具在那里发出的每个候选都经过人工裁决,标签提交在
`benchmarks/labels/` 下作为 ground truth。harness 克隆钉住的 commit,跑只读 `code` profile,
再用标签给新一次运行打分。

| 指标 | 值 |
|---|---|
| 已裁决候选 | 594(618 个标签;24 个因扫描器改动而失效) |
| 确认缺陷(true findings) | 16 |
| 收敛后的独立问题数 | 10 |
| 精确率 | 0.027 |
| 审查负担 | 每个确认缺陷约 37 个候选 |
| 变异语料召回率 | 1.000(25/25 注入目标) |

确认缺陷集中在两条通道——`duplicates`(10)与 `regions`(6);其他扫描器在这个语料里没有
确认缺陷。低精确率是**刻意**的:工具宁可过度报信号,也绝不静默漏掉一个真缺陷;下面的
期望价值 cohort 则压缩审查成本。

| cohort | 候选数 | 真发现 | 精确率 |
|---|---:|---:|---:|
| high(近精确重复、region 孪生) | 69 | 12 | 0.17 |
| medium(共享能力 region) | 21 | 2 | 0.10 |
| low(其余全部) | 504 | 2 | 0.004 |

markdown 工作表默认隐藏 low cohort,所以审查从携带 16 个确认缺陷中 14 个的 ~90 个
high/medium 候选开始;加 `--exhaustive` 恢复完整表面。

变异语料(`benchmarks/mutation/`)为每条通道注入一个已知缺陷,并按精确的
`(scanner, target_id)` 匹配核对召回率——打错目标算漏检。25 个注入,25 个命中。

方法学、指标定义、证据融合、以及逐批次的裁决历史,记录在
[BENCHMARKS.md](BENCHMARKS.md)。

## 持续集成

`.github/workflows/ci.yml` 在每次 push 与 pull request 上、跨 Python 3.10–3.13、在 Ubuntu
与 Windows 上运行:测试套件、阻塞式 mypy、入口 `--help` 烟测、端到端 dogfood 自审计(含
报告 diff 路径)、wheel 构建/导入烟测、以及空白符检查。

## 项目结构

```text
run_all.py              一键编排 + 汇总报告 + 报告 diff
adjudicate.py           可断点续审的第二层语义审查
run_verify.py           引擎自有的确定性验收门禁(修复后)
scan_*.py               确定性扫描器(deadcode、duplicates、regions、forks、
                        contracts、capabilities、hardcoded、style)
scan_cli_smoke.py       入口 --help 回归门禁
pyproject.toml          打包元数据;控制台脚本
benchmarks/             固定 commit 试点语料、标签与只读 harness
SKILL.md                完整的三层 agent 协议
LESSONS.md              误报教训档案(第二层前先读)
ignore.json             已批准抑制注册表(出厂为空)
tests/                  每个扫描器的 fixture 测试
```

## 诚实局限

Auto Code Audit 是候选生成器,不是裁决引擎。它自己的设计,要求它以对待你的代码库的同一
份诚实来对待自己:

- **大多数候选是刻意为之的误报。** 在钉住的公开语料上,594 个已裁决候选里只有 16 个
  确认缺陷(约 2.7%)——每个确认缺陷要审约 37 个候选。它刻意过度报信号,以确保什么都不
  被静默漏掉;代价是每个候选仍需人工(或 LLM)语义审查。
- **真正的工作在第二层。** 静态命中永远不是 bug 的证明——工具强制你写契约卡并裁决。
  跳过第二层,工具只会产出噪声。
- **它只能看见静态可见的东西。** 动态派发、运行时配置、只有执行时才浮现的行为都是盲区;
  contracts 扫描器对其中一些有通道,但它们仍是审查候选,不是裁决。
- **它从不替你做决定。** 没有任何东西被自动删除;每个改变代码的处置都是工具记录、事后
  验证的人类决定——不是它做的。
- **基准数字是语料绑定的。** 精确率/召回率来自六个钉住的小型公开项目加一个合成变异
  fixture;它们描述的是那些语料,不是你的代码库。
- **这个工具本身就是 AI 维护的。** 它是一个 dogfood 项目:它自己的 CI 用这些扫描器审它
  自己。请以它对待你的怀疑程度,来对待它自己的主张。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

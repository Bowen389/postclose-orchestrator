# postclose-orchestrator

A 股 17 个盘的**唯一外部开关**。不选股、不算账，只做一件事：看哪些包缺当日盘后产物，缺的立刻 `workflow_dispatch`，然后回查 run 有没有真的起来。

## 为什么要它
2026-11-20 / 11-23 两次实测：这个账号的 Actions `on.schedule` 被整段延迟 6~10 小时（11-23 周一 17:00 档实际落地 17:22–17:41，另有 9 个仓 17:59 前一次 run 都没有）。所以"准点"这件事没法靠再加几条 cron 解决 —— 只能把判定和触发放到一次可被随时唤起的调用里。

## 你需要的 90 秒设置
1. 本仓 Settings → Secrets and variables → Actions → 新建 **`GH_ACTIONS_PAT`**。
   fine-grained PAT：Repository access = Only select repositories → 勾上那 17 个仓；
   Permissions 里给 **Actions: Read and write**（这是 dispatch 端点需要的，`Workflows` 只够改 yml，不够发 dispatch）+ **Contents: Read and write**。
2. 完事。定时档已经写好在 `.github/workflows/sweep.yml`（北京 17:00 / 17:30 / 18:00 / 18:30 / 21:00 / 次日 09:00）。

## 手点
Actions → 「盘后扫描编排」→ Run workflow，三个输入框：
- `date` 留空即按北京时间自动判定（开盘前会自动回溯上一交易日）
- `only` 填 `zonghe,dayitong` 只补这几个
- `force` 选 `true` 则已有产物也重跑

## 两波语义（你要的那个"分开等 30 分钟"）
- **第一波**：15 个不读别人产物的包，判定缺就立刻 dispatch，不等任何东西。
- **第二波**：`yitibao`、`dayitong`（全组只有这两个读别人的产物）先等自己的上游落盘，**齐了立刻走，最多等 `downstream_wait` 秒（默认 1800 = 30 分钟）**，超时也照发（让它自己的 `aggregate_watch` 再等 1200s，兜到 PARTIAL 为止）。
- "上游就绪"的判定与下游代码完全同口径：**文件在 + 内嵌日期等于目标日**（`top/zonghe/ceshi/zhulizhuoyao/2j3` 看 `date`，`s9-duoyinzi` 看 `asof`）。只看"文件在不在"会放行昨天的陈旧文件 —— `ceshi/reports/state.json` 文件名里根本没有日期。
- 依赖表来源：两仓 `aggregate_watch.py:38-40` 的 `sources`，以及 `is_dayitong = Path('daily.py').exists()` 这个分支（`yitibao` 没有 `daily.py` → 只等 3 个上游；`dayitong` 等 6 个）。
- 想跳过等待：`--downstream-wait 0`（或网页里把 `downstream_wait` 填 `0`）。

## 开关
```bash
GH_ACTIONS_PAT=$(cat ~/.pat) ./kick.sh                # 补齐今天缺的
GH_ACTIONS_PAT=$(cat ~/.pat) ./kick.sh --dry-run        # 只看计划，不发请求（可匿名跑）
./kick.sh --only zonghe,dayitong --downstream-wait 900  # 只点这两个
```
本机、NAS、任意 cron 都能用；也可以放在 `~/.bashrc` 里做成一个 `kick` 别名。它和网页 Run workflow 走的是同一份 `scripts/sweep.py`，行为完全一致。

## 判定依据
`repos.json` 里每个仓的 `globs` 就是它自己 `postclose-watchdog.yml` 里的 `WATCHDOG_GLOBS` —— 单一事实来源，改包不用改这里。命中任一 glob 即视为已产出，直接跳过，所以每天多档重复调用不烧 runner。

## 本地跑
```bash
python3 scripts/sweep.py --dry-run                      # 匿名只读，看今天谁缺
GH_ACTIONS_PAT=$(cat tok) python3 scripts/sweep.py --only zonghe
```
退出码：0=全部就绪或已触发；1=有仓没起来（表里 FAIL/WARN）；2=参数或 repos.json 问题。

## 已知边界（别把它当保险箱）
- 它的 6 条 schedule 也在同一个被延迟的调度器上 —— 区别是**只有 1 个仓的 6 档**，而不是 17 个仓的 153 档；而且它随时可被手点救场。
- `WARN` 表示 dispatch 已被接受（204）但 `--verify-secs` 内没等到 run，通常是队列拥塞，不是没生效，稍后回看即可。
- 休市日（非周末的节假日）它会照样跑一轮并全部 skip 或触发一次空跑；各包自己的交易日历负责拒绝出报告。

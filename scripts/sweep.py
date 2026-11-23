#!/usr/bin/env python3
"""盘后扫描总开关：一次把 17 个包补齐；依赖别人产物的包单独排第二波，等上游落盘（默认上限 30 分钟）。

为什么这样分波（全部来自 2026-11 实测）：
  * Actions 的 on.schedule 是共享队列，标称时刻只是投递意向 —— top 的 run 曾被推迟 6.6~10 小时。
    所以这个脚本不赌时刻，只赌"缺就补、补完回查"。
  * push/manual 通道是秒级的（11-23 实测：16:52 手点 → 16:53 报告写好）。
  * dispatch 端点是 .../actions/workflows/{file}/dispatches —— 复数。单数 404，这是我上一版的致命拼写错误。
  * 下游两仓的上游清单与路径，直接照它们各自 aggregate_watch.py 的 sources 抄（那里是判定口径的源头）：
      yitibao  → top/zonghe/s9-duoyinzi 的 reports/{day}.json
      dayitong → 上述 3 个 + ceshi:reports/state.json、zhulizhuoyao:reports/{day}-close.json、2j3:state/{day}.json
    （aggregate_watch.py 用 `Path('daily.py').exists()` 区分两仓；yitibao 没有 daily.py，故只等 3 个。）

退出码：0 全部就绪/已触发；1 有 FAIL/WARN/UNKNOWN；2 参数或配置错。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "postclose-orchestrator", "Accept": "*/*"}
OWNER_ALLOWED = {"Bowen389"}     # 只允许操作这一个账号下的仓
ALLOWED = set(json.load(open(os.path.join(ROOT, "repos.json"), encoding="utf-8")))   # 17 个，多一个都不行


def log(msg):
    print(msg, flush=True)


class Net:
    """contents API 会被匿名限流；raw 不参与限流但对"不存在"稳定回 404 → raw 优先。"""

    def __init__(self, token=None, ref="main"):
        self.token = token
        self.ref = ref

    def api(self, method, path, body=None):
        h = dict(UA, **{"Accept": "application/vnd.github+json"})
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, data, h, method=method), timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else {})
            except Exception:
                return e.code, {"message": raw[:180].decode("utf-8", "replace")}
        except Exception as e:
            return 0, {"message": f"{type(e).__name__}: {e}"}

    def exists_raw(self, owner, repo, rel):
        url = f"{RAW}/{owner}/{repo}/{self.ref}/{rel}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            return None                       # 5xx 等：判定不了
        except Exception:
            return None

    def upstream_ready(self, owner, repo, rel, day, date_key):
        """上游是否真的可用：文件在 + 内嵌日期 == 目标日。

        只看"文件在不在"会放行昨天的陈旧文件（ceshi 的 state.json 文件名里就没有日期）。
        判定字段照抄下游代码：top/zonghe/ceshi/zhulizhuoyao/2j3 用 date，s9-duoyinzi 用 asof。
        """
        url = f"{RAW}/{owner}/{repo}/{self.ref}/{rel}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return str(doc.get(date_key)) == day

    def status(self, owner, repo, relpaths, day):
        """present / missing / unknown —— 三态必须分开，压成两态会把"限流"当成"已产出"（11-23 踩过）。"""
        for rel in relpaths:
            p = rel.replace("{D}", day)
            rc = self.exists_raw(owner, repo, p)
            if rc is True:
                return "present", p
            if rc is False:
                return "missing", p
            code, _ = self.api("GET", f"/repos/{owner}/{repo}/contents/{p}?ref={self.ref}")
            if code == 200:
                return "present", p
            if code == 404:
                return "missing", p
            log(f"    （{repo}/{p} 两源都判不了：raw=err contents={code}）")
        return "unknown", relpaths[0].replace("{D}", day)


def target_day(now):
    """盘后 → 当天；开盘前/周末 → 上一工作日（与各仓 watchdog 的 preopen 回溯保持一致）。"""
    d = now.date()
    if now.time() < dt.time(16, 45):
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="Bowen389")
    ap.add_argument("--date", help="YYYY-MM-DD，覆盖目标交易日")
    ap.add_argument("--only", help="逗号分隔仓名子集")
    ap.add_argument("--force", action="store_true", help="已有产物也重新触发")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--downstream-wait", type=int, default=1800, help="第二波等上游落盘的上限秒（0=不等）")
    ap.add_argument("--poll-secs", type=int, default=45)
    ap.add_argument("--verify-secs", type=int, default=240, help="等 dispatch 的 run 出现")
    ap.add_argument("--skip-downstream", action="store_true")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "repos.json"), encoding="utf-8"))
    now = dt.datetime.now(SH)
    day = a.date or target_day(now).isoformat()
    repos = [x.strip() for x in a.only.split(",")] if a.only else list(cfg)
    bad_names = [r for r in repos if r not in cfg]
    if bad_names:
        log(f"::error::repos.json 里没有这些仓：{bad_names}")
        return 2
    if a.owner not in OWNER_ALLOWED:
        log(f"::error::拒绝操作 owner={a.owner}：只允许 {sorted(OWNER_ALLOWED)}")
        return 2
    outside = [r for r in repos if r not in ALLOWED]
    if outside:
        log(f"::error::拒绝操作清单外的仓：{outside}")
        return 2
    net = Net(os.environ.get("GH_ACTIONS_PAT"), a.ref)
    if not net.token and not a.dry_run:
        log("::error::缺 GH_ACTIONS_PAT，无法 dispatch")
        return 2

    wave2 = [r for r in repos if cfg[r].get("upstreams")]
    wave1 = [r for r in repos if r not in wave2]
    log(f"目标交易日 {day}（北京 {now:%H:%M}）  第一波 {len(wave1)} 个独立包；第二波 {len(wave2)} 个依赖包"
        f"（等上游上限 {a.downstream_wait}s）  dry_run={a.dry_run}")

    rows, plan = [], {}
    for r in repos:
        st, hit = net.status(a.owner, r, cfg[r]["globs"], day)
        plan[r] = (st, hit)
        if st == "unknown":
            rows.append((r, "unknown", 1, "判定失败，本轮不碰"))
            log(f"  ? {r:<13} 判定失败 → 不触发（下轮再查）")
        elif st == "present" and not a.force:
            rows.append((r, "skip", 0, f"已有 {hit}"))
            log(f"  ✓ {r:<13} 已产出，跳过")

    def dispatch(r, wave, note):
        st0, _ = plan.get(r, ("missing", ""))
        if a.dry_run:
            rows.append((r, "dry", wave, f"[{wave}] 将 dispatch {cfg[r]['main_workflow']}；{note}"))
            log(f"  · {r:<13} 第{wave}波 → 会 dispatch（{note}）")
            return True
        t0 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        code, resp = net.api("POST", f"/repos/{a.owner}/{r}/actions/workflows/{cfg[r]['main_workflow']}/dispatches",
                             {"ref": a.ref})
        if code != 204:
            rows.append((r, "FAIL", wave, f"dispatch {code}: {str(resp.get('message'))[:60]}"))
            log(f"  ✗ {r:<13} dispatch 返回 {code}（期望 204）")
            return False
        run, deadline = None, time.monotonic() + a.verify_secs
        while time.monotonic() < deadline:
            time.sleep(15)
            _, d = net.api("GET", f"/repos/{a.owner}/{r}/actions/runs?event=workflow_dispatch&per_page=5")
            for x in d.get("workflow_runs", []):
                if (dt.datetime.strptime(x["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= t0 - dt.timedelta(seconds=120)
                        and x["name"] == cfg[r]["main_name"]):
                    run = x
                    break
            if run:
                break
        if not run:
            rows.append((r, "WARN", wave, f"{a.verify_secs}s 内没等到 run（队列拥塞，稍后回看）"))
            log(f"  ⚠ {r:<13} 第{wave}波已 dispatch，但没等到 run")
            return False
        rows.append((r, "ok", wave, f"run #{run['id']} 已创建"))
        log(f"  → {r:<13} 第{wave}波 run #{run['id']} 已创建")
        return True

    # ── 第一波：15 个不依赖别人的包，立刻补齐 ─────────────────────────────
    for r in wave1:
        if plan[r][0] != "missing" and not a.force:
            continue
        dispatch(r, 1, "无上游依赖")

    # ── 第二波：只等自己那几个上游的 json 出现，出现即走，最多等 --downstream-wait ──
    if not a.skip_downstream:
        pend = [r for r in wave2 if plan[r][0] == "missing" or a.force]
        for r in pend:
            need = [(u["repo"], u["path"].replace("{D}", day), u.get("date_key")) for u in cfg[r]["upstreams"]]
            ready = {}
            for n, pth, dk in need:
                ready[n] = (net.upstream_ready(a.owner, n, pth, day, dk) if dk
                            else net.exists_raw(a.owner, n, pth) is True)
            log(f"\n  ▷ {r}：等 {sum(1 for v in ready.values() if not v)} 个上游（共 {len(need)}）")
            for n, ok in ready.items():
                log(f"      {'✓' if ok else '…'} {n}")
            t0 = time.monotonic()
            if a.dry_run:
                miss = [n for n, ok in ready.items() if not ok]
                dispatch(r, 2, ("上游已齐，立刻触发" if not miss else "等 " + ",".join(miss) +
                                f"（上限 {a.downstream_wait}s）"))
                continue
            while not all(ready.values()) and time.monotonic() - t0 < a.downstream_wait:
                time.sleep(a.poll_secs)
                for n, pth, dk in need:
                    if not ready[n]:
                        ready[n] = (net.upstream_ready(a.owner, n, pth, day, dk) if dk
                                   else net.exists_raw(a.owner, n, pth) is True)
                got = sum(1 for v in ready.values() if v)
                log(f"      … 已等 {int(time.monotonic()-t0)}s：上游 {got}/{len(need)}")
            waited = int(time.monotonic() - t0)
            miss = [n for n, ok in ready.items() if not ok]
            dispatch(r, 2, (f"上游齐（等了 {waited}s）" if not miss
                            else f"超时 {waited}s，仍缺 {','.join(miss)} → 照样触发，让它自己 PARTIAL"))
        log("")

    n_present = sum(1 for x in rows if x[1] == "skip")
    n_act = sum(1 for x in rows if x[1] in ("dry", "ok"))
    bad = [x for x in rows if x[1] in ("FAIL", "WARN", "unknown")]
    tbl = ["| 波 | 仓 | 结果 | 说明 |", "|---|---|---|---|"]
    for r, st, w, note in sorted(rows, key=lambda x: (x[2], x[0])):
        tbl.append(f"| {w if w else '-'} | `{r}` | {st} | {note} |")
    summary = "\n".join(tbl) + (f"\n\n目标日 {day}：已产出 {n_present}；本轮触发 {n_act}；需人工看 {len(bad)}。\n"
                                f"第二波等待上限 {a.downstream_wait}s（0 = 不等）。\n")
    if not a.dry_run:
        with open(os.path.join(ROOT, "dispatch-audit.log"), "a", encoding="utf-8") as fh:
            for r, st, w, note in rows:
                if st in ("ok", "FAIL", "WARN"):
                    fh.write(f"{now:%m-%d %H:%M:%S} {st:<5} wave{w} {a.owner}/{r}  {note}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(summary)
    log(summary)
    if bad:
        kinds = {}
        for x in bad:
            kinds.setdefault(x[1], []).append(x[0])
        log("::warning::" + "；".join(f"{k}：{','.join(v)}" for k, v in kinds.items()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

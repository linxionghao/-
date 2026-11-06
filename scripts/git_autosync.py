#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每 5 分钟把工作区改动提交并推到 GitHub。

用法（一般由 Windows 计划任务调用，不必手跑）：

    python scripts/git_autosync.py            # 正常一轮
    python scripts/git_autosync.py --dry-run  # 只打印将要做什么
    python scripts/git_autosync.py --status   # 看最近几轮的记录

为什么需要这个脚本，而不是计划任务里直接写 `git add -A && git commit && git push`：

1. **推送根本走不通**。本机直连 github.com:443 被阻断（curl 28 超时），
   系统代理与 WorkBuddy 代理一个 502 一个连不上。唯一可行的是 github-web-skill
   的 pin 通道（钉 IP）。所以推送必须转交 gh.py，且要解析它输出的 JSON
   才能知道到底成没成——只看子进程返回码会把「真失败」当成「成功」。

2. **会撞锁**。计划任务每 5 分钟触发一次，而人和 agent 也在提交。
   不排他就会出现 .git/index.lock 冲突，git 报错退出，那一轮改动静默漏掉。
   这里用带过期时间的锁文件：正常退出删锁，异常退出超过 10 分钟的锁视为陈旧、
   自动接管，避免一次崩溃让自动同步永久停摆。

3. **每 5 分钟最容易产出 200 条 "update"**。那种历史等于没有历史。
   所以按改动落点生成前缀（docs/test/feat/fix/chore），
   至少三个月后回看还能知道「这一轮动的是哪一块」。

4. **不能在合并/变基中途提交**。那会把冲突标记当成正常改动提交上去，
   产生的提交既编译不过、也不该出现在历史里。检测到 MERGE_HEAD / rebase
   目录就直接放弃这一轮，等人工处理。

退出码：0 = 已完成（含「无需提交」）｜1 = 有失败｜2 = 被锁占用跳过本轮。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GIT_DIR = REPO / ".git"
LOCK = GIT_DIR / "autosync.lock"
LOG = GIT_DIR / "autosync.log"
GH_SKILL = Path(os.path.expanduser("~")) / ".workbuddy" / "skills" / "github-web-skill"
GH_PY = GH_SKILL / "scripts" / "gh.py"
PYTHON = sys.executable

LOCK_STALE_SECONDS = 600        # 10 分钟：超过就认为上一轮已崩，接管
PUSH_DEADLINE = "150"

# 提交信息前缀的判定顺序：先看最具体的目录，再看兜底。
# 顺序有讲究——一次改动同时动了 docs 和 src 时，应当记成代码改动（feat/fix），
# 而不是 docs：把代码改动标成 docs 会让 changelog 统计失真。
PREFIX_RULES = [
    ("src/quality_trace/core/analytics", "feat(analytics)"),
    ("src/quality_trace/core/io/", "feat(io)"),
    ("src/quality_trace/core/repo/", "feat(repo)"),
    ("src/quality_trace/core/services/", "feat(service)"),
    ("src/quality_trace/api/", "feat(api)"),
    ("src/quality_trace/web/", "feat(web)"),
    ("tests/", "test"),
    ("scripts/", "chore(scripts)"),
    ("docs/", "docs"),
    ("README.md", "docs"),
    ("CHANGELOG.md", "docs"),
]


def log(msg: str) -> None:
    """写轮转日志。落在 .git/ 下，不会被自己提交进去。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (stamp, msg)
    print(line)
    try:
        # 超过 512KB 就截断，保留最近一段即可——这是运维记录，不是业务数据
        if LOG.exists() and LOG.stat().st_size > 512 * 1024:
            tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
            LOG.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(args: list[str], *, timeout: int = 120, cwd: Path | None = None) -> tuple[int, str, str]:
    """跑一个子进程，返回 (返回码, stdout, stderr)。

    统一按 utf-8 + replace 解码：Windows 上 git 的中文报错是 GBK，
    直接按 utf-8 读会在日志里留一堆替换字符，排查时看不出原话。
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")   # 永不弹凭据输入框（计划任务里没人点）
    try:
        p = subprocess.run(
            args, cwd=str(cwd or REPO), capture_output=True, timeout=timeout, env=env,
        )
        return (
            p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip(),
        )
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%ds）" % timeout
    except OSError as exc:
        return 127, "", str(exc)


# --------------------------------------------------------------------- 锁
def acquire_lock() -> bool:
    """拿锁。返回 True 表示本轮可以干活。"""
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            log("跳过：上一轮仍在运行（锁 %.0fs 前创建）" % age)
            return False
        # 陈旧锁：上一轮大概崩了。接管而不是永久阻塞——
        # 一个 5 分钟的自动任务最怕的就是「静默停摆但没人发现」。
        log("接管陈旧锁（%.0fs 前创建，超过 %ds 阈值）" % (age, LOCK_STALE_SECONDS))
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError as exc:
        log("跳过：无法创建锁文件（%s）" % exc)
        return False


def release_lock() -> None:
    try:
        if LOCK.exists():
            LOCK.unlink()
    except OSError:
        pass


# ----------------------------------------------------------------- 判断状态
def in_conflict() -> str | None:
    """处于合并/变基/拣选的中途就返回原因，否则 None。"""
    markers = {
        "MERGE_HEAD": "合并中",
        "CHERRY_PICK_HEAD": "拣选中",
        "REVERT_HEAD": "回滚中",
        "rebase-merge": "变基中",
        "rebase-apply": "变基/打补丁中",
    }
    for name, label in markers.items():
        if (GIT_DIR / name).exists():
            return label
    return None


def changed_paths() -> list[str]:
    """工作区 + 暂存区的全部改动路径（含未跟踪）。"""
    rc, out, err = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if rc != 0:
        log("git status 失败：%s" % (err or out))
        return []
    parts = [p for p in out.split("\0") if p]
    paths: list[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        status, path = entry[:2], entry[3:]
        paths.append(path)
        # 重命名/复制在 -z 下多跟一段原路径，跳过它，否则会被当成一个改动文件
        if status[0] in ("R", "C") and i + 1 < len(parts):
            i += 1
        i += 1
    return paths


def pick_prefix(paths: list[str]) -> str:
    lowered = [p.replace("\\", "/") for p in paths]
    for needle, prefix in PREFIX_RULES:
        if any(p.startswith(needle) for p in lowered):
            return prefix
    return "chore"


def summarize(paths: list[str]) -> str:
    """一句话概括这轮动了什么：按顶层目录计数，最多列 4 组。"""
    buckets: dict[str, int] = {}
    for p in paths:
        norm = p.replace("\\", "/")
        top = norm.split("/")[0] if "/" in norm else norm
        buckets[top] = buckets.get(top, 0) + 1
    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    head = "、".join("%s %d 个" % (k, v) for k, v in ordered[:4])
    if len(ordered) > 4:
        head += " 等"
    return "%d 个文件（%s）" % (len(paths), head)


def behind_ahead() -> tuple[int, int]:
    """(落后, 领先) origin/main 的提交数。取不到远端时按 (0,0) 处理。"""
    rc, out, _ = run(["git", "rev-list", "--left-right", "--count", "origin/main...main"])
    if rc != 0 or not out:
        return 0, 0
    try:
        left, right = out.split()
        return int(left), int(right)
    except ValueError:
        return 0, 0


def head_summary() -> str:
    rc, out, _ = run(["git", "log", "-1", "--pretty=%h %s"])
    return out if rc == 0 else "（取不到）"


# ------------------------------------------------------------------- 推送
def push() -> tuple[bool, str]:
    """经 github-web-skill 的 pin 通道推送，返回 (是否成功, 说明)。

    为什么不用 gh.py 的返回码判成败：gh.py 成功时也可能因为告警返回非 0，
    失败时返回 1 但结构里带 offers。所以解析它 stdout 的 JSON 更可靠——
    那个 JSON 里有明确的 ok 字段和 tried[]，能直接看出走了哪条通道。
    """
    if not GH_PY.exists():
        return False, "找不到 GitHub 加速脚本：%s" % GH_PY
    rc, out, err = run(
        [PYTHON, str(GH_PY), "git", "push", "origin", "main",
         "--cwd", str(REPO), "--deadline", PUSH_DEADLINE],
        timeout=int(PUSH_DEADLINE) + 90,
        cwd=GH_SKILL,
    )
    payload = None
    # gh.py 的 stdout 是单个 JSON，但成功摘要会打一行到 stderr；
    # 这里从第一个 { 开始截，容忍前后夹带的杂讯
    start = out.find("{")
    if start >= 0:
        try:
            payload = json.loads(out[start:])
        except ValueError:
            payload = None

    if payload and payload.get("ok"):
        via = payload.get("channel") or "?"
        return True, "推送成功（通道 %s）%s" % (via, head_summary())

    if payload:
        tried = payload.get("tried") or []
        detail = "；".join(
            "%s:%s" % (t.get("channel"), t.get("detail")) for t in tried if isinstance(t, dict)
        ) or (err or "无详情")
        return False, "推送失败，试过 %s。建议：%s" % (detail, payload.get("next") or "见日志")
    return False, "推送失败（rc=%s）%s" % (rc, (err or out)[:400])


# -------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="工作区改动自动提交并推送到 GitHub")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不提交不推送")
    ap.add_argument("--status", action="store_true", help="查看最近 20 条日志后退出")
    ap.add_argument("--message", help="指定提交信息（不给则自动生成）")
    args = ap.parse_args()

    if args.status:
        if LOG.exists():
            print("\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]))
        else:
            print("暂无记录（%s 不存在）" % LOG)
        return 0

    if not (GIT_DIR / "HEAD").exists():
        log("跳过：%s 不是 git 仓库" % REPO)
        return 1

    conflict = in_conflict()
    if conflict:
        # 冲突中途自动提交会把 <<<<<<< 标记写进历史，且提交必然编译不过
        log("跳过：仓库处于%s，等人工处理完再自动同步" % conflict)
        return 0

    if args.dry_run:
        paths = changed_paths()
        pending = behind_ahead()
        log("干跑：%d 个改动文件｜未推送 %d 个提交｜落后远端 %d 个"
            % (len(paths), pending[1], pending[0]))
        if paths:
            log("将用前缀 %s，摘要：%s" % (pick_prefix(paths), summarize(paths)))
        return 0

    if not acquire_lock():
        return 2
    try:
        paths = changed_paths()
        committed = False

        if paths:
            rc, out, err = run(["git", "add", "-A"], timeout=180)
            if rc != 0:
                log("失败：git add 出错（%s）" % (err or out))
                return 1

            message = args.message or "%s: 自动同步 %s" % (
                pick_prefix(paths), summarize(paths),
            )
            stamp = datetime.now().strftime("%H:%M")
            rc, out, err = run(["git", "commit", "-m", message], timeout=180)
            if rc != 0:
                # 常见于「add 之后内容与 HEAD 相同」（例如只有 .gitignore 生效的空改动）
                if "nothing to commit" in (out + err) or "无文件要提交" in (out + err):
                    log("本轮无需提交（git 判定无实际变化）")
                else:
                    log("失败：git commit 出错（%s）" % (err or out))
                    return 1
            else:
                committed = True
                log("已提交 %s @ %s" % (message, stamp))

        behind, ahead = behind_ahead()
        if ahead == 0 and not committed:
            log("本轮无改动、无待推送提交，结束")
            return 0

        ok, detail = push()
        log(("完成：" if ok else "失败：") + detail)
        return 0 if ok else 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())

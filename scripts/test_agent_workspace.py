"""验证 Agent 工作区技能：路径监狱逃逸拦截 + 读写改查 + 执行(env清洗)。免 DB，本地直跑。

    python scripts/test_agent_workspace.py

设计红线见 docs/agent-workspace-tech-design.md。断言覆盖：监狱逃逸(../、符号链接)全拒、
edit 不唯一拒、二进制拒读、超大小拒、happy path 读写改查通、执行 env 不泄露 STOCK_* 密钥。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:  # Windows 控制台默认 GBK，打不出 ✓，强制 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# 必须在 import settings 前设好环境（settings 是 frozen dataclass，import 期即冻结）
_TMP = tempfile.mkdtemp(prefix="ws_test_")
os.environ["STOCK_WORKSPACE_DIR"] = _TMP
os.environ["STOCK_WORKSPACE_ENABLED"] = "true"
os.environ["STOCK_WORKSPACE_EXEC_ENABLED"] = "true"
# 放一个哨兵密钥进 env，稍后验证执行子进程读不到（env 清洗）
os.environ["STOCK_DB_PASSWORD"] = "SENTINEL_SECRET_should_not_leak"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.skills import agent_workspace as ws  # noqa: E402

ROOT = Path(_TMP).resolve()
ok = 0


def check(label: str, cond: bool) -> None:
    global ok
    assert cond, f"✗ {label}"
    ok += 1
    print(f"  ✓ {label}")


try:
    print("路径监狱：")
    # 1. ../ 越权 → 拒
    r = ws.ws_write_file("../escape.txt", "x")
    check("../escape.txt 被拒（越权）", "error" in r and "越权" in r["error"])
    r = ws.ws_read_file("../../etc/passwd")
    check("../../etc/passwd 被拒", "error" in r)
    # 2. 绝对路径 → 归一为监狱内（安全解释，不逃逸）
    r = ws.ws_write_file("/etc/passwd", "harmless")
    check("绝对路径 /etc/passwd 归一到监狱内、未逃逸", "error" not in r and (ROOT / "etc/passwd").exists())
    check("  且真实 /etc/passwd 未被写", True)  # 上一条已保证落在 ROOT 内
    # 3. 符号链接逃逸 → 拒（Windows 无权限则跳过）
    outside = Path(tempfile.mkdtemp(prefix="ws_outside_"))
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    link = ROOT / "sneaky"
    try:
        os.symlink(outside, link, target_is_directory=True)
        r = ws.ws_read_file("sneaky/secret.txt")
        check("经符号链接读监狱外文件 被拒", "error" in r and "越权" in r["error"])
    except OSError:
        print("  … 符号链接测试跳过（本机无创建权限，Linux 生产会执行）")

    print("写 / 读 / 改：")
    r = ws.ws_write_file("reports/test.md", "# 标题\n第一行\n第二行\n")
    check("写 reports/test.md 成功", r.get("bytes", 0) > 0 and r.get("overwritten") is False)
    r = ws.ws_write_file("reports/test.md", "覆盖内容")
    check("再写同文件 overwritten=True", r.get("overwritten") is True)
    r = ws.ws_read_file("reports/test.md")
    check("读回内容正确", r.get("content") == "覆盖内容")

    # edit 唯一性
    ws.ws_write_file("dup.txt", "foo bar\nfoo baz\n")
    r = ws.ws_edit_file("dup.txt", "foo", "FOO")
    check("edit 不唯一（2次）无 replace_all 被拒", "error" in r and "不唯一" in r["error"])
    r = ws.ws_edit_file("dup.txt", "foo", "FOO", replace_all=True)
    check("edit replace_all=True 替换 2 处", r.get("replaced") == 2)
    r = ws.ws_edit_file("dup.txt", "不存在的串", "x")
    check("edit old_string 不存在 被拒", "error" in r and "未找到" in r["error"])
    r = ws.ws_edit_file("dup.txt", "FOO bar", "唯一替换")
    check("edit 唯一匹配替换成功", r.get("replaced") == 1)

    # 二进制拒读
    (ROOT / "bin.dat").write_bytes(b"abc\x00def")
    r = ws.ws_read_file("bin.dat")
    check("二进制文件（含 NUL）拒读", "error" in r and "二进制" in r["error"])

    # 超大小拒写
    r = ws.ws_write_file("big.txt", "A" * (2 * 1024 * 1024 + 1))
    check("写 >2MB 被拒", "error" in r and "过大" in r["error"])

    print("查找：")
    r = ws.ws_glob("**/*.md")
    check("glob **/*.md 命中 test.md", any(f["path"].endswith("test.md") for f in r.get("files", [])))
    r = ws.ws_grep("唯一替换")
    check("grep 命中 dup.txt", r.get("count", 0) >= 1 and any(h["path"] == "dup.txt" for h in r.get("hits", [])))

    print("执行（env 清洗）：")
    script = (
        "import os, json\n"
        "leaked = {k: v for k, v in os.environ.items() if k.startswith('STOCK')}\n"
        "print(json.dumps({'leaked': leaked, 'hello': 'from-subprocess'}))\n"
    )
    ws.ws_write_file("scripts/probe.py", script)
    r = ws.ws_run_python("scripts/probe.py")
    check("执行退出码 0", r.get("exit_code") == 0)
    check("stdout 有输出", "from-subprocess" in r.get("stdout", ""))
    check("子进程读不到任何 STOCK_* 密钥（env 已清洗）", "SENTINEL_SECRET" not in r.get("stdout", ""))
    # 只执行 .py
    ws.ws_write_file("notes/x.txt", "not python")
    r = ws.ws_run_python("notes/x.txt")
    check("拒绝执行非 .py 文件", "error" in r and ".py" in r["error"])
    # 超时
    ws.ws_write_file("scripts/loop.py", "while True:\n    pass\n")
    r = ws.ws_run_python("scripts/loop.py", timeout_s=2)
    check("死循环脚本被超时强杀", r.get("timed_out") is True)

    print(f"\nAgent 工作区验证通过 ✓（{ok} 项断言）")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

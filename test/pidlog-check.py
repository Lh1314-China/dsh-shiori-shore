# 自测 shore_server._record_process_identity：字段、追加、异常分支（不启动 MCP）
# 跑法：python test/pidlog-check.py
import json
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_PY = str(Path(__file__).resolve().parent.parent / "python")
sys.path.insert(0, PLUGIN_PY)

import shore_server  # noqa: E402

home = Path(tempfile.mkdtemp(prefix="shore-pidtest-"))
os.environ["BILI_DUTY_HOME"] = str(home)
shore_server._record_process_identity()
shore_server._record_process_identity()

path = home / "python-pid.log"
lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
recs = [json.loads(ln) for ln in lines]

need = {"ts", "pid", "ppid", "python", "sys_executable", "real_image", "cwd", "server"}
ok = True
print(f"lines={len(lines)}")
if len(lines) != 2:
    ok = False
    print("FAIL: 期望追加 2 行")
for r in recs:
    if not need <= set(r):
        ok = False
        print("FAIL: 缺字段", need - set(r))
    if r["pid"] != os.getpid():
        ok = False
        print("FAIL: pid 不是本进程", r["pid"], os.getpid())
    if not r["server"].endswith("shore_server.py"):
        ok = False
        print("FAIL: server 路径不对", r["server"])
    # 字段语义：sys_executable 可能被改写；real_image 必须非空且是绝对路径的 exe
    if not r.get("sys_executable"):
        ok = False
        print("FAIL: sys_executable 为空")
    if not str(r.get("real_image", "")).lower().endswith(".exe"):
        ok = False
        print("FAIL: real_image 不是 exe 路径", r.get("real_image"))
    if "executable" in r and "sys_executable" not in r:
        ok = False
        print("FAIL: 旧字段名 executable 还在")
print("sample=", json.dumps(recs[-1], ensure_ascii=False))

# 异常分支：BILI_DUTY_HOME 指向一个已存在的文件 → mkdir 会炸，但必须被吞掉
bad = home / "not-a-dir"
bad.write_text("x", encoding="utf-8")
os.environ["BILI_DUTY_HOME"] = str(bad)
try:
    shore_server._record_process_identity()
    print("ok  : 异常分支没有抛出")
except Exception as e:  # pragma: no cover
    ok = False
    print("FAIL: 异常分支抛了", type(e).__name__, e)

print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

"""测试运行器回归：真实子进程超时、后续脚本继续执行以及异常退出识别，不访问网络。"""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("offline_runner", ROOT / "tests/run_all.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

passed = failed = 0


def check(name, ok):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✔ {name}")
    else:
        failed += 1
        print(f"  ✘ {name}")


with tempfile.TemporaryDirectory(prefix="bili_runner_") as tmp:
    root = Path(tmp)
    (root / "hang.py").write_text("import time\nprint('entered hang', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    (root / "ok.py").write_text("print('2 项通过, 0 项失败')\n", encoding="utf-8")
    (root / "bad.py").write_text("raise RuntimeError('offline failure')\n", encoding="utf-8")
    output = io.StringIO()
    with patch.multiple(runner, ROOT=root, PY=sys.executable, SCRIPTS=["hang.py", "ok.py", "bad.py"],
                        OFFLINE_TIMEOUT_SECONDS=3), redirect_stdout(output):
        p, f, bad = runner.run_offline()
    check("超时终止挂起脚本并记录最后的用例进度", "hang.py" in bad and "entered hang" in output.getvalue() and f == 1)
    check("一个脚本超时后仍执行后续成功用例", p == 2)
    check("无汇总的异常退出同样使整轮失败", bad == ["hang.py", "bad.py"] and "offline failure" in output.getvalue())

print(f"==== 测试运行器: {passed} 项通过, {failed} 项失败 ====")
raise SystemExit(int(failed > 0))

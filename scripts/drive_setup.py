"""用真实 CLI 驱动 setup 各步骤——黑盒复现/回归交互配置 bug。

为什么不直接调 setup_wizard.step_*：那等于绕过菜单、绕过 tui、绕过了 stdin 解码，
而这几层恰恰是 bug 的栖息地。这里全程走 `cli/hy_mem_cli.py` 子进程，键盘输入
用管道喂（非 TTY 时 tui 会降级成编号菜单，正好可脚本化）。

密钥只从环境变量读（Windows 上环境变量没进当前 shell 时，回落到用户级注册表），
命令行与输出里都不出现明文。

用法：
  .venv/Scripts/python.exe scripts/drive_setup.py <沙箱目录> <step> "<按键序列>"
例：
  .venv/Scripts/python.exe scripts/drive_setup.py /d/tools_open/tmp-401 embedding "1\\n\\n"
  .venv/Scripts/python.exe scripts/drive_setup.py /d/tools_open/tmp-401 all "1\\n2\\n1\\n\\n1\\n\\n"
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env_with_keys() -> dict:
    env = dict(os.environ)
    try:
        import winreg  # 只有 Windows 有；环境变量够用时不该走这里
    except ImportError:
        return env
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        for name in ("MEMORY_EMBEDDER_API_KEY", "MEMORY_LLM_API_KEY"):
            try:
                env.setdefault(name, winreg.QueryValueEx(k, name)[0])
            except FileNotFoundError:
                pass
    except OSError:
        pass
    return env


def make_sandbox(sb: Path) -> Path:
    """沙箱配置：拷真实 config.json，只把 dataDir 指到沙箱，apiKey 保持仓库里的样子。"""
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    (sb / "data").mkdir(parents=True, exist_ok=True)
    cfg.setdefault("storage", {})["dataDir"] = str(sb / "data")
    p = sb / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def mask(text: str) -> str:
    """把可能漏进输出的长 token 抹掉，只留前后 6/4 位。"""
    return re.sub(r"(sk-[A-Za-z0-9_\-]{20,})", lambda m: m.group(1)[:9] + "…" + m.group(1)[-4:], text)


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    sb = Path(sys.argv[1]).resolve()
    step = sys.argv[2]
    keys = sys.argv[3].encode().decode("unicode_escape")

    sb.mkdir(parents=True, exist_ok=True)
    cfg_path = make_sandbox(sb)
    env = env_with_keys()
    env["HY_MEM_CONFIG"] = str(cfg_path)
    env["MEMORY_DATA_DIR"] = str(Path(cfg_path).parent / "data")

    cmd = [sys.executable, str(ROOT / "cli" / "hy_mem_cli.py"), "setup", "--step", step]
    print(f"$ hy-mem setup --step {step}   (stdin={keys!r})")
    proc = subprocess.run(cmd, input=keys, capture_output=True, text=True, env=env, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    print(mask(out).strip())
    print(f"\n[rc={proc.returncode}]")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    emb = cfg.get("embedding") or {}
    print("[config 结果] apiKey 长度:", len(emb.get("apiKey") or ""),
          "| keyEnv:", emb.get("keyEnv"), "| dims:", emb.get("dims"), "| model:", emb.get("model"))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

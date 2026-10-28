"""stdin 编码链路测试（2026-10-28 线上事故的回归闸门）.

事故现场：TUI「写记忆」输入中文后报

    hy-mem: UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcaa'
            in position 17: surrogates not allowed（密钥或地址里含非 ASCII 字符…）

提示指向密钥，实际是**冻结进程的 stdin 用 cp936 + surrogateescape 解码**，把
上游的 UTF-8 字节切出了孤立代理项，代理项一路活到建 HTTP 请求体时才炸。

本测试不碰网络、不碰向量库，只测三件事：
1. `_decode_stdin` 对 UTF-8 / GBK / BOM / 二进制垃圾各自给出正确结果；
2. `_strip_surrogates` 能把孤立代理项清干净（这是最后一道闸）；
3. 在**复刻的事故环境**（PYTHONIOENCODING=gbk:surrogateescape）下跑一次
   `_read_text`，内容必须逐字正确 —— 这一条如果回退，事故就会重现。

跑法：.venv/Scripts/python.exe scripts/test_encoding.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

import hy_mem_cli  # noqa: E402

CJK = "刘德华是个歌星，香港歌手。"


class DecodeStdinTest(unittest.TestCase):
    def test_utf8_pipe(self):
        text, enc = hy_mem_cli._decode_stdin(CJK.encode("utf-8"))
        self.assertEqual(text, CJK)
        self.assertEqual(enc, "utf-8")

    def test_utf8_with_bom(self):
        text, enc = hy_mem_cli._decode_stdin(b"\xef\xbb\xbf" + CJK.encode("utf-8"))
        self.assertEqual(text, CJK)
        self.assertEqual(enc, "utf-8-sig")

    def test_gbk_pipe(self):
        # cmd.exe 里的老习惯：`type note.txt | hy-mem add`
        text, enc = hy_mem_cli._decode_stdin(CJK.encode("gbk"))
        self.assertEqual(text, CJK)
        self.assertEqual(enc, "gbk")

    def test_utf16_with_bom(self):
        text, enc = hy_mem_cli._decode_stdin(CJK.encode("utf-16"))
        self.assertEqual(text, CJK)
        self.assertEqual(enc, "utf-16")

    def test_binary_garbage_never_raises(self):
        text, _enc = hy_mem_cli._decode_stdin(bytes(range(256)))
        self.assertIsInstance(text, str)  # 不抛就够了：宁可乱码也不能崩

    def test_empty(self):
        self.assertEqual(hy_mem_cli._decode_stdin(b""), ("", "utf-8"))


class StripSurrogatesTest(unittest.TestCase):
    def test_removes_lone_surrogates(self):
        # 这正是事故里的形态：cp936 切完剩下的零头被 surrogateescape 记成 \udcaa
        dirty = "刘德华是\udcaa个歌星\udca6"
        clean, n = hy_mem_cli._strip_surrogates(dirty)
        self.assertEqual(clean, "刘德华是个歌星")
        self.assertEqual(n, 2)

    def test_clean_text_untouched(self):
        clean, n = hy_mem_cli._strip_surrogates(CJK)
        self.assertEqual(clean, CJK)
        self.assertEqual(n, 0)

    def test_clean_text_is_encodable(self):
        # 闸门的真正目的：结果必须能 utf-8 编码（否则照样在请求体上炸）
        clean, _ = hy_mem_cli._strip_surrogates("a\ud800b")
        clean.encode("utf-8")


class ReadTextInAccidentEnvTest(unittest.TestCase):
    """在冻结进程的真实编码环境下读一次 stdin。

    为什么要开子进程：`sys.stdin` 的编码在进程启动时就定了，没法在本进程里
    改造成 "cp936 + surrogateescape"。用 PYTHONIOENCODING 复刻同样的配置，
    这是事故现场的关键条件，缺了它就测不出问题。
    """

    def test_frozen_like_stdin(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk:surrogateescape"
        env["PYTHONPATH"] = str(ROOT / "cli") + os.pathsep + env.get("PYTHONPATH", "")
        code = (
            "import sys, hy_mem_cli;"
            "t = hy_mem_cli._read_text(None);"
            "sys.stdout.buffer.write(t.encode('utf-8'))"
        )
        p = subprocess.run(
            [sys.executable, "-c", code],
            input=CJK.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(out, CJK, f"内容被解码坏了：{out!r}")
        # 事故的指纹就是这行报错，出现即回退
        self.assertNotIn("surrogates not allowed", p.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

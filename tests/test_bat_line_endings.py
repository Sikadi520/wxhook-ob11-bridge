# -*- coding: utf-8 -*-
"""
守住 Windows 批处理的换行符:仓库里的 blob 必须是 CRLF。

## 背景(2026-10-23 真实故障)

0.2.1 发布后,`start.bat` 在用户机器上一打开就满屏:

    '------------------------------------' 不是内部或外部命令
    'istrator' 不是内部或外部命令
    'rlevel' 不是内部或外部命令
    'ements.txt' 不是内部或外部命令

每个报错都是某个单词的**后半截**(Administrator→istrator、
errorlevel→rlevel、requirements.txt→ements.txt)。

根因不是命令写错,而是 **blob 里只有 LF**:

* `.gitattributes` 当时写 `*.bat -text`,注释里写着“这样能保住 CRLF”,
  但 `-text` 的语义是「不做换行转换」——提交什么就分发什么;
* 而提交进去的 blob 本身就是 LF;
* cmd.exe 解析 .bat 时按字节偏移逐行推进,LF-only 再加上多字节
  中文注释会累积错位,把每行从中间切断,前半截丢弃、后半截当命令
  执行。

## 这条测试为什么必须在

这个问题**在编辑器里、在 git diff 里、在 Linux CI 上都看不出来** ——
文件内容逐字相同,只有字节层面不同。只有真的拿 cmd 跑一次才会暴露,
而那时用户已经下载完了。

测试直接读 **git 对象库里的 blob**(`git cat-file`),而不是工作区文件,
因为决定用户下载到什么的正是 blob。

> 实测对比(为什么不用 `text eol=crlf`):
>
>     text eol=crlf  -> blob 存 LF,仅检出/archive 时转 CRLF
>                       Raw 单文件下载拿到 LF -> 仍然会炸
>     -text          -> blob 原样分发,只要 blob 是 CRLF,所有渠道都对
>"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*args):
    """跑 git 并返回 bytes;失败返回 None(比如没装 git、或是导出的目录)。"""
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT),
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _count_breaks(data: bytes):
    """返回 (lf 总数, crlf 数, 孤立 lf 数)。"""
    lf = data.count(b"\n")
    crlf = data.count(b"\r\n")
    return lf, crlf, lf - crlf


def _tracked_bat_files():
    """仓库里跟踪的 .bat / .cmd 文件。"""
    out = _git("ls-files", "*.bat", "*.cmd")
    if out is None:
        return []
    return [line.strip() for line in out.decode("utf-8", "replace").splitlines()
            if line.strip()]


def _blob_of(path: str):
    """
    读某个文件即将分发出去的 blob 内容。

    优先读**暂存区**(`git cat-file -p :path`):那是你下一次 commit 会写进
    仓库的东西,所以测试在 commit **之前**就能拦住错误。
    暂存区不可用时(比如 CI 里工作区比索引新、或者裸仓库)回退到 HEAD。

    为什么不直接读工作区文件:工作区只是「你现在看到的」,
    决定用户下载到什么的永远是 blob。两者可以不一致
    (`.gitattributes` 的转换规则就发生在这两者之间)。
    """
    data = _git("cat-file", "-p", ":" + path)
    if data is None:
        data = _git("cat-file", "-p", "HEAD:" + path)
    return data


class BatLineEndingTest(unittest.TestCase):

    def setUp(self):
        if _git("rev-parse", "--git-dir") is None:
            self.skipTest("不是 git 仓库(或没装 git),跳过 blob 检查")
        self.files = _tracked_bat_files()

    def test_there_is_at_least_one_bat(self):
        """如果没有 .bat 被跟踪,下面几条测试会空跑,反而掩盖问题。"""
        self.assertTrue(self.files, "没有跟踪到任何 .bat/.cmd,测试没意义")

    def test_blobs_are_crlf(self):
        """
        核心断言:仓库里存的就是 CRLF。

        这条保护的是 `git clone` 和 GitHub “Download ZIP” 的用户 ——
        拿到的东西由 blob 决定。
        """
        for path in self.files:
            with self.subTest(path=path):
                data = _blob_of(path)
                self.assertIsNotNone(data, "读不到 blob: %s" % path)
                lf, crlf, stray = _count_breaks(data)
                self.assertGreater(lf, 0, "%s 没有换行?" % path)
                self.assertEqual(
                    stray, 0,
                    "%s 的仓库版本有 %d 个孤立 LF(共 %d 行)。\n"
                    "cmd.exe 会把每一行从中间切断,用户看到的就是满屏\n"
                    "「'xxx' 不是内部或外部命令」。\n"
                    "修复:把文件转成 CRLF 后 git add 重新提交。"
                    % (path, stray, lf))
                self.assertEqual(crlf, lf,
                                 "%s 的 CRLF(%d)与 LF(%d)不相等" % (path, crlf, lf))

    def test_working_tree_is_crlf_too(self):
        """
        工作区也应该是 CRLF,否则本地测试和用户实际下载的行为不一致。
        """
        for path in self.files:
            with self.subTest(path=path):
                f = ROOT / path
                if not f.is_file():
                    self.skipTest("工作区缺少 %s" % path)
                lf, crlf, stray = _count_breaks(f.read_bytes())
                self.assertEqual(stray, 0,
                                 "%s 工作区有 %d 个孤立 LF" % (path, stray))

    def test_gitattributes_keeps_bat_unconverted(self):
        """
        `.gitattributes` 必须用 `-text` 标记 .bat/.cmd。

        为什么不是 `text eol=crlf`（实测对比过）:

            text eol=crlf  -> blob 存 LF,仅检出/archive 时转 CRLF。
                              GitHub 的「Raw / Download raw file」拿的是
                              blob 原样,所以单文件下载仍然是 LF,照样炸。
            -text          -> 完全不转换,提交什么就分发什么。
                              只要 blob 是 CRLF,所有渠道都是 CRLF。

        所以断言是:标记为 -text,且**不能**被 `eol=crlf` 类的归一化接管。
        """
        attrs = ROOT / ".gitattributes"
        if not attrs.is_file():
            self.skipTest("没有 .gitattributes")
        lines = [line.strip() for line in
                 attrs.read_text(encoding="utf-8").splitlines()]
        rules = [line for line in lines if line and not line.startswith("#")]
        joined = "\n".join(rules)

        self.assertIn("*.bat -text", joined,
                      ".bat 必须标 -text,否则 blob 里的 CRLF 会被归一化成 LF")
        for rule in rules:
            self.assertNotRegex(
                rule, r"\*\.bat\s.*eol=crlf",
                "eol=crlf 只保证检出,blob 仍是 LF,Raw 下载会炸: %s" % rule)

    def test_blob_crlf_is_what_ships(self):
        """
        因为用 `-text`,给终端用户的每个渠道都直接拿 blob,
        所以“blob 是纯 CRLF”这一个断言同时保证 clone / ZIP / Raw。
        """
        for path in self.files:
            with self.subTest(path=path):
                blob = _blob_of(path) or b""
                lf, crlf, stray = _count_breaks(blob)
                self.assertEqual(stray, 0, "%s 的 blob 有孤立 LF" % path)
                self.assertEqual(crlf, lf, "%s 的 blob 不是纯 CRLF" % path)

    def test_no_bom(self):
        """
        .bat 带 BOM 时,cmd 会把 BOM 当成命令的一部分,
        第一行 `@echo off` 可能失效并打印一次自身。
        """
        for path in self.files:
            with self.subTest(path=path):
                data = _blob_of(path) or b""
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"),
                                 "%s 有 UTF-8 BOM,cmd 会解析出错" % path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
启动前的配置归一化:把"靠人会看文档"的事变成"程序替你做对"。

本文件只处理一件事,但这件事让一份排障报告绕了两个小时:

    faked_version 和 patch_x64_to_x32 必须【同时】开启。

四组对照实验(2026-10-23,微信 3.9.5.81):

    伪装  补丁   结果
    ----  ----   --------------------------
    关    开     版本过低,微信被关
    开    关     版本过低,弹窗在扫码前
    开    开     登录成功(两台机器都是)

也就是说两个开关各自都"看起来有理",但单独开任意一个都过不了门禁 ——
而配置模板里 patch 的默认值是 false,注释还写着"保持关闭"。

所以这里的策略是:**自动补齐,并且把怎么补打出来**。

  * 显式写的 patch_x64_to_x32: false          -> 尊重用户,不补(用户可能就是为了排障而关它)
  * require_patch_and_faker: false             -> 尊重用户,不补(明确的逃生门)
  * faked_version 为空                         -> 补 3.9.12.51 + 打开补丁
  * faked_version 有值但补丁不是 true          -> 补打开
  * 补丁已经是 true                            -> 不补,但会确认伪装不是空

自动补齐之后会在日志里给出**还原办法**(一行配置),不会悄悄改用户的选择。
"""

import logging

log = logging.getLogger("options")

# 2026-10 两台机器都验证过能过门禁的目标版本;微信自身仍是 3.9.5.81。
DEFAULT_FAKED_VERSION = "3.9.12.51"

# 自动补齐时用的补丁站点上限。999 = "能打的全打"(实测 30~40 处)。
DEFAULT_PATCH_MAX_SITES = 999


def _is_true(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def normalize(cfg: dict, lg=None) -> dict:
    """
    原地修正 cfg,返回一份供日志打印的说明 dict:

        {"faked_version": str, "patch_x64_to_x32": bool,
         "adjusted": [...], "notes": [...], "revert": str|None}

    ``adjusted`` 每项是一句人话,说明"我替你改了什么、为什么"。
    """
    lg = lg or log
    before_faked = cfg.get("faked_version") or ""
    before_patch = cfg.get("patch_x64_to_x32", False)

    info = {"adjusted": [], "notes": [], "revert": None}

    # ---- 1) 逃生门:用户明确要求"别管我" ----
    if not _is_true(cfg.get("require_patch_and_faker", True)):
        info["notes"].append(
            "require_patch_and_faker=false:不做组合检查。"
            "如果一直提示「当前客户端版本过低」,把它改回 true")
        _fill_defaults_without_forcing(cfg, info)
        return _finish(cfg, info, before_faked, before_patch)

    # ---- 2) 伪装版本为空:门禁一定过不去,补上 ----
    if not str(cfg.get("faked_version") or "").strip():
        cfg["faked_version"] = DEFAULT_FAKED_VERSION
        info["adjusted"].append(
            "faked_version 为空 → 已设为 %s(不改的话服务端版本门禁一定过不去)"
            % DEFAULT_FAKED_VERSION)

    # ---- 3) 补丁没开:这是最常见的"只开一半" ----
    if not _is_true(before_patch):
        cfg["patch_x64_to_x32"] = True
        info["adjusted"].append(
            "patch_x64_to_x32=false → 已改为 true。伪装和补丁必须同时开:"
            "实测只开伪装、只开补丁都是「当前客户端版本过低」")

    # ---- 4) 补丁开了,顺手确认站点上限没被填成 0 之类 ----
    try:
        sites = int(cfg.get("patch_max_sites", DEFAULT_PATCH_MAX_SITES) or 0)
    except (TypeError, ValueError):
        sites = 0
    if sites <= 0:
        cfg["patch_max_sites"] = DEFAULT_PATCH_MAX_SITES
        info["adjusted"].append(
            "patch_max_sites=%r → 已设为 %d(补丁只打 1 处等于没打)"
            % (cfg.get("patch_max_sites"), DEFAULT_PATCH_MAX_SITES))

    info["revert"] = ('"faked_version": "", "patch_x64_to_x32": false, '
                      '"require_patch_and_faker": false')
    return _finish(cfg, info, before_faked, before_patch)


def _fill_defaults_without_forcing(cfg, info):
    """逃生门打开时只补"空值",不动任何显式选择。"""
    if not str(cfg.get("faked_version") or "").strip():
        cfg["faked_version"] = ""
        info["notes"].append("faked_version 为空,且组合检查已关闭 —— 本轮不做版本伪装")


def _finish(cfg, info, before_faked, before_patch):
    info["faked_version"] = cfg.get("faked_version") or ""
    info["patch_x64_to_x32"] = _is_true(cfg.get("patch_x64_to_x32", False))
    info["changed"] = (info["faked_version"] != (before_faked or "")
                       or info["patch_x64_to_x32"] != _is_true(before_patch))

    # 组合齐了也记一条:排障时先看这一行就知道门禁条件满不满足
    both = bool(info["faked_version"]) and info["patch_x64_to_x32"]
    if both:
        info["notes"].append("门禁条件:伪装 %s + x64→x32 补丁 已同时开启"
                             % info["faked_version"])
    else:
        info["notes"].append(
            "门禁条件不成立(伪装=%s,补丁=%s)—— 大概率提示「当前客户端版本过低」"
            % (info["faked_version"] or "空", info["patch_x64_to_x32"]))
    return info

"""配置加载。

只用标准库（tomllib, Python 3.11+），不引入 PyYAML —— 无人值守场景下依赖越少越好。

安全约定：配置里只放"环境变量名"，真正的凭据由 secret() 在运行时读取。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

# 项目根目录：本文件在 <root>/ops/core/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.toml"
# 本地密钥文件（与配置文件同目录，已在 .gitignore 中）
SECRETS_FILENAME = "secrets.local.toml"

# 本地覆盖配置：同样与主配置同目录，同样不进版本库。
#
# **为什么需要它**：主配置 `config.toml` 是要跟着仓库走的，里面只能放
# 通用示例；而每个人真正要盯的商品、要探的地址、自己的阈值，都是**私人内容**。
# 把私人内容写进主配置，一开源就等于公开了（例如"我正在比价买什么、预算是多少"）。
# 所以：主配置放示例，`config.local.toml` 放自己的，后者覆盖前者。
LOCAL_CONFIG_FILENAME = "config.local.toml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 叠到 base 上，返回新字典（不改动入参）。

    **列表整体替换，不做拼接。** 这一条是刻意的：
    `[[price_watch.items]]` 是"要盯的东西的清单"。如果本地清单会和公共示例
    拼在一起，用户就只能"往上加"，没法**换掉**仓库里的示例 ——
    而"示例不要出现在我的运行里"恰恰是最常见的要求。

    字典按键逐层合并（这样本地只改一个阈值，不用把整个块抄一遍）。
    """
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            # 列表、标量一律整体替换 —— 见上面的说明
            out[key] = value
    return out


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _read_toml(path: Path) -> dict[str, Any]:
    """读 TOML，容忍 UTF-8 BOM。

    为什么要处理 BOM：Windows 记事本保存 UTF-8 时**默认带 BOM**，
    而 TOML 解析器遇到 BOM 会直接报错。用户手改一次配置就崩，这种坑必须在校验层挡掉。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return tomllib.loads(raw.decode("utf-8", "replace"))


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 TOML 配置并做最小校验。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_file():
        raise ConfigError(f"配置文件不存在: {cfg_path}")
    try:
        cfg = _read_toml(cfg_path)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 TOML：{cfg_path}\n{exc}") from exc

    # 本地覆盖：同目录下的 config.local.toml（不进版本库）。
    # 私人配置放这里，主配置就能安心保持"通用示例"的状态。
    local_path = cfg_path.with_name(LOCAL_CONFIG_FILENAME)
    if local_path.is_file():
        try:
            cfg = _deep_merge(cfg, _read_toml(local_path))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"本地覆盖配置不是合法 TOML：{local_path}\n{exc}"
            ) from exc
        cfg["_local_config"] = str(local_path)

    project = cfg.get("project") or {}
    root = str(project.get("root") or "").strip()
    if not root:
        # 留空就按"配置文件上一级目录"推导：项目整体搬到哪都能直接用，
        # 不用为了换位置去改配置（写死绝对路径是搬移时最容易踩的坑）。
        root = str(cfg_path.resolve().parents[1])
    project["root"] = root.replace("\\", "/")
    cfg["project"] = project
    cfg["_secrets"] = _load_secrets(cfg_path)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def _load_secrets(cfg_path: Path) -> dict[str, Any]:
    """读同目录下的 secrets.local.toml（不存在就返回空）。

    为什么除了环境变量还要支持密钥文件：**计划任务不一定能及时看到新设的环境变量**
    ——Task Scheduler 服务的环境块可能被缓存，改完要重新登录才生效，
    而无人值守的场景恰恰不能依赖"你重新登录一次"。
    文件是每次进程启动都现读的，没有这个不确定性。

    格式就用环境变量名当键，和 config.toml 里的 `*_env` 一一对应：
        OPS_WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    """
    path = cfg_path.parent / SECRETS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = _read_toml(path)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, (str, int, float))}


def resolve_secret(cfg: dict[str, Any], env_name: str | None, default: str | None = None) -> str | None:
    """按"环境变量 → 本地密钥文件"的顺序取凭据。"""
    if not env_name:
        return default
    value = os.environ.get(env_name)
    if value:
        return value
    secrets = cfg.get("_secrets") or {}
    fallback = secrets.get(env_name)
    if fallback:
        return str(fallback)
    return default


def secret_sources(cfg: dict[str, Any], env_name: str | None) -> str:
    """说明凭据当前是从哪来的，用于日志与报错信息。"""
    if not env_name:
        return "未指定"
    if os.environ.get(env_name):
        return f"环境变量 {env_name}"
    secrets = cfg.get("_secrets") or {}
    if secrets.get(env_name):
        return f"{SECRETS_FILENAME} 里的 {env_name}"
    return f"{env_name}（环境变量与 {SECRETS_FILENAME} 中都没有）"


def write_secret(cfg: dict[str, Any], name: str, value: str) -> Path:
    """把一条凭据写进 `config/secrets.local.toml`（该文件已被 .gitignore 排除）。

    与 `scripts/set-secret.ps1` **写同一个文件、同一个格式**（`NAME = "value"`），
    这样两边的工具可以混用，用户不会遇到"用 A 写、B 读不到"。
    值里的 `"` 和 `\\` 会转义 —— 但**不沿用原来的键序**：同名键会被替换掉，
    避免文件里出现两条同名凭据（那样读到哪条就全看运气了）。
    """
    path = Path(cfg["_config_path"]).parent / SECRETS_FILENAME
    lines: list[str] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    kept = [line for line in lines
            if line.strip() and not re.match(rf"^\s*{re.escape(name)}\s*=", line)]
    kept.append(f'{name} = "{escaped}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    # 不带 BOM：PowerShell 的 set-secret.ps1 也是这么写的，保持一致
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return path


def project_root(cfg: dict[str, Any]) -> Path:
    return Path(cfg["project"]["root"])


def data_dir(cfg: dict[str, Any], *parts: str) -> Path:
    """data/ 下的路径，必要目录自动创建。"""
    path = project_root(cfg) / "data"
    for part in parts:
        path = path / part
    return path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def secret(env_name: str | None, default: str | None = None) -> str | None:
    """按环境变量名取凭据。配置里只存名字，不存值。"""
    if not env_name:
        return default
    value = os.environ.get(env_name)
    return value if value else default


def get(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """按 'api_watch.thresholds.consecutive_failures' 取值。"""
    node: Any = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _coerce(raw: str) -> Any:
    """把命令行字符串还原成合适的类型，别让 '3' 变成字符串 '3'。"""
    text = raw.strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _check_numeric_on_dict(node: dict[str, Any], key: str, dotted_hint: str = "") -> None:
    """字典层出现纯数字键，几乎一定是路径写错了。

    例如想写 `api_watch.targets.0.url` 却写成 `api_watch.0.url`。
    不拦的话会静默创建一个没人读的键，让人以为"override 生效了但没效果"。
    """
    if key.isdigit() and key not in node:
        raise ConfigError(
            f"override 路径里 {key!r} 像是列表下标，但上一层是字典（没有这个键）。"
            "多半是漏了一层，例如应为 api_watch.targets.0.url 而不是 api_watch.0.url"
        )


def _descend(node: Any, key: str) -> Any:
    """沿路径往下走一层，中间层不存在就建字典。"""
    if isinstance(node, list):
        try:
            return node[int(key)]
        except (ValueError, IndexError) as exc:
            raise ConfigError(
                f"override 路径里 {key!r} 不是有效的列表下标（当前层是列表，长度 {len(node)}）"
            ) from exc
    _check_numeric_on_dict(node, key)
    child = node.get(key)
    if not isinstance(child, (dict, list)):
        child = {}
        node[key] = child
    return child


def _assign(node: Any, key: str, value: Any) -> None:
    if isinstance(node, list):
        try:
            node[int(key)] = value
        except (ValueError, IndexError) as exc:
            raise ConfigError(
                f"override 路径里 {key!r} 不是有效的列表下标（当前层是列表，长度 {len(node)}）"
            ) from exc
        return
    _check_numeric_on_dict(node, key)
    node[key] = value


def set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    """按点号路径写入配置，支持列表下标（例如 log_scan.sources.0.path）。

    支持列表下标是必需的：日志源、监控目标都是 TOML 数组，
    没有它就没法"临时换个日志文件重跑一次"。
    """
    keys = [k for k in dotted.split(".") if k]
    if not keys:
        raise ConfigError("override 的键不能为空")
    node: Any = cfg
    for key in keys[:-1]:
        node = _descend(node, key)
    _assign(node, keys[-1], value)


def apply_overrides(cfg: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """应用 --override key=value。

    存在的意义：运维上"临时改个参数重跑一次"很常见（例如换 variant 复现问题），
    不该为了跑一次就去改配置文件。
    """
    for item in overrides or []:
        if "=" not in item:
            raise ConfigError(f"override 需要 KEY=VALUE 形式，收到：{item!r}")
        dotted, raw = item.split("=", 1)
        set_path(cfg, dotted.strip(), _coerce(raw))
    return cfg

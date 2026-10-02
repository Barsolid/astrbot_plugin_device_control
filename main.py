# -*- coding: utf-8 -*-
"""astrbot_plugin_device_control

让机器人控制周边的设备（例如打印机），并且遵循「先询问、后执行」的安全流程：

1. 用户用自然语言提出需求，或使用 /print、/say 等指令；
2. 插件不会立刻执行，而是登记一个「待确认操作」并向用户发出确认询问；
3. 用户回复「确认」后，才会真正调用设备完成需求；回复「取消」则放弃。

设计目标：结构清晰、易于调试与二次开发。
- 设备通过 DeviceBase 抽象，新增设备只需实现 execute() 并注册即可。
- 所有配置项见 _conf_schema.json，可在 AstrBot WebUI 中直接修改。
- 打开 dry_run 后不会真正操作硬件，便于调试。

作者: A3uracY
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 视为「确认」的回复（不区分大小写，会做 strip）
CONFIRM_WORDS = {
    "确认", "确定", "是", "好", "好的", "可以", "执行", "同意", "打印",
    "yes", "y", "ok", "okay", "confirm",
}

#: 视为「取消」的回复
CANCEL_WORDS = {
    "取消", "否", "不", "不用", "不要", "算了", "放弃", "停止", "别",
    "no", "n", "cancel", "abort",
}

#: 注入给模型的系统提示：让模型自己判断是否需要使用周边设备，并先征求确认。
DEVICE_HINT = (
    "\n\n【周边设备能力】你连接了现实世界中的周边设备（如打印机、扬声器）。"
    "当用户希望把内容输出到现实世界时（例如让你写一封信/便签/清单并打印出来、"
    "把某段文字打印成纸、把某段文字用扬声器念出来），你应当调用 device_request 工具"
    "来登记该操作。该工具不会立即执行，而是会先向用户发出确认询问；"
    "只有用户确认后才会真正执行。请先用 device_list 了解可用设备与动作。"
    "在用户确认之前，绝不要声称操作已经完成，也不要重复调用工具。"
)


# ---------------------------------------------------------------------------
# 待确认操作
# ---------------------------------------------------------------------------
@dataclass
class PendingAction:
    """一条等待用户确认的设备操作。"""

    device: str
    action: str
    params: dict
    summary: str
    created_at: float
    timeout: int

    def expired(self) -> bool:
        return time.time() - self.created_at > self.timeout


# ---------------------------------------------------------------------------
# 设备抽象
# ---------------------------------------------------------------------------
class DeviceBase:
    """设备基类。新增设备只需继承并实现 execute()。"""

    name: str = ""
    description: str = ""
    #: {action: {"description": str, "params": {参数名: 说明}}}
    actions: dict[str, dict] = {}

    def __init__(self, plugin: "DeviceControlPlugin"):
        self.plugin = plugin

    def action_names(self) -> list[str]:
        return list(self.actions.keys())

    async def execute(self, action: str, params: dict) -> str:
        """执行动作，返回给用户的结果文本。"""
        raise NotImplementedError


class PrinterDevice(DeviceBase):
    """打印机设备：支持打印文本和本地文件（Windows）。"""

    name = "printer"
    description = "打印机，可打印一段文本或一个本地文件"
    actions = {
        "print_text": {
            "description": "打印一段文本",
            "params": {"text": "要打印的文本内容"},
        },
        "print_file": {
            "description": "打印一个本地文件（txt/pdf/图片等）",
            "params": {"path": "文件的绝对路径"},
        },
    }

    async def execute(self, action: str, params: dict) -> str:
        if action == "print_text":
            text = str(params.get("text", ""))
            if not text.strip():
                return "❌ 打印失败：文本内容为空。"
            max_chars = int(self.plugin.config.get("max_print_chars", 5000))
            if len(text) > max_chars:
                return f"❌ 文本过长（{len(text)} 字），上限为 {max_chars} 字。"
            return await self._print_text(text)

        if action == "print_file":
            path = str(params.get("path", "")).strip()
            if not path or not os.path.isfile(path):
                return f"❌ 打印失败：找不到文件 `{path}`。"
            return await self._print_file(path)

        return f"❌ 打印机不支持的动作：{action}"

    # -- 具体实现 ---------------------------------------------------------
    async def _print_text(self, text: str) -> str:
        """把文本写入临时文件后用记事本打印。"""
        printer = str(self.plugin.config.get("printer_name", "")).strip()
        if self.plugin.config.get("dry_run", False):
            logger.info(f"[device_control][dry_run] 打印文本: {text[:80]}...")
            return f"🛠️ 调试模式：本应打印文本（{len(text)} 字）。"

        # 使用 utf-8-sig 以便记事本正确识别中文
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="astrbot_print_")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(text)

        cmd = ["notepad", "/pt", tmp_path, printer] if printer else ["notepad", "/p", tmp_path]
        ok, msg = await self.plugin.run_process(cmd)
        self.plugin.schedule_cleanup(tmp_path)
        if not ok:
            return f"❌ 打印失败：{msg}"
        target = printer or "默认打印机"
        return f"🖨️ 已发送到打印机（{target}）。"

    async def _print_file(self, path: str) -> str:
        printer = str(self.plugin.config.get("printer_name", "")).strip()
        if self.plugin.config.get("dry_run", False):
            logger.info(f"[device_control][dry_run] 打印文件: {path}")
            return f"🛠️ 调试模式：本应打印文件 `{os.path.basename(path)}`。"

        if sys.platform != "win32":
            return "❌ 当前仅支持 Windows 打印。"

        try:
            if printer:
                # 通过「printto」动词指定打印机
                os.startfile(path, "printto", printer)  # type: ignore[attr-defined]
            else:
                os.startfile(path, "print")  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.error(f"[device_control] 打印文件失败: {e}", exc_info=True)
            return f"❌ 打印失败：{e}"
        target = printer or "默认打印机"
        return f"🖨️ 已发送到打印机（{target}）。"


class SpeakerDevice(DeviceBase):
    """扬声器设备：用系统 TTS 朗读文本（Windows）。"""

    name = "speaker"
    description = "电脑扬声器，可朗读一段文本"
    actions = {
        "say": {
            "description": "用扬声器朗读一段文本",
            "params": {"text": "要朗读的文本"},
        },
    }

    async def execute(self, action: str, params: dict) -> str:
        if action != "say":
            return f"❌ 扬声器不支持的动作：{action}"
        text = str(params.get("text", "")).strip()
        if not text:
            return "❌ 朗读失败：文本为空。"
        if self.plugin.config.get("dry_run", False):
            logger.info(f"[device_control][dry_run] 朗读: {text[:80]}...")
            return f"🛠️ 调试模式：本应朗读（{len(text)} 字）。"

        if sys.platform != "win32":
            return "❌ 朗读功能当前仅支持 Windows。"

        # 把文本写入临时文件，避免在 PowerShell 命令里转义
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="astrbot_say_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$t = Get-Content -Raw -Encoding UTF8 '{tmp_path}'; "
            "$s.Speak($t);"
        )
        ok, msg = await self.plugin.run_process(["powershell", "-NoProfile", "-Command", script])
        self.plugin.schedule_cleanup(tmp_path)
        if not ok:
            return f"❌ 朗读失败：{msg}"
        return "🔊 已在扬声器播放。"


# ---------------------------------------------------------------------------
# LLM 工具定义
# ---------------------------------------------------------------------------
@pydantic_dataclass(config=dict(arbitrary_types_allowed=True))
class DeviceListTool(FunctionTool[AstrAgentContext]):
    """列出可用设备及其动作（无副作用，随时可调用）。"""

    name: str = "device_list"
    description: str = (
        "列出机器人当前可以控制的周边设备（如打印机、扬声器）及其可用动作。"
        "在需要操作设备前，先调用本工具了解可用能力。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )
    plugin: Any = Field(default=None)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        return self.plugin.describe_devices()


@pydantic_dataclass(config=dict(arbitrary_types_allowed=True))
class DeviceRequestTool(FunctionTool[AstrAgentContext]):
    """登记一个设备操作并向用户发起确认（不会立即执行）。"""

    name: str = "device_request"
    description: str = (
        "把内容输出到现实世界的周边设备。当用户希望获得一份「实物」时调用本工具，"
        "例如：让你写一封信/便签/清单并打印出来、把某段文字打印成纸、把某段文字用扬声器念出来。"
        "调用时需自行创作或准备要输出的内容，并通过 text 参数传入（print_file 用 file_path）。"
        "本工具不会立即执行，而是登记操作并向用户发出确认询问；只有用户确认后才会真正执行。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "设备名，例如 printer、speaker",
                },
                "action": {
                    "type": "string",
                    "description": "动作名，例如 printer 的 print_text / print_file，speaker 的 say",
                },
                "text": {
                    "type": "string",
                    "description": "当动作为 print_text 或 say 时，要处理/打印的文本内容",
                },
                "file_path": {
                    "type": "string",
                    "description": "当动作为 print_file 时，文件的绝对路径",
                },
            },
            "required": ["device", "action"],
        }
    )
    plugin: Any = Field(default=None)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = context.context.event
        device = str(kwargs.get("device", "")).strip()
        action = str(kwargs.get("action", "")).strip()
        params: dict = {}
        if kwargs.get("text"):
            params["text"] = kwargs["text"]
        if kwargs.get("file_path"):
            params["path"] = kwargs["file_path"]

        prompt, err = self.plugin.create_pending(event, device, action, params)
        if err:
            return err

        # 主动把确认询问发给用户，保证用户一定看得到
        try:
            await self.plugin.context.send_message(
                event.unified_msg_origin, MessageChain().message(prompt)
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[device_control] 发送确认询问失败: {e}", exc_info=True)

        return (
            "已登记待确认操作并向用户发出确认询问。"
            "请简短告知用户查看确认消息，不要重复调用本工具，也不要假定操作已执行。"
        )


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------
@register(
    "astrbot_plugin_device_control",
    "A3uracY",
    "让机器人控制周边设备（如打印机），执行前先向用户确认。",
    "0.1.0",
)
class DeviceControlPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        #: umo -> PendingAction
        self.pending: dict[str, PendingAction] = {}
        self.devices: dict[str, DeviceBase] = {}
        self._setup_devices()
        self._register_tools()
        logger.info(
            f"[device_control] 已加载，设备: {list(self.devices.keys())}, "
            f"dry_run={config.get('dry_run', False)}"
        )

    # -- 初始化 -----------------------------------------------------------
    def _setup_devices(self) -> None:
        if self.config.get("enable_printer", True):
            self.devices[PrinterDevice.name] = PrinterDevice(self)
        if self.config.get("enable_speaker", True):
            self.devices[SpeakerDevice.name] = SpeakerDevice(self)

    def _register_tools(self) -> None:
        if not self.config.get("enable_llm_tools", True):
            logger.info("[device_control] 已按配置跳过 LLM 工具注册。")
            return
        try:
            self.context.add_llm_tools(
                DeviceListTool(plugin=self),
                DeviceRequestTool(plugin=self),
            )
            logger.info("[device_control] LLM 工具已注册: device_list, device_request")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[device_control] 注册 LLM 工具失败: {e}", exc_info=True)

    # -- 通用工具方法 -----------------------------------------------------
    def describe_devices(self) -> str:
        if not self.devices:
            return "当前没有启用任何设备。"
        lines = ["可用设备："]
        for dev in self.devices.values():
            lines.append(f"- {dev.name}: {dev.description}")
            for act, meta in dev.actions.items():
                lines.append(f"    · {act}: {meta.get('description', '')}")
        return "\n".join(lines)

    def _remainder(self, event: AstrMessageEvent) -> str:
        """取出指令后的剩余文本。"""
        text = (event.message_str or "").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _normalize(text: str) -> str:
        """去掉空白与常见标点，便于匹配确认/取消。"""
        return re.sub(r"[\s，。！？!?、~,.…]+", "", (text or "").strip().lower())

    def _is_cancel(self, text: str) -> bool:
        n = self._normalize(text)
        return len(n) <= 8 and any(w in n for w in CANCEL_WORDS)

    def _is_confirm(self, text: str) -> bool:
        n = self._normalize(text)
        if not n:
            return False
        # 否定优先，避免「不确认」「先别打印」被误判为确认
        if any(w in n for w in ("取消", "不", "别", "no", "not")):
            return False
        if n in CONFIRM_WORDS:
            return True
        # 允许「好的，打印吧」「可以 帮我打印」这类短句
        return len(n) <= 8 and any(
            w in n for w in ("确认", "确定", "打印", "可以", "好的", "执行", "同意", "帮我")
        )

    def create_pending(
        self, event: AstrMessageEvent, device: str, action: str, params: dict
    ) -> tuple[str, str]:
        """登记待确认操作。返回 (确认提示文本, 错误文本)。二者其一为空。"""
        if device not in self.devices:
            return "", f"❌ 未知设备：{device}。可用设备：{list(self.devices.keys())}"
        dev = self.devices[device]
        if action not in dev.actions:
            return "", f"❌ 设备 {device} 不支持动作：{action}。可用：{dev.action_names()}"

        timeout = int(self.config.get("confirm_timeout", 60))
        summary = self._summarize(device, action, params)
        umo = event.unified_msg_origin
        self.pending[umo] = PendingAction(
            device=device,
            action=action,
            params=params,
            summary=summary,
            created_at=time.time(),
            timeout=timeout,
        )
        prompt = (
            f"⚠️ 即将执行设备操作：\n{summary}\n\n"
            f"请回复「确认」执行，或回复「取消」放弃（{timeout} 秒内有效）。"
        )
        logger.info(f"[device_control] 登记待确认操作 {umo}: {summary}")
        return prompt, ""

    @staticmethod
    def _summarize(device: str, action: str, params: dict) -> str:
        if device == "printer" and action == "print_text":
            text = str(params.get("text", ""))
            preview = text[:100] + ("..." if len(text) > 100 else "")
            return f"🖨️ 打印文本：{preview}"
        if device == "printer" and action == "print_file":
            return f"🖨️ 打印文件：{params.get('path', '')}"
        if device == "speaker" and action == "say":
            text = str(params.get("text", ""))
            preview = text[:100] + ("..." if len(text) > 100 else "")
            return f"🔊 朗读文本：{preview}"
        return f"设备 {device} 执行 {action}，参数：{params}"

    async def _execute(self, pending: PendingAction) -> str:
        """真正执行一个已确认的操作。"""
        dev = self.devices.get(pending.device)
        if dev is None:
            return f"❌ 设备已不可用：{pending.device}"
        try:
            return await dev.execute(pending.action, pending.params)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[device_control] 执行失败: {e}", exc_info=True)
            return f"❌ 执行失败：{e}"

    async def run_process(self, cmd: list[str]) -> tuple[bool, str]:
        """在子进程中执行命令，返回 (是否成功, 信息)。"""
        logger.info(f"[device_control] 执行命令: {cmd}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                msg = (stderr or b"").decode("utf-8", errors="ignore").strip()
                logger.error(f"[device_control] 命令返回码 {proc.returncode}: {msg}")
                return False, msg or f"返回码 {proc.returncode}"
            return True, ""
        except FileNotFoundError as e:
            return False, f"找不到命令：{e}"
        except Exception as e:  # noqa: BLE001
            logger.error(f"[device_control] 命令执行异常: {e}", exc_info=True)
            return False, str(e)

    @staticmethod
    def schedule_cleanup(path: str, delay: int = 60) -> None:
        """延迟删除临时文件。"""

        async def _cleanup() -> None:
            await asyncio.sleep(delay)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.get_event_loop().create_task(_cleanup())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("devices", alias={"设备列表", "设备"})
    async def devices_cmd(self, event: AstrMessageEvent):
        """列出机器人可以控制的周边设备。"""
        yield event.plain_result(self.describe_devices())

    @filter.command("print", alias={"打印"})
    async def print_cmd(self, event: AstrMessageEvent, text: str = ""):
        """打印一段文本：/print 要打印的内容"""
        text = (text or "").strip() or self._remainder(event)
        if not text:
            yield event.plain_result("用法：/print 要打印的内容")
            return
        prompt, err = self.create_pending(event, "printer", "print_text", {"text": text})
        yield event.plain_result(err or prompt)
        event.stop_event()

    @filter.command("printfile", alias={"打印文件"})
    async def printfile_cmd(self, event: AstrMessageEvent, path: str = ""):
        """打印本地文件：/printfile 文件绝对路径"""
        path = (path or "").strip() or self._remainder(event)
        if not path:
            yield event.plain_result("用法：/printfile 文件绝对路径")
            return
        prompt, err = self.create_pending(event, "printer", "print_file", {"path": path})
        yield event.plain_result(err or prompt)
        event.stop_event()

    @filter.command("say", alias={"朗读"})
    async def say_cmd(self, event: AstrMessageEvent, text: str = ""):
        """用扬声器朗读：/say 文本"""
        text = (text or "").strip() or self._remainder(event)
        if not text:
            yield event.plain_result("用法：/say 要朗读的文本")
            return
        prompt, err = self.create_pending(event, "speaker", "say", {"text": text})
        yield event.plain_result(err or prompt)
        event.stop_event()

    @filter.command("device_status", alias={"设备状态"})
    async def status_cmd(self, event: AstrMessageEvent):
        """查看插件运行状态，便于调试。"""
        umo = event.unified_msg_origin
        p = self.pending.get(umo)
        lines = [
            "📋 设备控制插件状态",
            f"- 已启用设备：{list(self.devices.keys())}",
            f"- 打印机：{self.config.get('printer_name', '') or '系统默认'}",
            f"- 调试模式(dry_run)：{self.config.get('dry_run', False)}",
            f"- 确认超时：{self.config.get('confirm_timeout', 60)} 秒",
        ]
        if p:
            lines.append(f"- 待确认操作：{p.summary}（剩余 {max(0, int(p.timeout - (time.time() - p.created_at)))} 秒）")
        else:
            lines.append("- 待确认操作：无")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 确认监听
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 让模型自行判断是否需要用设备（无需任何指令）
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_device_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        """在每轮请求的 system prompt 中注入稳定的设备能力说明。"""
        if not self.devices:
            return
        if req.system_prompt is None:
            req.system_prompt = ""
        if "【周边设备能力】" not in req.system_prompt:
            req.system_prompt += DEVICE_HINT

    # ------------------------------------------------------------------
    # 确认监听
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_confirmation(self, event: AstrMessageEvent):
        """监听「确认 / 取消」，处理待确认操作。"""
        umo = event.unified_msg_origin
        pending = self.pending.get(umo)
        if pending is None:
            return

        if pending.expired():
            self.pending.pop(umo, None)
            yield event.plain_result("⌛ 确认已超时，操作已取消。")
            event.stop_event()
            return

        raw = event.message_str or ""
        if self._is_cancel(raw):
            self.pending.pop(umo, None)
            yield event.plain_result("👌 已取消该操作。")
            event.stop_event()
        elif self._is_confirm(raw):
            self.pending.pop(umo, None)
            result = await self._execute(pending)
            yield event.plain_result(result)
            event.stop_event()
        # 其他内容不拦截，交给正常流程

    async def terminate(self):
        """插件卸载/停用时清理。"""
        self.pending.clear()

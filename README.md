# astrbot_plugin_device_control

> 让机器人控制周边的设备（例如打印机、扬声器），并且**先询问、后执行**。

本插件为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计。当用户提出使用设备的需求时，
机器人不会立刻执行，而是先登记一个「待确认操作」并询问用户；只有用户回复「确认」后，
才会真正调用设备完成任务；回复「取消」则放弃。

## 特性

- 🖨️ **打印机**：打印一段文本，或打印本地文件（txt / pdf / 图片等）。
- 🔊 **扬声器**：用系统 TTS 朗读文本。
- 🛡️ **确认流程**：所有会产生实际动作的操作，执行前都必须经用户确认。
- 🧩 **易于扩展**：新增设备只需继承 `DeviceBase` 并实现 `execute()`。
- 🐞 **方便调试**：`dry_run` 调试模式不会真正操作硬件，仅记录日志。
- 🤖 **LLM 工具**：机器人可自主判断并调用 `device_list` / `device_request`。

## 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，或在 WebUI 插件市场从本仓库安装：

```
AstrBot/data/plugins/astrbot_plugin_device_control/
```

然后在 WebUI 的「插件管理」中点击「重载插件」。

## 使用

### 指令

| 指令 | 说明 |
| --- | --- |
| `/devices` | 列出可用设备与动作 |
| `/print 要打印的内容` | 请求打印文本（会先询问确认） |
| `/printfile D:\path\to\file.pdf` | 请求打印文件（会先询问确认） |
| `/say 要朗读的文本` | 请求朗读文本（会先询问确认） |
| `/device_status` | 查看插件状态，便于调试 |

确认方式：收到询问后回复 **确认** 或 **取消**。

### 自然语言

也可以直接对机器人说：

> 帮我把「明天开会」打印出来

机器人会调用 `device_request` 工具登记操作，并发出确认询问；你回复「确认」后才会打印。

## 配置

在 WebUI 的插件配置页可修改（对应 `_conf_schema.json`）：

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_printer` | bool | true | 启用打印机设备 |
| `printer_name` | string | 空 | 目标打印机名称，留空使用系统默认 |
| `max_print_chars` | int | 5000 | 单次打印文本最大字符数 |
| `enable_speaker` | bool | true | 启用扬声器设备 |
| `enable_llm_tools` | bool | true | 启用 LLM 工具 |
| `confirm_timeout` | int | 60 | 确认等待超时（秒） |
| `dry_run` | bool | false | 调试模式，不真正操作硬件 |

## 二次开发：新增一个设备

```python
class MyDevice(DeviceBase):
    name = "my_device"
    description = "我的自定义设备"
    actions = {
        "do_something": {
            "description": "做某件事",
            "params": {"arg": "参数说明"},
        },
    }

    async def execute(self, action: str, params: dict) -> str:
        if action == "do_something":
            # 在这里实现具体动作
            return "✅ 完成"
        return f"不支持的动作: {action}"
```

然后在 `DeviceControlPlugin._setup_devices()` 中注册即可：

```python
self.devices[MyDevice.name] = MyDevice(self)
```

## 兼容性

- 打印与朗读功能目前基于 **Windows**（记事本打印 / .NET Speech）。
- AstrBot 版本要求：`>= 4.5.1`。

## License

MIT

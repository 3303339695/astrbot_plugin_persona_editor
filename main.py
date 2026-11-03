"""人格设定编辑器。

自带的人格编辑页只能往对话示例末尾追加，不能中途插入、不能调序。
这个插件把「对」拆开，一对一张卡片，能插、能挪、能删、能复制，改完整份写回。
整份人格（提示词、示例、工具、技能、报错话术）可以导出成一个文件，也能导回来。

接口：
    GET  /astrbot_plugin_persona_editor/editor/list        人格列表
    GET  /astrbot_plugin_persona_editor/editor/get         读一个人格
    GET  /astrbot_plugin_persona_editor/editor/catalog     工具与技能目录
    GET  /astrbot_plugin_persona_editor/editor/export_all  导出全部人格
    POST /astrbot_plugin_persona_editor/editor/save        整份写回
    POST /astrbot_plugin_persona_editor/editor/import      导入人格

只调人格管理器的公开方法，不碰数据库、不碰本体。
"""

from __future__ import annotations

import json
import re
import traceback
from datetime import datetime

from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.web import json_response, request
except Exception:  # 旧面板没有这个模块，页面不可用，插件仍能加载
    json_response = None
    request = None

PLUGIN_NAME = "astrbot_plugin_persona_editor"
PLUGIN_VERSION = "v1.1.0"

# 导出文件的格式标记，导入时用来认自己人，也方便以后升格式
FORMAT_ID = "astrbot-persona-full"
FORMAT_VERSION = 1

# 助手自带的导入会在重名时加 _imported、_imported_1 这样的尾巴
IMPORT_SUFFIX = re.compile(r"_imported(?:_\d+)?$")


def _as_list(value):
    """数据库里这一列可能是列表，也可能是一段 JSON 字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("不是列表")
        return list(parsed)
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError("不是列表")


def _as_dialogs(value) -> list:
    items = _as_list(value)
    if items is None:
        return []
    return [str(item) for item in items]


def _as_names(value):
    items = _as_list(value)
    if items is None:
        return None
    return [str(item) for item in items]


def _persona_payload(persona) -> dict:
    """把一个人格摊平成可以写成文件的形状。"""
    return {
        "persona_id": persona.persona_id,
        "system_prompt": persona.system_prompt or "",
        "begin_dialogs": _as_dialogs(persona.begin_dialogs),
        "tools": _as_names(getattr(persona, "tools", None)),
        "skills": _as_names(getattr(persona, "skills", None)),
        "custom_error_message": getattr(persona, "custom_error_message", None) or "",
        "folder_id": getattr(persona, "folder_id", None),
        "sort_order": int(getattr(persona, "sort_order", 0) or 0),
    }


def _strip_imported(name: str) -> str:
    """把助手导入时加上的 _imported 尾巴一层层去掉。"""
    text = (name or "").strip()
    for _ in range(5):
        stripped = IMPORT_SUFFIX.sub("", text).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    return text


def _clean_persona_id(raw, strip_imported: bool = True) -> str:
    name = str(raw or "").strip()
    if strip_imported:
        name = _strip_imported(name)
    if not name:
        raise ValueError("人格标识不能为空")
    if len(name) > 255:
        raise ValueError("人格标识最多 255 个字")
    if any(ch in name for ch in "/\\\r\n\t"):
        raise ValueError("人格标识里不能有斜杠和换行")
    return name


def _dialog_items(value) -> list:
    items = _as_list(value)
    if items is None:
        items = []
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError("对话示例第 %d 条不是文本" % (index + 1))
        result.append(item)
    if len(result) % 2:
        raise ValueError("对话示例是 %d 条，奇数没法配对" % len(result))
    return result


def _name_items(value, label: str):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(label + "必须是列表")
    result = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError("%s 第 %d 项不是文本" % (label, index + 1))
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _parse_payload(data) -> dict:
    """把一份导出文件翻译成新建/更新人格要用的字段。"""
    if not isinstance(data, dict):
        raise ValueError("人格数据不是对象")
    inner = data.get("persona")
    if isinstance(inner, dict):
        data = inner
    prompt = data.get("system_prompt")
    if prompt is None:
        prompt = data.get("prompt")  # 四点零以前的旧备份用这个字段
    if prompt is None:
        raise ValueError("缺少系统提示词")
    if not isinstance(prompt, str):
        raise ValueError("系统提示词必须是文本")
    error_message = data.get("custom_error_message")
    if error_message is None:
        error_message = ""
    if not isinstance(error_message, str):
        raise ValueError("自定义报错回复必须是文本")
    folder_id = data.get("folder_id")
    if folder_id is not None and not isinstance(folder_id, str):
        raise ValueError("所属文件夹格式不对")
    sort_order = data.get("sort_order")
    if sort_order is None:
        sort_order = 0
    try:
        sort_order = int(sort_order)
    except (TypeError, ValueError):
        raise ValueError("排序值必须是整数")
    return {
        "persona_id": str(data.get("persona_id") or data.get("name") or ""),
        "system_prompt": prompt,
        "begin_dialogs": _dialog_items(data.get("begin_dialogs")),
        "tools": _name_items(data.get("tools"), "工具"),
        "skills": _name_items(data.get("skills"), "技能"),
        "custom_error_message": error_message,
        "folder_id": folder_id,
        "sort_order": sort_order,
    }


@register(PLUGIN_NAME, "咱", "人格设定编辑器：按对编辑对话示例，整份导出与导入", PLUGIN_VERSION)
class PersonaEditor(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.conf = config if config is not None else {}
        self._register_apis(context)

    # ------------------------------------------------------------------ 接口

    def _register_apis(self, context: Context) -> None:
        if json_response is None or request is None:
            return
        try:
            context.register_web_api(
                "/%s/editor/list" % PLUGIN_NAME, self.api_list, ["GET"], "人格列表"
            )
            context.register_web_api(
                "/%s/editor/get" % PLUGIN_NAME, self.api_get, ["GET"], "读取一个的人格"
            )
            context.register_web_api(
                "/%s/editor/catalog" % PLUGIN_NAME, self.api_catalog, ["GET"], "工具与技能目录"
            )
            context.register_web_api(
                "/%s/editor/export_all" % PLUGIN_NAME, self.api_export_all, ["GET"], "导出全部人格"
            )
            context.register_web_api(
                "/%s/editor/save" % PLUGIN_NAME, self.api_save, ["POST"], "整份写回"
            )
            context.register_web_api(
                "/%s/editor/import" % PLUGIN_NAME, self.api_import, ["POST"], "导入人格"
            )
        except Exception:
            traceback.print_exc()

    async def api_catalog(self):
        result = {"tools": [], "skills": []}
        errors = {}
        try:
            manager = self.context.get_llm_tool_manager()
            stars = self.context.get_all_stars()
            modules = {s.module_path: s for s in stars if s.module_path}
            tools = list(manager.func_list)
            names = {t.name for t in tools}
            for tool in manager.iter_builtin_tools():
                if tool.name not in names:
                    tools.append(tool)
                    names.add(tool.name)
            for tool in tools:
                star = modules.get(getattr(tool, "handler_module_path", None))
                source = (
                    "AstrBot Core" if manager.is_builtin_tool(tool.name)
                    else getattr(tool, "mcp_server_name", None)
                    or (star.display_name or star.name if star else "")
                )
                result["tools"].append({
                    "name": tool.name,
                    "source": str(source or ""),
                    "desc": str(tool.description or ""),
                })
        except Exception as exc:
            result["tools"] = []
            errors["tools"] = str(exc) or type(exc).__name__
        try:
            from astrbot.core.skills.skill_manager import SkillManager

            runtime = self.context.get_config().get("provider_settings", {}).get(
                "computer_use_runtime", "local"
            )
            stars = self.context.get_all_stars()
            names = {
                key: s.display_name or s.name or key
                for s in stars for key in (s.name, s.root_dir_name) if key
            }
            for skill in SkillManager().list_skills(
                active_only=False, runtime=runtime, show_sandbox_path=False
            ):
                source = names.get(skill.plugin_name, skill.plugin_name) if skill.plugin_name else skill.source_label
                result["skills"].append({
                    "name": skill.name,
                    "source": str(source or ""),
                    "desc": str(skill.description or ""),
                })
        except Exception as exc:
            result["skills"] = []
            errors["skills"] = str(exc) or type(exc).__name__
        if errors:
            result["errors"] = errors
            result["error"] = "; ".join("%s: %s" % item for item in errors.items())
        return json_response(result)

    async def api_list(self):
        try:
            pm = self.context.persona_manager
            rows = []
            for persona in await pm.get_all_personas():
                dialogs = _as_dialogs(persona.begin_dialogs)
                rows.append(
                    {
                        "persona_id": persona.persona_id,
                        "pairs": len(dialogs) // 2,
                        "prompt_len": len(persona.system_prompt or ""),
                    }
                )
            return json_response({"personas": rows})
        except Exception as exc:
            return json_response({"error": str(exc)}, status_code=500)

    async def api_get(self):
        try:
            persona_id = str(request.query.get("persona_id", "") or "").strip()
            if not persona_id:
                return json_response({"error": "缺少人格标识"}, status_code=400)
            pm = self.context.persona_manager
            persona = await pm.get_persona(persona_id)
            dialogs = _as_dialogs(persona.begin_dialogs)
            if len(dialogs) % 2:
                raise ValueError("对话示例数量为奇数，无法组成完整的对")
            pairs = [
                {"user": dialogs[i], "assistant": dialogs[i + 1]}
                for i in range(0, len(dialogs), 2)
            ]
            return json_response(
                {
                    "persona_id": persona_id,
                    "system_prompt": persona.system_prompt or "",
                    "custom_error_message": persona.custom_error_message or "",
                    "tools": getattr(persona, "tools", None),
                    "skills": getattr(persona, "skills", None),
                    "folder_id": getattr(persona, "folder_id", None),
                    "sort_order": int(getattr(persona, "sort_order", 0) or 0),
                    "pairs": pairs,
                    "updated_at": str(getattr(persona, "updated_at", "") or ""),
                }
            )
        except Exception as exc:
            return json_response({"error": str(exc)}, status_code=500)

    async def api_export_all(self):
        try:
            pm = self.context.persona_manager
            personas = [_persona_payload(p) for p in await pm.get_all_personas()]
            return json_response(
                {
                    "format": FORMAT_ID,
                    "format_version": FORMAT_VERSION,
                    "plugin": PLUGIN_NAME,
                    "plugin_version": PLUGIN_VERSION.lstrip("v"),
                    "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "personas": personas,
                }
            )
        except Exception as exc:
            traceback.print_exc()
            return json_response({"error": str(exc)}, status_code=500)

    async def api_save(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return json_response({"error": "请求内容不是有效的 JSON 对象"}, status_code=400)
            persona_id = str(body.get("persona_id") or "").strip()
            if not persona_id:
                return json_response({"error": "缺少人格标识"}, status_code=400)
            pairs = body.get("pairs", [])
            if not isinstance(pairs, list):
                return json_response({"error": "示例格式不对"}, status_code=400)
            dialogs: list[str] = []
            for index, pair in enumerate(pairs):
                if not isinstance(pair, dict):
                    return json_response({"error": "第 %d 对格式不对" % (index + 1)}, status_code=400)
                dialogs.append(str(pair.get("user") or ""))
                dialogs.append(str(pair.get("assistant") or ""))

            kwargs = {"persona_id": persona_id, "begin_dialogs": dialogs}
            if "system_prompt" in body and body["system_prompt"] is not None:
                kwargs["system_prompt"] = str(body["system_prompt"])
            if "custom_error_message" in body:
                text = body["custom_error_message"]
                kwargs["custom_error_message"] = str(text) if text else None
            for field in ("tools", "skills"):
                if field in body:
                    value = body[field]
                    if value is not None and (
                        not isinstance(value, list)
                        or not all(isinstance(item, str) for item in value)
                    ):
                        label = "工具" if field == "tools" else "技能"
                        return json_response({"error": label + "格式不对"}, status_code=400)
                    kwargs[field] = value
            pm = self.context.persona_manager
            await pm.update_persona(**kwargs)
            return json_response({"ok": True, "pairs": len(dialogs) // 2})
        except Exception as exc:
            traceback.print_exc()
            return json_response({"ok": False, "error": str(exc)}, status_code=500)

    async def api_import(self):
        """导入人格设定。

        请求体：
            {"strip_imported": true, "items": [{"data": {...}, "persona_id": "名字", "mode": "create"}]}
        mode 不填时，重名就用覆盖，没重名就新建。
        只给一个对象（不带 items）也可以，就当只有一项。
        """
        try:
            body = await request.json()
        except Exception:
            return json_response({"ok": False, "error": "请求内容不是有效的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return json_response({"ok": False, "error": "请求内容不是对象"}, status_code=400)
        items = body.get("items")
        if not isinstance(items, list):
            items = [body]
        if not items:
            return json_response({"ok": False, "error": "没有要导入的人格"}, status_code=400)
        strip_imported = body.get("strip_imported", True) is not False

        try:
            pm = self.context.persona_manager
            existing = {p.persona_id for p in await pm.get_all_personas()}
        except Exception as exc:
            traceback.print_exc()
            return json_response({"ok": False, "error": str(exc)}, status_code=500)

        results = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "第 %d 项不是对象" % (index + 1)})
                continue
            try:
                data = item.get("data")
                if not isinstance(data, dict):
                    data = item
                fields = _parse_payload(data)
                target = _clean_persona_id(
                    item.get("persona_id") or fields["persona_id"], strip_imported
                )
                mode = str(item.get("mode") or "").strip().lower()
                if mode not in ("create", "overwrite"):
                    mode = "overwrite" if target in existing else "create"

                folder_id = fields["folder_id"]
                if folder_id:
                    try:
                        if not await pm.get_folder(folder_id):
                            folder_id = None  # 这台机器上没有这个人格文件夹，就放根目录
                    except Exception:
                        folder_id = None

                error_message = fields["custom_error_message"] or None
                if mode == "overwrite":
                    if target not in existing:
                        raise ValueError("要覆盖的人格「%s」不存在" % target)
                    await pm.update_persona(
                        persona_id=target,
                        system_prompt=fields["system_prompt"],
                        begin_dialogs=fields["begin_dialogs"],
                        tools=fields["tools"],
                        skills=fields["skills"],
                        custom_error_message=error_message,
                    )
                else:
                    if target in existing:
                        raise ValueError("人格「%s」已经存在，要用覆盖" % target)
                    await pm.create_persona(
                        target,
                        fields["system_prompt"],
                        fields["begin_dialogs"],
                        tools=fields["tools"],
                        skills=fields["skills"],
                        custom_error_message=error_message,
                        folder_id=folder_id,
                        sort_order=fields["sort_order"],
                    )
                existing.add(target)
                results.append(
                    {
                        "ok": True,
                        "persona_id": target,
                        "action": mode,
                        "pairs": len(fields["begin_dialogs"]) // 2,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "persona_id": str(item.get("persona_id") or ""),
                        "error": str(exc) or type(exc).__name__,
                    }
                )

        done = sum(1 for row in results if row.get("ok"))
        return json_response(
            {
                "ok": done == len(results),
                "imported": done,
                "total": len(results),
                "results": results,
            }
        )

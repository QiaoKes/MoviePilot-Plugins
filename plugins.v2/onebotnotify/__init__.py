"""
OneBot v11 通知插件
接入 MoviePilot 普通通知消息，并通过 OneBot v11 HTTP API 转发到 QQ 私聊或群聊。
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MessageChannel, NotificationType
from app.utils.http import RequestUtils


class OneBotNotify(_PluginBase):
    """OneBot v11 通知转发"""

    plugin_name = "OneBot通知"
    plugin_desc = "接入 MoviePilot 普通通知消息，通过 OneBot v11 HTTP API 推送到 QQ。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/QQ_A.png"
    plugin_version = "1.0.0"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "onebotnotify_"
    plugin_order = 22
    auth_level = 1

    _enabled: bool = False
    _base_url: str = ""
    _access_token: str = ""
    _token_in_query: bool = False
    _group_ids: List[str] = []
    _user_ids: List[str] = []
    _message_types: List[str] = []
    _prefer_event_target: bool = False
    _include_image: bool = True
    _include_link: bool = True
    _timeout: int = 10

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._base_url = str(config.get("base_url") or "").strip()
        self._access_token = str(config.get("access_token") or "").strip()
        self._token_in_query = bool(config.get("token_in_query", False))
        self._group_ids = self._split_values(config.get("group_ids") or [])
        self._user_ids = self._split_values(config.get("user_ids") or [])
        self._message_types = self._normalize_message_types(config.get("message_types") or [])
        self._prefer_event_target = bool(config.get("prefer_event_target", False))
        self._include_image = bool(config.get("include_image", True))
        self._include_link = bool(config.get("include_link", True))
        try:
            self._timeout = max(3, int(config.get("timeout") or 10))
        except Exception:
            self._timeout = 10

    @staticmethod
    def _split_values(values: Any) -> List[str]:
        if not values:
            return []
        if isinstance(values, str):
            raw_values = values.replace("\n", ",").replace(";", ",").split(",")
        elif isinstance(values, list):
            raw_values = values
        else:
            raw_values = [values]
        result: List[str] = []
        for value in raw_values:
            value = str(value).strip()
            if value and value not in result:
                result.append(value)
        return result

    @classmethod
    def _normalize_message_types(cls, values: Any) -> List[str]:
        return [cls._enum_value(value) for value in cls._split_values(values)]

    @staticmethod
    def _enum_value(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    @staticmethod
    def _normalize_id(value: str) -> Any:
        value = str(value).strip()
        if value.isdigit():
            return int(value)
        return value

    @staticmethod
    def _dedupe(values: List[str]) -> List[str]:
        result: List[str] = []
        for value in values:
            value = str(value).strip()
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _escape_cq_text(text: Any) -> str:
        text = "" if text is None else str(text)
        return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")

    @staticmethod
    def _escape_cq_param(text: Any) -> str:
        text = "" if text is None else str(text)
        return (
            text.replace("&", "&amp;")
            .replace("[", "&#91;")
            .replace("]", "&#93;")
            .replace(",", "&#44;")
        )

    def _notice_type_allowed(self, data: Dict[str, Any]) -> bool:
        if not self._message_types:
            return True
        mtype = self._enum_value(data.get("mtype") or data.get("type"))
        return mtype in self._message_types

    def _message_channel_allowed(self, data: Dict[str, Any]) -> bool:
        channel = self._enum_value(data.get("channel"))
        return not channel or channel == MessageChannel.QQ.value

    def _message_targets(self, data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        groups: List[str] = []
        users: List[str] = []
        if not self._prefer_event_target:
            return groups, users

        userid = data.get("userid")
        if userid:
            userid = str(userid).strip()
            if userid.lower().startswith("group:"):
                groups.append(userid[6:].strip())
            else:
                users.append(userid)

        targets = data.get("targets")
        if isinstance(targets, dict):
            for key in ("onebot_group_id", "onebot_group", "qq_group_id", "qq_group"):
                if targets.get(key):
                    groups.extend(self._split_values(targets.get(key)))
            for key in ("onebot_user_id", "onebot_user", "qq_user_id", "qq_userid", "qq"):
                if targets.get(key):
                    users.extend(self._split_values(targets.get(key)))

        return self._dedupe(groups), self._dedupe(users)

    def _targets_for_notice(self, data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        message_groups, message_users = self._message_targets(data)
        groups = message_groups or list(self._group_ids)
        users = message_users or list(self._user_ids)
        return self._dedupe(groups), self._dedupe(users)

    def _build_message(
            self,
            data: Optional[Dict[str, Any]] = None,
            title: Optional[str] = None,
            text: Optional[str] = None
    ) -> str:
        data = data or {}
        title = title if title is not None else data.get("title")
        text = text if text is not None else data.get("text")
        image = data.get("image")
        link = data.get("link")

        parts: List[str] = []
        if title:
            parts.append(f"【{self._escape_cq_text(title)}】")
        if text:
            parts.append(self._escape_cq_text(text).strip())
        if self._include_image and image:
            parts.append(f"[CQ:image,file={self._escape_cq_param(image)}]")
        if self._include_link and link:
            parts.append(f"详情：{self._escape_cq_text(link)}")
        return "\n".join(part for part in parts if part).strip() or "MoviePilot 通知"

    def _onebot_url(self, action: str) -> str:
        return f"{self._base_url.rstrip('/')}/{action.lstrip('/')}"

    def _request_options(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        headers = {"Content-Type": "application/json"}
        params: Dict[str, str] = {}
        if self._access_token:
            if self._token_in_query:
                params["access_token"] = self._access_token
            else:
                headers["Authorization"] = f"Bearer {self._access_token}"
        return headers, params

    @staticmethod
    def _response_ok(resp) -> Tuple[bool, str]:
        if resp is None:
            return False, "OneBot 无响应"
        status_code = getattr(resp, "status_code", 0)
        try:
            data = resp.json()
        except Exception:
            data = None
        if status_code and status_code >= 400:
            return False, f"HTTP {status_code}: {data or getattr(resp, 'text', '')}"
        if isinstance(data, dict):
            retcode = data.get("retcode")
            status = str(data.get("status") or "").lower()
            if retcode not in (None, 0):
                return False, str(data)
            if status and status not in ("ok", "async"):
                return False, str(data)
        return True, str(data or "ok")

    def _send_action(self, action: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        if not self._base_url:
            return False, "未配置 OneBot HTTP 地址"
        headers, params = self._request_options()
        try:
            resp = RequestUtils(timeout=self._timeout).post_res(
                self._onebot_url(action),
                params=params,
                json=payload,
                headers=headers
            )
            return self._response_ok(resp)
        except Exception as e:
            return False, str(e)

    def _send_to_target(self, target_type: str, target_id: str, message: str) -> Tuple[bool, str]:
        if target_type == "group":
            return self._send_action("send_group_msg", {
                "group_id": self._normalize_id(target_id),
                "message": message,
                "auto_escape": False
            })
        return self._send_action("send_private_msg", {
            "user_id": self._normalize_id(target_id),
            "message": message,
            "auto_escape": False
        })

    def _send_message(self, message: str, groups: List[str], users: List[str]) -> Dict[str, Any]:
        details: List[Dict[str, Any]] = []
        success_count = 0
        fail_count = 0

        for group_id in groups:
            ok, result = self._send_to_target("group", group_id, message)
            details.append({"type": "group", "target": group_id, "success": ok, "result": result})
            success_count += 1 if ok else 0
            fail_count += 0 if ok else 1
        for user_id in users:
            ok, result = self._send_to_target("private", user_id, message)
            details.append({"type": "private", "target": user_id, "success": ok, "result": result})
            success_count += 1 if ok else 0
            fail_count += 0 if ok else 1

        result = {
            "success": success_count > 0 and fail_count == 0,
            "success_count": success_count,
            "fail_count": fail_count,
            "details": details,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.save_data("last_result", result)
        return result

    def _dispatch_notification(self, data: Dict[str, Any], warning_context: str = "OneBot通知"):
        if not self._enabled:
            return None
        if not isinstance(data, dict):
            return None
        if not self._message_channel_allowed(data):
            return None
        if not self._notice_type_allowed(data):
            return None

        groups, users = self._targets_for_notice(data)
        if not groups and not users:
            logger.warning(f"{warning_context}未配置接收目标，跳过发送")
            return None

        message = self._build_message(data)
        result = self._send_message(message=message, groups=groups, users=users)
        if result.get("success_count"):
            logger.info(
                f"OneBot通知发送完成：成功 {result.get('success_count')}，"
                f"失败 {result.get('fail_count')}"
            )
        else:
            logger.warning(f"OneBot通知发送失败：{result.get('details')}")
        return None

    @staticmethod
    def _message_to_dict(message: Any) -> Dict[str, Any]:
        if not message:
            return {}
        if isinstance(message, dict):
            return dict(message)
        if hasattr(message, "model_dump"):
            return message.model_dump()
        if hasattr(message, "to_dict"):
            return message.to_dict()
        return {}

    def module_post_message(self, message: Any = None, **kwargs):
        data = self._message_to_dict(message)
        return self._dispatch_notification(data)

    def api_test(self, message: str = "") -> Dict[str, Any]:
        groups, users = self._targets_for_notice({})
        if not groups and not users:
            return {"success": False, "message": "未配置群号或用户QQ号"}
        test_message = self._build_message(
            title="OneBot通知测试",
            text=message or f"MoviePilot OneBot v11 通知测试：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return self._send_message(test_message, groups=groups, users=users)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_module(self) -> Dict[str, Any]:
        return {
            "post_message": self.module_post_message,
        }

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET", "POST"],
                "summary": "发送 OneBot 测试通知"
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        type_items = [
            {"title": item.value, "value": item.value}
            for item in NotificationType
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "enabled", "label": "启用"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "include_image", "label": "发送图片"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "include_link", "label": "发送链接"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "prefer_event_target", "label": "优先消息目标"
                                    }}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "base_url",
                                        "label": "OneBot HTTP 地址",
                                        "placeholder": "http://127.0.0.1:5700",
                                        "hint": "填写 OneBot v11 正向 HTTP API 根地址"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "timeout",
                                        "label": "请求超时秒数",
                                        "type": "number",
                                        "placeholder": "10"
                                    }}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "access_token",
                                        "label": "Access Token",
                                        "type": "password",
                                        "placeholder": "未设置可留空"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "token_in_query",
                                        "label": "Token放入URL参数"
                                    }}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "group_ids",
                                        "label": "群号",
                                        "placeholder": "123456,234567",
                                        "hint": "多个群号用逗号分隔"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "user_ids",
                                        "label": "用户QQ号",
                                        "placeholder": "10001,10002",
                                        "hint": "多个QQ号用逗号分隔"
                                    }}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VSelect", "props": {
                                        "model": "message_types",
                                        "label": "通知类型过滤",
                                        "items": type_items,
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "hint": "不选择则转发全部 MoviePilot 通知"
                                    }}
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "base_url": "http://127.0.0.1:5700",
            "access_token": "",
            "token_in_query": False,
            "group_ids": "",
            "user_ids": "",
            "message_types": [],
            "prefer_event_target": False,
            "include_image": True,
            "include_link": True,
            "timeout": 10,
        }

    def get_page(self) -> Optional[List[dict]]:
        last_result = self.get_data("last_result") or {}
        rows = []
        for detail in last_result.get("details") or []:
            rows.append({
                "type": detail.get("type"),
                "target": detail.get("target"),
                "success": "成功" if detail.get("success") else "失败",
                "result": str(detail.get("result") or "")[:160]
            })
        return [
            {
                "component": "VCard",
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 8},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-subtitle-1"},
                                             "text": f"最近发送：{last_result.get('time') or '暂无'}"},
                                            {"component": "div", "props": {"class": "text-caption"},
                                             "text": f"成功 {last_result.get('success_count', 0)}，失败 {last_result.get('fail_count', 0)}"}
                                        ]
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4, "class": "text-md-right"},
                                        "content": [
                                            {
                                                "component": "VBtn",
                                                "props": {
                                                    "color": "primary",
                                                    "variant": "outlined",
                                                    "prepend-icon": "mdi-send"
                                                },
                                                "text": "发送测试",
                                                "events": {
                                                    "click": {
                                                        "api": f"/plugin/OneBotNotify/test?apikey={settings.API_TOKEN}",
                                                        "method": "get"
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                "component": "VTable",
                                "props": {"class": "mt-4"},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {"component": "tr", "content": [
                                                {"component": "th", "text": "类型"},
                                                {"component": "th", "text": "目标"},
                                                {"component": "th", "text": "状态"},
                                                {"component": "th", "text": "响应"}
                                            ]}
                                        ]
                                    },
                                    {
                                        "component": "tbody",
                                        "content": [
                                            {"component": "tr", "content": [
                                                {"component": "td", "text": str(row.get("type") or "")},
                                                {"component": "td", "text": str(row.get("target") or "")},
                                                {"component": "td", "text": str(row.get("success") or "")},
                                                {"component": "td", "text": str(row.get("result") or "")}
                                            ]}
                                            for row in rows[:20]
                                        ] or [
                                            {"component": "tr", "content": [
                                                {"component": "td", "props": {"colspan": 4, "class": "text-center text-grey"},
                                                 "text": "暂无发送记录"}
                                            ]}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    @staticmethod
    def get_service() -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    def close(self):
        self.stop_service()

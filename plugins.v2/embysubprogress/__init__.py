"""
Emby订阅进度同步插件
根据媒体服务器已入库剧集回写 MoviePilot 订阅进度。
"""
import datetime
import re
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.mediaserver_oper import MediaServerOper
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType, NotificationType


class EmbySubProgress(_PluginBase):
    """媒体服务器订阅进度同步"""

    plugin_name = "Emby订阅进度同步"
    plugin_desc = "读取媒体服务器已入库剧集，回写 MoviePilot 我的订阅进度。"
    plugin_icon = "https://raw.githubusercontent.com/boeto/MoviePilot-Plugins/main/icons/Emby_A.png"
    plugin_version = "1.0.0"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "embysubprogress_"
    plugin_order = 21
    auth_level = 1

    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "15 3 * * *"
    _notify: bool = False
    _finish_completed: bool = False
    _overwrite_note: bool = False
    _include_new_state: bool = True
    _mediaservers: List[str] = []
    _exclude_subscribes: List[int] = []
    _scheduler: Optional[BackgroundScheduler] = None
    _lock = Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = str(config.get("cron") or self._cron).strip()
            self._notify = bool(config.get("notify", False))
            self._finish_completed = bool(config.get("finish_completed", False))
            self._overwrite_note = bool(config.get("overwrite_note", False))
            self._include_new_state = bool(config.get("include_new_state", True))
            self._mediaservers = config.get("mediaservers") or []
            self._exclude_subscribes = self._normalize_ids(config.get("exclude_subscribes") or [])

        if self._onlyonce:
            self._onlyonce = False
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sync_subscribe_progress,
                trigger="date",
                run_date=datetime.datetime.now(
                    tz=datetime.timezone.utc
                ) + datetime.timedelta(seconds=3),
                name="Emby订阅进度同步立即运行"
            )
            self.__update_config()

        if self._scheduler and self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "notify": self._notify,
            "finish_completed": self._finish_completed,
            "overwrite_note": self._overwrite_note,
            "include_new_state": self._include_new_state,
            "mediaservers": self._mediaservers,
            "exclude_subscribes": self._exclude_subscribes,
        })

    @staticmethod
    def _normalize_ids(values: Any) -> List[int]:
        if not values:
            return []
        if isinstance(values, str):
            raw_values = [v.strip() for v in values.split(",")]
        elif isinstance(values, list):
            raw_values = values
        else:
            raw_values = [values]
        result: List[int] = []
        for value in raw_values:
            try:
                if value is not None and str(value).strip():
                    result.append(int(value))
            except Exception:
                logger.warning(f"订阅ID格式无效，已跳过：{value}")
        return list(dict.fromkeys(result))

    @staticmethod
    def _to_int_set(values: Any) -> Set[int]:
        result: Set[int] = set()
        if not values:
            return result
        if isinstance(values, str):
            values = re.findall(r"\d+", values)
        for value in values:
            try:
                ivalue = int(value)
                if ivalue > 0:
                    result.add(ivalue)
            except Exception:
                continue
        return result

    def _recognize_media(self, subscribe) -> Optional[MediaInfo]:
        meta = MetaInfo(subscribe.name)
        meta.year = subscribe.year
        meta.begin_season = subscribe.season or 1
        meta.type = MediaType.TV
        try:
            mediainfo: MediaInfo = self.chain.recognize_media(
                meta=meta,
                mtype=MediaType.TV,
                tmdbid=subscribe.tmdbid,
                doubanid=subscribe.doubanid,
                bangumiid=getattr(subscribe, "bangumiid", None),
                episode_group=getattr(subscribe, "episode_group", None),
                cache=True
            )
            if mediainfo:
                mediainfo.season = subscribe.season or mediainfo.season or 1
                return mediainfo
        except Exception as e:
            logger.warning(f"订阅 {subscribe.name} 媒体信息识别失败，将使用订阅字段查询：{e}")

        mediainfo = MediaInfo()
        mediainfo.type = MediaType.TV
        mediainfo.title = subscribe.name
        mediainfo.year = subscribe.year
        mediainfo.season = subscribe.season or 1
        mediainfo.tmdb_id = subscribe.tmdbid
        mediainfo.imdb_id = getattr(subscribe, "imdbid", None)
        mediainfo.tvdb_id = getattr(subscribe, "tvdbid", None)
        mediainfo.douban_id = subscribe.doubanid
        mediainfo.bangumi_id = getattr(subscribe, "bangumiid", None)
        return mediainfo

    @staticmethod
    def _get_local_item_id(mediainfo: MediaInfo, season: int) -> Optional[str]:
        try:
            return MediaServerOper().get_item_id(
                mtype=MediaType.TV.value,
                title=mediainfo.title,
                tmdbid=mediainfo.tmdb_id
            )
        except Exception as e:
            logger.debug(f"读取媒体库登记簿失败：{e}")
        return None

    def _target_servers(self) -> List[Dict[str, Any]]:
        services = MediaServerHelper().get_services(name_filters=self._mediaservers or None)
        if not services:
            return []
        servers: List[Dict[str, Any]] = []
        for name, service in services.items():
            if service and service.instance and hasattr(service.instance, "is_inactive") \
                    and service.instance.is_inactive():
                logger.warning(f"媒体服务器 {name} 未连接，跳过")
                continue
            servers.append({"name": name, "service": service})
        return servers

    def _query_existing_episodes(self, subscribe, servers: List[Dict[str, Any]]) -> Tuple[Set[int], Optional[str]]:
        mediainfo = self._recognize_media(subscribe)
        if not mediainfo:
            return set(), None
        target_season = subscribe.season or 1
        item_id = self._get_local_item_id(mediainfo, target_season)
        all_episodes: Set[int] = set()
        matched_servers: List[str] = []

        for server_info in servers:
            server = server_info.get("name")
            service = server_info.get("service")
            try:
                if not service or not service.module:
                    continue
                exists_info = service.module.media_exists(
                    mediainfo=mediainfo,
                    itemid=item_id,
                    server=server
                )
            except Exception as e:
                logger.warning(f"查询媒体服务器 {server} 的 {subscribe.name} 失败：{e}")
                continue
            if not exists_info or not exists_info.seasons:
                continue
            episodes = self._to_int_set(exists_info.seasons.get(target_season) or [])
            if episodes:
                all_episodes.update(episodes)
                matched_servers.append(server)

        return all_episodes, ",".join(matched_servers) if matched_servers else None

    def _expected_episodes(self, subscribe) -> Set[int]:
        total_episode = int(subscribe.total_episode or 0)
        if total_episode <= 0:
            return set()
        start_episode = int(subscribe.start_episode or 1)
        if start_episode <= 0:
            start_episode = 1
        return set(range(start_episode, total_episode + 1))

    def _sync_one(self, subscribe, servers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        existing_episodes, matched_server = self._query_existing_episodes(subscribe, servers)
        expected = self._expected_episodes(subscribe)

        # 参考 115 追更：订阅已经显示完整时，不再依赖媒体服务器返回集数，
        # 直接补齐 note，避免 26/26 这类订阅仍缺少完成集数记录。
        if not existing_episodes and expected and int(subscribe.lack_episode or 0) == 0:
            existing_episodes = set(expected)
            matched_server = matched_server or "订阅进度"

        if not existing_episodes:
            return {
                "id": subscribe.id,
                "name": subscribe.name,
                "season": subscribe.season or 1,
                "status": "未入库",
                "changed": False,
                "message": "媒体服务器未找到对应剧集"
            }

        current_note = self._to_int_set(subscribe.note or [])
        if expected:
            existing_episodes = existing_episodes & expected
        if not existing_episodes:
            return {
                "id": subscribe.id,
                "name": subscribe.name,
                "season": subscribe.season or 1,
                "status": "无有效集数",
                "changed": False,
                "message": "媒体服务器集数不在订阅范围内"
            }

        new_note = existing_episodes if self._overwrite_note else current_note | existing_episodes
        if expected:
            new_lack = len(expected - new_note)
        else:
            current_lack = int(subscribe.lack_episode or 0)
            newly_done = len(existing_episodes - current_note)
            new_lack = max(0, current_lack - newly_done)

        update_data: Dict[str, Any] = {}
        if new_note != current_note:
            update_data["note"] = sorted(new_note)
        if new_lack != int(subscribe.lack_episode or 0):
            update_data["lack_episode"] = new_lack
        if update_data:
            SubscribeOper().update(subscribe.id, update_data)
            logger.info(
                f"同步订阅进度：{subscribe.name} S{subscribe.season or 1} "
                f"note {sorted(current_note)} -> {sorted(new_note)}，"
                f"lack_episode {subscribe.lack_episode} -> {new_lack}"
            )

        completed = expected and not (expected - new_note)
        if completed and self._finish_completed:
            try:
                refreshed_subscribe = SubscribeOper().get(subscribe.id)
                mediainfo = self._recognize_media(refreshed_subscribe)
                if not mediainfo:
                    raise RuntimeError("媒体信息识别失败")
                meta = MetaInfo(refreshed_subscribe.name)
                meta.year = refreshed_subscribe.year
                meta.begin_season = refreshed_subscribe.season or 1
                meta.type = MediaType.TV
                SubscribeChain().finish_subscribe_or_not(
                    subscribe=refreshed_subscribe,
                    meta=meta,
                    mediainfo=mediainfo,
                    downloads=None,
                    lefts={},
                    force=True
                )
            except Exception as e:
                logger.error(f"完成订阅 {subscribe.name} 失败：{e}")

        return {
            "id": subscribe.id,
            "name": subscribe.name,
            "season": subscribe.season or 1,
            "server": matched_server,
            "existing": sorted(existing_episodes),
            "note": sorted(new_note),
            "lack_episode": new_lack,
            "changed": bool(update_data),
            "completed": bool(completed),
            "status": "已更新" if update_data else "无需更新"
        }

    def sync_subscribe_progress(self) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            logger.info("Emby订阅进度同步正在运行，本次跳过")
            return {"success": False, "message": "任务正在运行"}
        try:
            logger.info("开始执行 Emby订阅进度同步 ...")
            servers = self._target_servers()
            if not servers:
                logger.warning("未找到可用媒体服务器")
                return {"success": False, "message": "未找到可用媒体服务器"}

            states = "R,N" if self._include_new_state else "R"
            subscribes = SubscribeOper().list(states) or []
            exclude_ids = set(self._exclude_subscribes or [])
            tv_subscribes = [
                s for s in subscribes
                if s.type == MediaType.TV.value and s.id not in exclude_ids
            ]

            details: List[Dict[str, Any]] = []
            updated = 0
            completed = 0
            for subscribe in tv_subscribes:
                detail = self._sync_one(subscribe, servers)
                if not detail:
                    continue
                details.append(detail)
                if detail.get("changed"):
                    updated += 1
                if detail.get("completed"):
                    completed += 1

            summary = {
                "success": True,
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "servers": [server.get("name") for server in servers],
                "total": len(tv_subscribes),
                "updated": updated,
                "completed": completed,
                "details": details[:100],
            }
            self.save_data("last_result", summary)
            logger.info(
                f"Emby订阅进度同步完成：检查 {len(tv_subscribes)} 个订阅，"
                f"更新 {updated} 个，完整 {completed} 个"
            )
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【Emby订阅进度同步】执行完成",
                    text=f"检查 {len(tv_subscribes)} 个订阅，更新 {updated} 个，完整 {completed} 个。"
                )
            return summary
        except Exception as e:
            logger.error(f"Emby订阅进度同步出错：{e}", exc_info=True)
            return {"success": False, "message": str(e)}
        finally:
            self._lock.release()

    def api_sync(self) -> Dict[str, Any]:
        return self.sync_subscribe_progress()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/sync",
                "endpoint": self.api_sync,
                "methods": ["GET"],
                "summary": "同步订阅进度"
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        server_items = [
            {"title": config.name, "value": config.name}
            for config in MediaServerHelper().get_configs().values()
        ]
        subscribe_items = []
        try:
            for subscribe in SubscribeOper().list("N,R") or []:
                if subscribe.type != MediaType.TV.value:
                    continue
                season = f" S{int(subscribe.season or 1):02d}"
                progress = ""
                if subscribe.total_episode:
                    done = max(int(subscribe.total_episode or 0) - int(subscribe.lack_episode or 0), 0)
                    progress = f" [{done}/{subscribe.total_episode}]"
                subscribe_items.append({
                    "title": f"{subscribe.name}{season}{progress} - ID:{subscribe.id}",
                    "value": subscribe.id
                })
        except Exception as e:
            logger.warning(f"读取订阅列表失败：{e}")

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
                                        "model": "enabled", "label": "启用定时同步"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "onlyonce", "label": "立即运行一次"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "notify", "label": "执行后通知"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "include_new_state", "label": "包含新建订阅"
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
                                        "model": "cron",
                                        "label": "定时同步Cron",
                                        "placeholder": "15 3 * * *",
                                        "hint": "留空则不创建定时任务"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSelect", "props": {
                                        "model": "mediaservers",
                                        "label": "媒体服务器",
                                        "items": server_items,
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "hint": "不选择则使用所有已启用媒体服务器"
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "overwrite_note",
                                        "label": "覆盖已完成集数",
                                        "hint": "关闭时只把媒体库已有集数合并到订阅note"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "finish_completed",
                                        "label": "完整后移至历史"
                                    }}
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSelect", "props": {
                                        "model": "exclude_subscribes",
                                        "label": "排除订阅",
                                        "items": subscribe_items,
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True
                                    }}
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "15 3 * * *",
            "notify": False,
            "finish_completed": False,
            "overwrite_note": False,
            "include_new_state": True,
            "mediaservers": [],
            "exclude_subscribes": [],
        }

    def get_page(self) -> Optional[List[dict]]:
        result = self.get_data("last_result") or {}
        details = result.get("details") or []
        rows = []
        for detail in details[:30]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(detail.get("id", ""))},
                    {"component": "td", "text": f"{detail.get('name', '')} S{int(detail.get('season') or 1):02d}"},
                    {"component": "td", "text": ",".join([f"E{ep:02d}" for ep in detail.get("existing") or []])},
                    {"component": "td", "text": str(detail.get("lack_episode", ""))},
                    {"component": "td", "text": detail.get("status", "")},
                ]
            })

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "text": (
                                    f"上次执行：{result.get('time', '暂无')}；"
                                    f"检查 {result.get('total', 0)} 个，"
                                    f"更新 {result.get('updated', 0)} 个，"
                                    f"完整 {result.get('completed', 0)} 个。"
                                )
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VTable",
                "content": [
                    {
                        "component": "thead",
                        "content": [{
                            "component": "tr",
                            "content": [
                                {"component": "th", "text": "ID"},
                                {"component": "th", "text": "订阅"},
                                {"component": "th", "text": "媒体库已有"},
                                {"component": "th", "text": "缺失"},
                                {"component": "th", "text": "状态"},
                            ]
                        }]
                    },
                    {"component": "tbody", "content": rows}
                ]
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "EmbySubProgress",
                    "name": "Emby订阅进度同步",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_subscribe_progress,
                    "kwargs": {}
                }]
            except Exception as e:
                logger.error(f"Emby订阅进度同步 Cron 配置无效：{e}")
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

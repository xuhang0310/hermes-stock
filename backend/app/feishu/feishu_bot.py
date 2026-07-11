"""飞书机器人（自建应用）长连接网关：数字人 1↔1 飞书应用，私聊 / 群 @ 即由对应数字人应答。

运行时设计（针对 lark-oapi 1.7.x 的关键约束）：
- SDK 的 ws/client.py 在模块导入时捕获全局 event loop，且 Client.start() 会用
  run_until_complete 永久阻塞该 loop —— 多客户端 / 在 uvicorn 循环内导入都会炸。
  因此这里起一条专属事件循环线程，并保证 lark_oapi 的**首次 import 发生在该线程内**
  （SDK 全局 loop 从而绑定到我们的循环），随后所有 ws 客户端以协程方式共存在这条循环上，
  绕开阻塞式 start()，停用时真正 await 私有 _disconnect() 并关闭自动重连。
  依赖已在 requirements 锁定 lark-oapi==1.7.1（内部 API 兼容性以该版本为准）。
- 事件回调必须秒回：on_event 只做去重与过滤，立即派发工作线程；
  工作线程先发「正在分析…」占位卡，再无头调用数字人，最后 PATCH 更新同一张卡片。
- 发送 / 更新卡片用 httpx 直调 REST：tenant_access_token 按飞书返回的 expire 缓存，
  遇失效错误码清缓存重取一次。
- 群聊只应答 @ 本机器人；bot open_id 启动拉取失败后按节流惰性重试（不至于永久失聪）。
- App Secret 只存 Fernet 密文；本模块解密后仅驻内存，绝不进 LLM 上下文。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import threading
import time
from collections import deque
from datetime import date, datetime

import httpx
from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.feishu.feishu import decrypt_value
from backend.app.core.models import DigitalHumanProfile, SystemLog
from backend.app.scheduler.rhythm_scheduler import _build_digital_human_context
from backend.app.core.settings import settings


FEISHU_BASE = "https://open.feishu.cn"
CARD_CONTENT_LIMIT = 4000
_HISTORY_TURNS = 8
_DEDUP_MAX = 2000
_OPEN_ID_RETRY_SECONDS = 60
_MAX_INFLIGHT = 8
_TOKEN_INVALID_CODES = {99991661, 99991663, 99991664, 99991668}
_MENTION_TOKEN = re.compile(r"@_user_\d+")

# 全局并发上限：所有机器人共享，避免多人同时提问打爆 LLM
_agent_slots = threading.Semaphore(2)
_inflight = 0
_inflight_lock = threading.Lock()


def _log(level: str, message: str, context: dict | None = None) -> None:
    from backend.app.core.applog import log_event

    log_event("feishu_bot", level, message, context=context)


def _card(title: str, body: str, template: str, note: str | None = None) -> str:
    text = body.strip()
    if len(text) > CARD_CONTENT_LIMIT:
        text = text[:CARD_CONTENT_LIMIT] + "\n……内容过长已截断"
    elements: list[dict] = [{"tag": "markdown", "content": text}]
    if note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title[:120]}, "template": template},
            "elements": elements,
        },
        ensure_ascii=False,
    )


def fetch_tenant_token(app_id: str, app_secret: str) -> tuple[str | None, int, str]:
    """换取 tenant_access_token。返回 (token, expire秒, 错误文案)。"""
    try:
        response = httpx.post(
            f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except Exception as exc:  # noqa: BLE001
        return None, 0, f"网络错误: {str(exc)[:200]}"
    try:
        data = response.json()
    except json.JSONDecodeError:
        return None, 0, f"HTTP {response.status_code}: 非 JSON 响应"
    if data.get("code") != 0:
        return None, 0, f"飞书返回 code={data.get('code')}: {str(data.get('msg'))[:200]}"
    return str(data.get("tenant_access_token")), int(data.get("expire") or 0), ""


def fetch_bot_info(token: str) -> dict:
    """获取机器人信息（open_id / 名称），用于群聊 @ 识别与测试反馈。"""
    try:
        response = httpx.get(
            f"{FEISHU_BASE}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"获取机器人信息失败: {str(exc)[:200]}"}
    bot = data.get("bot") or {}
    if data.get("code") != 0:
        return {"error": f"飞书返回 code={data.get('code')}: {str(data.get('msg'))[:200]}"}
    return {"open_id": bot.get("open_id"), "name": bot.get("app_name")}


# --------------------------------------------------------------------------- #
# 专属事件循环线程：lark_oapi 必须首次 import 于此线程（SDK 模块级 loop 绑定到这条循环）
# --------------------------------------------------------------------------- #
class _WsLoop:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.import_error: str | None = None

    def ensure(self, timeout: float = 60) -> bool:
        """确保循环线程就绪。首次调用会在专属线程内 import lark_oapi（protobuf 较重，
        冷进程可能耗时 10s+，故等待窗口放宽），后续调用瞬时返回。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._ready.wait(timeout)
                return self.loop is not None
            self._ready.clear()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    import lark_oapi  # noqa: F401  首次导入必须在本线程：SDK 全局 loop 由此绑定
                except Exception as exc:  # noqa: BLE001
                    self.import_error = str(exc)[:300]
                    self._ready.set()
                    return
                self.loop = loop
                self._ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws-loop")
            self._thread.start()
            self._ready.wait(timeout)
            return self.loop is not None

    def submit(self, coro):
        if self.loop is None:
            raise RuntimeError("飞书长连接事件循环未就绪")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


_ws_loop = _WsLoop()


class BotWorker:
    """一个数字人绑定对应的长连接工作单元（客户端以协程驻留在共享 ws 循环上）。"""

    def __init__(self, profile_id: int, code: str, name: str, app_id: str, app_secret: str):
        self.profile_id = profile_id
        self.code = code
        self.name = name
        self.app_id = app_id
        self._app_secret = app_secret
        self._client = None
        self._token: str | None = None
        self._token_expire = 0.0
        self._token_lock = threading.Lock()
        self._bot_open_id: str | None = None
        self._open_id_next_try = 0.0
        self._open_id_lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._history: dict[str, deque] = {}
        self.connected = False
        self.started_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.today_key = ""
        self.today_count = 0
        self.error: str | None = None
        self.stopping = False

    # ------------------------------------------------------------------ token
    def _tenant_token(self, force_refresh: bool = False) -> str | None:
        with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._token_expire:
                return self._token
            token, expire, error = fetch_tenant_token(self.app_id, self._app_secret)
            if token is None:
                self.error = error
                _log("ERROR", f"机器人[{self.name}] 获取 token 失败", {"error": error})
                return None
            self._token = token
            # 按飞书返回的真实有效期缓存，留 5 分钟余量（官方在剩余 >=30min 时会返回同一 token）
            self._token_expire = time.monotonic() + max(60, expire - 300)
            return token

    def _api_call(self, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None) -> dict | None:
        """带「token 失效自动重取一次」的飞书 REST 调用。返回响应 JSON（网络失败返回 None）。"""
        for attempt in (0, 1):
            token = self._tenant_token(force_refresh=attempt == 1)
            if not token:
                return None
            try:
                response = httpx.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                )
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                _log("ERROR", f"机器人[{self.name}] 飞书接口调用失败", {"url": url, "error": str(exc)[:200]})
                return None
            if data.get("code") in _TOKEN_INVALID_CODES and attempt == 0:
                with self._token_lock:
                    self._token = None
                continue
            return data
        return None

    def _ensure_bot_open_id(self) -> str | None:
        if self._bot_open_id:
            return self._bot_open_id
        token = self._tenant_token()
        if not token:
            return None
        info = fetch_bot_info(token)
        if info.get("error"):
            _log("WARNING", f"机器人[{self.name}] 获取 open_id 失败（群聊 @ 暂不可识别，将自动重试）", {"error": info["error"]})
            return None
        self._bot_open_id = info.get("open_id")
        return self._bot_open_id

    def _retry_open_id_throttled(self) -> None:
        """群聊场景 open_id 缺失时的节流自愈：至多每 60s 重试一次。"""
        with self._open_id_lock:
            now = time.monotonic()
            if now < self._open_id_next_try:
                return
            self._open_id_next_try = now + _OPEN_ID_RETRY_SECONDS
        threading.Thread(target=self._ensure_bot_open_id, daemon=True).start()

    def _send_card(self, chat_id: str, card_json: str) -> str | None:
        data = self._api_call(
            "POST",
            f"{FEISHU_BASE}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={"receive_id": chat_id, "msg_type": "interactive", "content": card_json},
        )
        if not data:
            return None
        if data.get("code") != 0:
            _log("ERROR", f"机器人[{self.name}] 发送卡片被拒", {"code": data.get("code"), "msg": str(data.get("msg"))[:200]})
            return None
        return (data.get("data") or {}).get("message_id")

    def _patch_card(self, message_id: str, card_json: str) -> bool:
        data = self._api_call(
            "PATCH",
            f"{FEISHU_BASE}/open-apis/im/v1/messages/{message_id}",
            json_body={"content": card_json},
        )
        if not data or data.get("code") != 0:
            if data:
                _log("ERROR", f"机器人[{self.name}] 更新卡片被拒", {"code": data.get("code"), "msg": str(data.get("msg"))[:200]})
            return False
        return True

    # -------------------------------------------------------------- 事件处理
    def _dedup(self, message_id: str) -> bool:
        now = time.monotonic()
        with self._seen_lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = now
            if len(self._seen) > _DEDUP_MAX:
                cutoff = sorted(self._seen.values())[len(self._seen) // 2]
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            return False

    def _bump_counter(self) -> None:
        today = date.today().isoformat()
        if self.today_key != today:
            self.today_key = today
            self.today_count = 0
        self.today_count += 1
        self.last_event_at = datetime.now()

    def on_event(self, data) -> None:  # noqa: ANN001 —— lark P2ImMessageReceiveV1
        """SDK 回调（运行在 ws 循环线程）：只做过滤与派发，必须立即返回。"""
        try:
            if self.stopping:
                return
            message = data.event.message
            message_id = message.message_id or ""
            if not message_id or self._dedup(message_id):
                return
            chat_id = message.chat_id or ""
            chat_type = message.chat_type or ""
            if chat_type == "group":
                bot_open_id = self._bot_open_id
                if not bot_open_id:
                    self._retry_open_id_throttled()
                    return
                mentioned = any(
                    (m.id and m.id.open_id == bot_open_id) for m in (message.mentions or [])
                )
                if not mentioned:
                    return
            if message.message_type != "text":
                threading.Thread(
                    target=self._send_card,
                    args=(chat_id, _card(self.name, "目前只支持文字消息，请用文字描述你的问题。", "grey")),
                    daemon=True,
                ).start()
                return
            try:
                question = json.loads(message.content or "{}").get("text", "")
            except json.JSONDecodeError:
                question = ""
            question = _MENTION_TOKEN.sub("", question).strip()
            if not question:
                return
            global _inflight
            with _inflight_lock:
                if _inflight >= _MAX_INFLIGHT:
                    threading.Thread(
                        target=self._send_card,
                        args=(chat_id, _card(self.name, "当前提问较多，队列已满，请稍后再试。", "grey")),
                        daemon=True,
                    ).start()
                    return
                _inflight += 1
            self._bump_counter()
            threading.Thread(target=self._handle, args=(chat_id, question), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            _log("ERROR", f"机器人[{self.name}] 事件处理异常", {"error": str(exc)[:300]})

    def _handle(self, chat_id: str, question: str) -> None:
        global _inflight
        try:
            placeholder_id = self._send_card(
                chat_id,
                _card(f"{self.name} 正在分析…", "已收到问题，正在检索与分析，完成后本条消息会更新为结论。", "wathet"),
            )
            history = list(self._history.get(chat_id) or [])
            with _agent_slots:
                if self.stopping:
                    return
                try:
                    context = _build_digital_human_context(self.code)
                    answer, _model, error = _consume_agent_with_history(question, history, context, self.code)
                except Exception as exc:  # noqa: BLE001
                    answer, error = "", str(exc)[:500]
            if error or not answer:
                final = _card(
                    f"{self.name} · 分析失败",
                    f"抱歉，本次分析失败：{error or '模型未返回内容'}\n请稍后重试。",
                    "red",
                )
            else:
                bucket = self._history.setdefault(chat_id, deque(maxlen=_HISTORY_TURNS * 2))
                bucket.append({"role": "user", "content": question})
                bucket.append({"role": "assistant", "content": answer})
                final = _card(
                    f"{self.name} · {date.today().isoformat()}",
                    answer,
                    "blue",
                    note="仅供研究与复盘 · 不构成投资建议",
                )
            if placeholder_id:
                if not self._patch_card(placeholder_id, final):
                    self._send_card(chat_id, final)
            else:
                self._send_card(chat_id, final)
        finally:
            with _inflight_lock:
                _inflight -= 1

    # -------------------------------------------------------------- 生命周期
    def start(self) -> str | None:
        """在共享 ws 循环上建立长连接。返回错误文案（None=已提交连接）。"""
        if not _ws_loop.ensure():
            return _ws_loop.import_error or "飞书长连接事件循环启动失败"
        import lark_oapi as lark  # 模块已在 ws 循环线程完成首次导入，这里仅取引用

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_event)
            .build()
        )
        self._client = lark.ws.Client(
            self.app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        try:
            future = _ws_loop.submit(self._client._connect())  # noqa: SLF001 —— 见文件头运行时设计
        except Exception as exc:  # noqa: BLE001
            return f"提交连接失败: {str(exc)[:200]}"

        def _done(f) -> None:
            exc = f.exception()
            if exc is not None:
                if not self.stopping:
                    self.error = str(exc)[:300]
                    _log("ERROR", f"机器人[{self.name}] 长连接建立失败", {"error": self.error})
            else:
                self.connected = True
                self.started_at = datetime.now()
                self.error = None
                _log("INFO", f"机器人[{self.name}] 长连接已建立", {"app_id": self.app_id[:8] + "****"})
                threading.Thread(target=self._ensure_bot_open_id, daemon=True).start()

        future.add_done_callback(_done)

        def _watchdog() -> None:
            time.sleep(20)
            if self.connected or self.stopping:
                return
            state = "pending"
            if future.done():
                exc = future.exception()
                state = f"done, exc={type(exc).__name__ if exc else None}: {str(exc)[:200] if exc else '无'}"
            self.error = f"连接 20 秒未完成（future={state}）"
            _log("ERROR", f"机器人[{self.name}] 连接看门狗超时", {"future": state, "loop_alive": _ws_loop.loop is not None})

        threading.Thread(target=_watchdog, daemon=True).start()
        return None

    def stop(self) -> None:
        self.stopping = True
        self.connected = False
        client = self._client
        self._client = None
        if client is None or _ws_loop.loop is None:
            return
        try:
            setattr(client, "_auto_reconnect", False)  # 阻止 SDK 断开后自动重连复活
        except Exception:  # noqa: BLE001
            pass
        try:
            _ws_loop.submit(client._disconnect()).result(timeout=5)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass

    @property
    def running(self) -> bool:
        return bool(self.connected and not self.stopping)


def _consume_agent_with_history(question: str, history: list[dict], context: str | None, code: str | None):
    """带多轮上下文的无头调用：消费 run_agent_events 生成器收集最终答案。"""
    from backend.app.agents.stock_agent import run_agent_events

    buffer: list[str] = []
    answer = ""
    model_name = None
    error = None
    generator = run_agent_events(question, history, context, None, "research", None, code)
    try:
        for event in generator:
            kind = event.get("type")
            if kind == "model_selected":
                model_name = (event.get("model") or {}).get("model_name")
            elif kind == "content_delta":
                buffer.append(str(event.get("content") or ""))
            elif kind == "content_end":
                if event.get("role") == "answer":
                    answer = "".join(buffer).strip()
                buffer.clear()
            elif kind == "error":
                error = str(event.get("content") or "模型响应失败")
                break
    finally:
        generator.close()
    return answer, model_name, error


# ------------------------------------------------------------------ 管理器
_workers: dict[int, BotWorker] = {}
_spawn_errors: dict[int, str] = {}  # 最近一次 spawn 失败原因（worker 不存在时前端可见）
_manager_lock = threading.Lock()


def _sdk_available() -> bool:
    return importlib.util.find_spec("lark_oapi") is not None


def _load_bindings() -> list[DigitalHumanProfile]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(DigitalHumanProfile).where(
                    DigitalHumanProfile.feishu_bot_enabled.is_(True),
                    DigitalHumanProfile.feishu_app_id.is_not(None),
                )
            ).all()
        )


def _spawn(profile: DigitalHumanProfile) -> str | None:
    """启动一个绑定的 worker；返回错误文案（None=成功提交）。调用方需持有 _manager_lock。"""
    secret = decrypt_value(profile.feishu_app_secret_cipher)
    if not profile.feishu_app_id or not secret:
        error = "App Secret 缺失或密文解密失败，请重新保存绑定"
        _spawn_errors[profile.id] = error
        return error
    worker = BotWorker(profile.id, profile.code, profile.name, profile.feishu_app_id, secret)
    error = worker.start()
    if error:
        _spawn_errors[profile.id] = error
        return error
    _workers[profile.id] = worker
    _spawn_errors.pop(profile.id, None)
    return None


def start_feishu_bots() -> None:
    """lifespan 入口：后台线程拉起全部启用绑定（首次 import lark 较慢，不能阻塞 uvicorn 启动）。"""
    if not settings.feishu_bot_enabled:
        return
    if not _sdk_available():
        _log("ERROR", "未安装 lark-oapi，飞书机器人未启动（pip install lark-oapi）")
        return

    def _boot() -> None:
        with _manager_lock:
            for profile in _load_bindings():
                if profile.id not in _workers:
                    error = _spawn(profile)
                    if error:
                        _log("ERROR", f"机器人[{profile.name}] 启动失败", {"error": error})

    threading.Thread(target=_boot, daemon=True, name="feishu-bot-boot").start()


def stop_feishu_bots() -> None:
    with _manager_lock:
        for worker in list(_workers.values()):
            worker.stop()
        _workers.clear()
        _spawn_errors.clear()


def restart_bot(profile_id: int) -> str | None:
    """绑定配置变更后重启对应 worker；返回错误文案（None=成功或已按停用处理）。"""
    if not settings.feishu_bot_enabled:
        return "飞书机器人功能未启用（STOCK_FEISHU_BOT_ENABLED=false）"
    if not _sdk_available():
        return "服务器未安装 lark-oapi"
    with _manager_lock:
        old = _workers.pop(profile_id, None)
        if old is not None:
            old.stop()
        with SessionLocal() as session:
            profile = session.get(DigitalHumanProfile, profile_id)
        if profile is None or not profile.feishu_bot_enabled or not profile.feishu_app_id:
            _spawn_errors.pop(profile_id, None)
            return None
        error = _spawn(profile)
        if error:
            _log("ERROR", f"机器人[{profile.name}] 启动失败", {"error": error})
        return error


def bot_runtime(profile_id: int) -> dict:
    worker = _workers.get(profile_id)
    if worker is None:
        return {"running": False, "today_count": 0, "last_event_at": None, "error": _spawn_errors.get(profile_id)}
    return {
        "running": worker.running,
        "today_count": worker.today_count if worker.today_key == date.today().isoformat() else 0,
        "last_event_at": worker.last_event_at.isoformat() if worker.last_event_at else None,
        "error": worker.error,
    }

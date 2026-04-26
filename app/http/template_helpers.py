from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


PRODUCT_NAME = "极速收件箱"
ADMIN_PRODUCT_NAME = "极速收件箱管理台"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_ADMIN_ROLE_LABELS = {
    "superadmin": "超级管理员",
    "operator": "运维成员",
    "viewer": "只读访客",
}

_PARSE_STATUS_LABELS = {
    "pending": "解析中",
    "parsed": "已解析",
    "failed": "解析失败",
}

_API_KEY_KIND_LABELS = {
    "admin": "管理员",
    "service": "服务",
    "public": "公开访问",
}

_API_KEY_STATUS_LABELS = {
    "active": "可用",
    "revoked": "已吊销",
    "expired": "已过期",
    "disabled": "已停用",
}

_API_KEY_SCOPE_LABELS = {
    "public.read": "公开邮件读取",
    "live.read": "实时会话查看",
    "domains.read": "域名只读",
    "domains.write": "域名管理",
    "mailboxes.write": "邮箱管理",
    "messages.write": "邮件重解析",
    "audit.read": "审计日志读取",
    "system.read": "系统设置只读",
    "system.write": "系统设置修改",
    "api_keys.write": "API 密钥管理",
}

_DNS_STATUS_LABELS = {
    "unknown": "未检查",
    "ok": "正常",
    "warning": "警告",
    "error": "异常",
}

_AUDIT_STATUS_LABELS = {
    "success": "成功",
    "failure": "失败",
    "error": "异常",
    "denied": "拒绝",
}

_ACTOR_TYPE_LABELS = {
    "admin": "管理员",
    "api_key": "API 密钥",
    "system": "系统",
    "anonymous": "匿名访客",
}

_SESSION_STATUS_LABELS = {
    "open": "进行中",
    "closed": "已关闭",
    "error": "异常",
}

_EVENT_TYPE_LABELS = {
    "rcpt_accepted": "收件人已接受",
    "rcpt_rejected": "收件人被拒绝",
    "queued": "邮件已入队",
}

_RESOURCE_TYPE_LABELS = {
    "domain": "域名",
    "mailbox": "邮箱",
    "message": "邮件",
    "mail_store": "邮件存储",
    "api_key": "API 密钥",
    "system_settings": "系统设置",
}

_ACTION_LABELS = {
    "domains.dns_check": "执行 DNS 检查",
    "domains.create": "创建域名",
    "mailboxes.update": "更新邮箱",
    "mail.clear_all": "清除所有邮件",
    "messages.reparse": "重新解析邮件",
    "settings.update": "更新系统设置",
    "api_keys.create": "创建 API 密钥",
    "api_keys.revoke": "吊销 API 密钥",
}

_PLUS_ADDRESSING_LABELS = {
    "keep": "保留原样",
    "strip": "去除加号标签",
}


def _translate(value: Any, labels: dict[str, str], *, fallback: str = "未知") -> str:
    key = str(value or "").strip().lower()
    if not key:
        return fallback
    return labels.get(key, str(value))


def cn_bool(value: Any, *, yes: str = "是", no: str = "否") -> str:
    return yes if bool(value) else no


def cn_text(value: Any, default: str = "无") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value if value.strip() else default
    return str(value)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SHANGHAI_TZ)


def cn_datetime(value: Any, default: str = "无") -> str:
    dt = _coerce_datetime(value)
    if dt is None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def cn_time(value: Any, default: str = "--:--:--") -> str:
    dt = _coerce_datetime(value)
    if dt is None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return str(value)
    return dt.strftime("%H:%M:%S")


def cn_admin_role(value: Any) -> str:
    return _translate(value, _ADMIN_ROLE_LABELS)


def cn_parse_status(value: Any) -> str:
    return _translate(value, _PARSE_STATUS_LABELS)


def cn_api_key_kind(value: Any) -> str:
    return _translate(value, _API_KEY_KIND_LABELS)


def cn_api_key_status(value: Any) -> str:
    return _translate(value, _API_KEY_STATUS_LABELS)


def cn_api_key_scope(value: Any) -> str:
    return _translate(value, _API_KEY_SCOPE_LABELS)


def cn_dns_status(value: Any) -> str:
    return _translate(value, _DNS_STATUS_LABELS)


def cn_audit_status(value: Any) -> str:
    return _translate(value, _AUDIT_STATUS_LABELS)


def cn_actor_type(value: Any) -> str:
    return _translate(value, _ACTOR_TYPE_LABELS)


def cn_session_status(value: Any) -> str:
    return _translate(value, _SESSION_STATUS_LABELS)


def cn_event_type(value: Any) -> str:
    return _translate(value, _EVENT_TYPE_LABELS)


def cn_resource_type(value: Any) -> str:
    return _translate(value, _RESOURCE_TYPE_LABELS)


def cn_action(value: Any) -> str:
    return _translate(value, _ACTION_LABELS)


def cn_plus_addressing_mode(value: Any) -> str:
    return _translate(value, _PLUS_ADDRESSING_LABELS)


def register_template_helpers(templates: Any) -> None:
    templates.env.globals.update(
        product_name=PRODUCT_NAME,
        admin_product_name=ADMIN_PRODUCT_NAME,
        cn_bool=cn_bool,
        cn_text=cn_text,
        cn_datetime=cn_datetime,
        cn_time=cn_time,
        cn_admin_role=cn_admin_role,
        cn_parse_status=cn_parse_status,
        cn_api_key_kind=cn_api_key_kind,
        cn_api_key_status=cn_api_key_status,
        cn_api_key_scope=cn_api_key_scope,
        cn_dns_status=cn_dns_status,
        cn_audit_status=cn_audit_status,
        cn_actor_type=cn_actor_type,
        cn_session_status=cn_session_status,
        cn_event_type=cn_event_type,
        cn_resource_type=cn_resource_type,
        cn_action=cn_action,
        cn_plus_addressing_mode=cn_plus_addressing_mode,
    )

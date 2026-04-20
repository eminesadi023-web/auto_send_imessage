from __future__ import annotations

import json
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - 仅在未安装依赖时触发
    OpenAI = None  # type: ignore[assignment]

# 自定义异常，用于iMessage发送失败时抛出
class IMessageSendError(RuntimeError):
    """当iMessage发送失败时抛出该异常"""


def _escape_applescript_string(value: str) -> str:
    """对AppleScript字符串进行转义"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_attachment_path(attachment: str) -> str:
    attachment_path = Path(attachment).expanduser()
    if not attachment_path.is_absolute():
        attachment_path = Path(__file__).resolve().parent / attachment_path
    attachment_path = attachment_path.resolve()
    if not attachment_path.exists():
        raise IMessageSendError(f"图片文件不存在: {attachment_path}")
    return str(attachment_path)


def _send_imessage_script(phone: str, message: str | None = None, attachment: str | None = None) -> str:
    """构造 AppleScript 发送 iMessage，支持文本和附件"""
    escaped_phone = _escape_applescript_string(phone)
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{escaped_phone}" of targetService
'''
    if attachment:
        escaped_attachment = _escape_applescript_string(_normalize_attachment_path(attachment))
        script += f'    send (POSIX file "{escaped_attachment}") to targetBuddy\n'
    if message:
        escaped_message = _escape_applescript_string(message)
        script += f'    send "{escaped_message}" to targetBuddy\n'
    script += 'end tell'
    return script


def _send_imessage_direct(phone: str, message: str | None = None, attachment: str | None = None) -> None:
    """直接使用AppleScript发送iMessage"""
    if sys.platform != "darwin":
        raise IMessageSendError("iMessage发送仅支持macOS")
    ensure_contact_exists(phone)
    time.sleep(1)
    script = _send_imessage_script(phone, message, attachment)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise IMessageSendError("发送iMessage超时：AppleScript 在 30 秒内未完成。") from e
    except subprocess.CalledProcessError as e:
        raise IMessageSendError(f"发送iMessage失败: {e.stderr.decode('utf-8', errors='ignore')}")

def _add_to_contacts_script(phone: str, name: str = "iMessage Customer") -> str:
    """构造 AppleScript 将手机号添加到通讯录，以提高 iMessage 发送成功率"""
    escaped_phone = _escape_applescript_string(phone)
    escaped_name = _escape_applescript_string(name)
    return f'''
tell application "Contacts"
    set existingPeople to (every person whose value of phones contains "{escaped_phone}")
    if (count of existingPeople) is 0 then
        set newPerson to make new person with properties {{first name:"{escaped_name}", last name:""}}
        make new phone at end of phones of newPerson with properties {{label:"mobile", value:"{escaped_phone}"}}
        save
    end if
end tell
'''

def ensure_contact_exists(phone: str) -> bool:
    """确保手机号在通讯录中存在"""
    if sys.platform != "darwin":
        return False
    script = _add_to_contacts_script(phone)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False

# 发送风控参数配置，控制延迟、批次、每日上限等
@dataclass(slots=True)
class IMessageRiskControl:
    # 启动时初始等待秒数范围（防止批量密集启动）
    startup_delay_min_seconds: int = 20
    startup_delay_max_seconds: int = 90
    # 两次消息之间的最小、最大随机延迟
    min_delay_between_messages_seconds: int = 60
    max_delay_between_messages_seconds: int = 120
    # 每批次最大发送数量，及批次间的暂停范围（秒）
    batch_size: int = 5
    batch_pause_min_seconds: int = 1800
    batch_pause_max_seconds: int = 1800
    # 每日最多可发送消息数量
    daily_limit: int = 100
    # 同一手机号发送后冷却周期（小时）
    cooldown_hours: int = 72
    # 允许自动发送的时间窗口（小时）
    active_hours_start: int = 0
    active_hours_end: int = 24

# 单个手机号的发送结果
@dataclass(slots=True)
class SendResult:
    phone: str           # 发送手机号
    status: str          # 状态：sent，failed，skipped，dry_run 等
    detail: str          # 详细描述（出错原因、跳过原因、成功标识）
    error: str | None = None  # 失败时的错误信息（可选）


@dataclass(slots=True)
class DeliveryCheckResult:
    status: str
    detail: str
    error: str | None = None
    raw_error: str | None = None


APPLE_EPOCH_UNIX_SECONDS = 978307200


class IMessageLLMRewriter:
    """基于 OpenAI 兼容接口对 iMessage 文案做语义一致改写。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 20,
        temperature: float = 0.9,
    ) -> None:
        if OpenAI is None:
            raise IMessageSendError("未安装 openai 依赖，请先安装 requirements.txt 中的新依赖。")
        self._client = OpenAI(
            api_key=api_key.strip(),
            base_url=base_url.strip(),
            timeout=max(timeout_seconds, 1),
        )
        self._model = model.strip()
        self._temperature = max(0.0, min(temperature, 1.8))

    def rewrite(self, original_text: str) -> str:
        source = original_text.strip()
        if not source:
            return original_text
        prompt = (
            "你是 iMessage 文案优化助手。请改写下面的消息，要求：\n"
            "1) 保持核心主题和意图不变，必须与原文契合；\n"
            "2) 用词、语序、句式随机变化，避免重复模板感；\n"
            "3) 保持自然、礼貌、可直接发送；\n"
            "4) 禁止添加解释、标题、引号、编号、前后缀说明；\n"
            "5) 尽量保持与原文接近的长度与段落结构。\n\n"
            f"原文：\n{source}"
        )
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": "你只输出可直接发送的最终消息正文。"},
                {"role": "user", "content": prompt},
            ],
        )
        rewritten = (response.choices[0].message.content or "").strip()
        rewritten = rewritten.strip().strip('"').strip("'").strip()
        return rewritten or original_text


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _batch_record_key(recipient: str, message: str) -> str:
    return f"{recipient}\n{message}"


def _resolve_batch_paths(
    batch_root_dir: str | Path,
    *,
    batch_date: str | None = None,
) -> tuple[str, Path, Path]:
    if batch_date is None:
        batch_date = datetime.now().strftime("%Y-%m-%d")
    batch_dir = Path(batch_root_dir) / batch_date
    batch_dir.mkdir(parents=True, exist_ok=True)
    return batch_date, batch_dir / "results.json", batch_dir / "events.jsonl"


def _load_batch_results(batch_results_path: Path, batch_date: str) -> dict[str, Any]:
    if not batch_results_path.exists():
        return {"batch_date": batch_date, "updated_at": _now_iso(), "records": []}
    try:
        payload = json.loads(batch_results_path.read_text(encoding="utf-8"))
    except Exception:
        return {"batch_date": batch_date, "updated_at": _now_iso(), "records": []}
    records = payload.get("records", [])
    if not isinstance(records, list):
        records = []
    return {
        "batch_date": str(payload.get("batch_date", batch_date)),
        "updated_at": str(payload.get("updated_at", _now_iso())),
        "records": records,
    }


def _save_batch_results(
    batch_results_path: Path,
    *,
    batch_date: str,
    batch_records_by_key: dict[str, dict[str, Any]],
) -> None:
    records = sorted(
        batch_records_by_key.values(),
        key=lambda item: (
            str(item.get("recipient", "")),
            str(item.get("message", "")),
            str(item.get("updated_at", "")),
        ),
    )
    payload = {
        "batch_date": batch_date,
        "updated_at": _now_iso(),
        "records": records,
    }
    batch_results_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_batch_event(batch_events_path: Path, payload: dict[str, Any]) -> None:
    line = json.dumps(
        {
            "event_at": _now_iso(),
            **payload,
        },
        ensure_ascii=False,
    )
    with batch_events_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def _index_batch_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        recipient = str(record.get("recipient", "")).strip()
        message = str(record.get("message", ""))
        if not recipient:
            continue
        indexed[_batch_record_key(recipient, message)] = record
    return indexed


def _upsert_batch_record(
    batch_records_by_key: dict[str, dict[str, Any]],
    *,
    recipient: str,
    message: str,
    transport_status: str,
    delivery_status: str,
    detail: str,
    attempted_at: str,
    error: str | None = None,
    raw_error: str | None = None,
) -> None:
    key = _batch_record_key(recipient, message)
    existing = batch_records_by_key.get(key, {})
    previous_attempt_count = int(existing.get("attempt_count", 0) or 0)
    batch_records_by_key[key] = {
        "recipient": recipient,
        "message": message,
        "transport_status": transport_status,
        "delivery_status": delivery_status,
        "detail": detail,
        "error": error,
        "raw_error": raw_error,
        "attempted_at": existing.get("attempted_at", attempted_at),
        "last_checked_at": _now_iso(),
        "attempt_count": previous_attempt_count + 1,
        "updated_at": _now_iso(),
    }


def _normalize_identity(value: str | None) -> tuple[str, str]:
    text = (value or "").strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    return text, digits


def _recipient_matches(candidate: str | None, recipient: str) -> bool:
    candidate_text, candidate_digits = _normalize_identity(candidate)
    recipient_text, recipient_digits = _normalize_identity(recipient)
    if candidate_text and candidate_text == recipient_text:
        return True
    if candidate_digits and recipient_digits:
        return (
            candidate_digits == recipient_digits
            or candidate_digits.endswith(recipient_digits)
            or recipient_digits.endswith(candidate_digits)
        )
    return False


def _is_truthy_db_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized not in {"", "0", "false", "none", "null"}
    return True


def _is_failed_db_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized not in {"", "0", "none", "null"}
    return True


def _apple_to_unix_timestamp(value: object) -> float | None:
    if value is None:
        return None
    try:
        raw = float(value)
    except Exception:
        return None

    # chat.db 在不同版本中时间精度存在差异，做多尺度兜底转换。
    candidate_values = (
        raw + APPLE_EPOCH_UNIX_SECONDS,
        raw / 1_000 + APPLE_EPOCH_UNIX_SECONDS,
        raw / 1_000_000 + APPLE_EPOCH_UNIX_SECONDS,
        raw / 1_000_000_000 + APPLE_EPOCH_UNIX_SECONDS,
        raw,
        raw / 1_000,
        raw / 1_000_000,
        raw / 1_000_000_000,
    )
    for candidate in candidate_values:
        if 946684800 <= candidate <= 4102444800:
            return candidate
    return None


def _inspect_delivery_status_once(
    recipient: str,
    message: str,
    *,
    lookback_seconds: int,
) -> DeliveryCheckResult:
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if sys.platform != "darwin":
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail="当前非macOS环境，无法回查chat.db送达状态。",
        )
    if not db_path.exists():
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail=f"未找到消息数据库：{db_path}",
        )

    now_ts = time.time()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception as exc:
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail=f"打开chat.db失败：{exc}",
            error=str(exc),
            raw_error=str(exc),
        )

    try:
        conn.row_factory = sqlite3.Row
        table_info_rows = conn.execute("PRAGMA table_info(message)").fetchall()
        available_columns = {str(row["name"]) for row in table_info_rows}
        select_columns = [
            "m.ROWID AS rowid",
            "m.handle_id AS handle_id",
            "h.id AS handle_value",
        ]
        candidate_columns = (
            "text",
            "date",
            "date_delivered",
            "date_read",
            "is_delivered",
            "is_sent",
            "is_finished",
            "error",
            "service",
            "guid",
        )
        for column_name in candidate_columns:
            if column_name in available_columns:
                select_columns.append(f"m.{column_name} AS {column_name}")
        query = (
            f"SELECT {', '.join(select_columns)} "
            "FROM message AS m "
            "LEFT JOIN handle AS h ON m.handle_id = h.ROWID "
            "WHERE m.is_from_me = 1 "
            "ORDER BY m.date DESC "
            "LIMIT 500"
        )
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    scored_candidates: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        handle_value = row["handle_value"] if "handle_value" in row.keys() else None
        if not _recipient_matches(str(handle_value) if handle_value is not None else None, recipient):
            continue
        created_ts = _apple_to_unix_timestamp(row["date"]) if "date" in row.keys() else None
        if created_ts is not None and now_ts - created_ts > lookback_seconds:
            continue

        score = 0.0
        text_value = row["text"] if "text" in row.keys() else None
        if isinstance(text_value, str):
            compared_text = text_value.strip()
            if compared_text == message:
                score += 8
            elif compared_text and (message in compared_text or compared_text in message):
                score += 4
        if created_ts is not None:
            score += max(0.0, 3 - ((now_ts - created_ts) / 30))
        if "is_sent" in row.keys() and _is_truthy_db_value(row["is_sent"]):
            score += 1
        scored_candidates.append((score, row))

    if not scored_candidates:
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail="chat.db中尚未匹配到该收件人近期发送记录。",
        )

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_row = scored_candidates[0]

    error_value = best_row["error"] if "error" in best_row.keys() else None
    if _is_failed_db_value(error_value):
        return DeliveryCheckResult(
            status="failed",
            detail=f"消息发送失败（chat.db error={error_value}）。",
            error=str(error_value),
            raw_error=str(error_value),
        )

    if "is_delivered" in best_row.keys() and _is_truthy_db_value(best_row["is_delivered"]):
        return DeliveryCheckResult(
            status="delivered",
            detail="消息已送达（chat.db is_delivered=1）。",
        )
    if "date_delivered" in best_row.keys() and _is_truthy_db_value(best_row["date_delivered"]):
        return DeliveryCheckResult(
            status="delivered",
            detail="消息已送达（chat.db date_delivered存在）。",
        )

    if "is_sent" in best_row.keys() and _is_truthy_db_value(best_row["is_sent"]):
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail="消息已发送，但尚未确认送达。",
        )
    if "is_finished" in best_row.keys() and _is_truthy_db_value(best_row["is_finished"]):
        return DeliveryCheckResult(
            status="sent_not_confirmed",
            detail="消息已完成发送流程，但未确认送达。",
        )

    return DeliveryCheckResult(
        status="sent_not_confirmed",
        detail="消息状态待确认（未观察到送达或失败信号）。",
    )


def _poll_delivery_status(
    recipient: str,
    message: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
    lookback_seconds: int,
) -> DeliveryCheckResult:
    timeout_seconds = max(timeout_seconds, 1)
    interval_seconds = max(interval_seconds, 1)
    deadline = time.time() + timeout_seconds
    last_observation = DeliveryCheckResult(
        status="sent_not_confirmed",
        detail="尚未开始状态回查。",
    )

    while True:
        last_observation = _inspect_delivery_status_once(
            recipient,
            message,
            lookback_seconds=lookback_seconds,
        )
        if last_observation.status in {"delivered", "failed"}:
            return last_observation
        if time.time() >= deadline:
            return DeliveryCheckResult(
                status="sent_not_confirmed",
                detail=(
                    f"在 {timeout_seconds} 秒内未拿到送达回执。"
                    f"最后观测：{last_observation.detail}"
                ),
                error=last_observation.error,
                raw_error=last_observation.raw_error,
            )
        time.sleep(interval_seconds)

# 对AppleScript字符串进行转义
def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

# 加载发送历史，返回历史消息记录字典
def _load_history(state_path: Path) -> dict[str, list[dict[str, str]]]:
    if not state_path.exists():
        return {"messages": []}
    return json.loads(state_path.read_text(encoding="utf-8"))

# 保存发送历史到指定路径
def _save_history(state_path: Path, payload: dict[str, list[dict[str, str]]]) -> None:
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

# 检查当前是否在允许的自动发送窗口内
def _can_send_now(rules: IMessageRiskControl, now: datetime) -> tuple[bool, str]:
    return True, ""

# 从发送历史中提取24小时内消息及正在冷却的手机号集合
def _recent_history_entries(
    history: dict[str, list[dict[str, str]]],
    *,
    now_ts: float,
    rules: IMessageRiskControl,
) -> tuple[list[dict[str, str]], set[str]]:
    recent_day_entries: list[dict[str, str]] = []
    cooling_down_phones: set[str] = set()
    for entry in history.get("messages", []):
        sent_ts = float(entry["timestamp"])
        if now_ts - sent_ts <= 24 * 60 * 60:
            recent_day_entries.append(entry)
        if now_ts - sent_ts <= rules.cooldown_hours * 60 * 60:
            cooling_down_phones.add(entry["phone"])
    return recent_day_entries, cooling_down_phones

# 构造AppleScript脚本用于直接向某手机号发送iMessage


# 向指定手机号发送一条iMessage（底层调用AppleScript）
def send_imessage_once(phone: str, message: str, attachment: str | None = None) -> None:
    _send_imessage_direct(phone, message, attachment)


def send_imessages(
    phones: list[str],
    message: str = "Hi",
    *,
    attachment: str | None = None,
    state_path: str | Path = ".imessage_send_history.json",
    normalize_phone_numbers: bool = True,
    batch_root_dir: str | Path = "imessage_batches",
    batch_date: str | None = None,
    delivery_check_timeout_seconds: int = 45,
    delivery_check_interval_seconds: int = 3,
) -> list[SendResult]:
    return send_imessages_with_risk_control(
        phones,
        message,
        attachment=attachment,
        state_path=state_path,
        normalize_phone_numbers=normalize_phone_numbers,
        batch_root_dir=batch_root_dir,
        batch_date=batch_date,
        delivery_check_timeout_seconds=delivery_check_timeout_seconds,
        delivery_check_interval_seconds=delivery_check_interval_seconds,
    )

# 带有防刷风控的iMessage批量发送主流程
def send_imessages_with_risk_control(
    phones: list[str],
    message: str = "Hi",
    *,
    attachment: str | None = None,
    dry_run: bool = False,
    rules: IMessageRiskControl | None = None,
    max_send_count: int | None = None,
    state_path: str | Path = ".imessage_send_history.json",
    normalize_phone_numbers: bool = True,
    batch_root_dir: str | Path = "imessage_batches",
    batch_date: str | None = None,
    delivery_check_timeout_seconds: int = 45,
    delivery_check_interval_seconds: int = 3,
    delivery_check_lookback_seconds: int = 600,
    llm_rewrite_enabled: bool = False,
    openai_base_url: str | None = None,
    openai_api_key: str | None = None,
    openai_model: str = "gemini-2.0-flash",
    openai_timeout_seconds: int = 20,
    openai_temperature: float = 0.9,
) -> list[SendResult]:
    # 采用传入的风控规则，否则用默认
    rules = rules or IMessageRiskControl()
    # 首先对手机号去重及规范化
    if normalize_phone_numbers:
        normalized_unique_phones = list(dict.fromkeys(_normalize_phone_number(phone) for phone in phones))
        unique_phones = [phone for phone in normalized_unique_phones if phone]
    else:
        unique_phones = list(dict.fromkeys((phone or "").strip() for phone in phones if (phone or "").strip()))
    state_file = Path(state_path)
    history = _load_history(state_file)
    batch_date, batch_results_path, batch_events_path = _resolve_batch_paths(
        batch_root_dir,
        batch_date=batch_date,
    )
    batch_payload = _load_batch_results(batch_results_path, batch_date)
    batch_records_by_key = _index_batch_records(batch_payload.get("records", []))
    now = datetime.now()
    # 检查时间窗口
    can_send, reason = _can_send_now(rules, now)
    if not can_send:
        raise IMessageSendError(reason)

    now_ts = time.time()
    recent_day_entries, cooling_down_phones = _recent_history_entries(history, now_ts=now_ts, rules=rules)
    if len(recent_day_entries) >= rules.daily_limit:
        raise IMessageSendError(
            f"今日已达发送上限：过去24小时内已发送{len(recent_day_entries)}条消息。"
        )

    # 计算本次实际可发送数量
    send_budget = rules.daily_limit - len(recent_day_entries)
    if max_send_count is not None:
        send_budget = min(send_budget, max_send_count)

    rewriter: IMessageLLMRewriter | None = None
    if llm_rewrite_enabled:
        if not openai_base_url or not openai_api_key:
            raise IMessageSendError("已启用 LLM 改写，但 OPENAI_BASE_URL 或 OPENAI_API_KEY 未配置。")
        rewriter = IMessageLLMRewriter(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=openai_model,
            timeout_seconds=openai_timeout_seconds,
            temperature=openai_temperature,
        )

    # 排除正在冷却期手机号，及本批次已送达的手机号（同一消息文本）
    queued_phones = [phone for phone in unique_phones if phone not in cooling_down_phones]
    skip_statuses_in_batch = {"delivered", "sent_not_confirmed"}
    already_processed_in_batch = {
        str(record.get("recipient", "")).strip()
        for record in batch_records_by_key.values()
        if str(record.get("delivery_status", "")) in skip_statuses_in_batch
        and (
            rewriter is not None
            or str(record.get("message", "")) == message
        )
    }

    results: list[SendResult] = []
    for phone in queued_phones:
        if phone in already_processed_in_batch:
            detail = f"跳过：该批次中收件人已存在非失败状态记录（{batch_date}）"
            results.append(SendResult(phone=phone, status="skipped_processed", detail=detail))
            _append_batch_event(
                batch_events_path,
                {
                    "event_type": "skip_processed",
                    "recipient": phone,
                    "message": message,
                    "detail": detail,
                },
            )

    queued_phones = [phone for phone in queued_phones if phone not in already_processed_in_batch]

    # dry_run模式，仅输出将会被发送的手机号和内容，不实际执行
    if dry_run:
        dry_run_results: list[SendResult] = []
        for phone in queued_phones[:send_budget]:
            message_to_send = message
            if rewriter is not None:
                try:
                    message_to_send = rewriter.rewrite(message)
                except Exception:
                    message_to_send = message
            dry_run_results.append(
                SendResult(phone=phone, status="dry_run", detail=f'将会发送 "{message_to_send}"')
            )
        return results + dry_run_results

    # 启动初期随机等待，进一步防止批量行为
    time.sleep(random.randint(rules.startup_delay_min_seconds, rules.startup_delay_max_seconds))

    sent_in_this_run = 0
    for index, phone in enumerate(queued_phones):
        if sent_in_this_run >= send_budget:
            results.append(SendResult(phone=phone, status="skipped", detail="跳过，因为本次发送额度已用尽"))
            continue

        # 每达到一个batch，长暂停一次
        if sent_in_this_run and sent_in_this_run % rules.batch_size == 0:
            batch_pause = random.randint(rules.batch_pause_min_seconds, rules.batch_pause_max_seconds)
            time.sleep(batch_pause)

        attempted_at = _now_iso()
        message_to_send = message
        if attachment is None and rewriter is not None:
            try:
                message_to_send = rewriter.rewrite(message)
            except Exception as rewrite_exc:
                _append_batch_event(
                    batch_events_path,
                    {
                        "event_type": "llm_rewrite_failed",
                        "recipient": phone,
                        "message": message,
                        "detail": f"LLM改写失败，回退原文：{rewrite_exc}",
                    },
                )
                message_to_send = message
        _append_batch_event(
            batch_events_path,
            {
                "event_type": "send_attempt",
                "recipient": phone,
                "message": message_to_send,
                "attempted_at": attempted_at,
            },
        )

        try:
            # 1. 尝试将联系人添加到通讯录（绕过风控的关键尝试）
            ensure_contact_exists(phone)
            time.sleep(1)  # 给通讯录同步留一点点时间

            send_imessage_once(phone, message_to_send, attachment)
            sent_in_this_run += 1
            # 记录本次发送历史
            history.setdefault("messages", []).append(
                {
                    "phone": phone,
                    "timestamp": str(time.time()),
                    "message": message_to_send,
                }
            )
            _save_history(state_file, history)
            delivery_status = _poll_delivery_status(
                phone,
                message_to_send,
                timeout_seconds=delivery_check_timeout_seconds,
                interval_seconds=delivery_check_interval_seconds,
                lookback_seconds=delivery_check_lookback_seconds,
            )
            results.append(
                SendResult(
                    phone=phone,
                    status=delivery_status.status,
                    detail=delivery_status.detail,
                    error=delivery_status.error,
                )
            )
            _append_batch_event(
                batch_events_path,
                {
                    "event_type": "delivery_status",
                    "recipient": phone,
                    "message": message_to_send,
                    "delivery_status": delivery_status.status,
                    "detail": delivery_status.detail,
                    "error": delivery_status.error,
                },
            )
            _upsert_batch_record(
                batch_records_by_key,
                recipient=phone,
                message=message_to_send,
                transport_status="sent",
                delivery_status=delivery_status.status,
                detail=delivery_status.detail,
                attempted_at=attempted_at,
                error=delivery_status.error,
                raw_error=delivery_status.raw_error,
            )
            _save_batch_results(
                batch_results_path,
                batch_date=batch_date,
                batch_records_by_key=batch_records_by_key,
            )
        except Exception as exc:  # noqa: BLE001
            error_detail = str(exc)
            results.append(
                SendResult(
                    phone=phone,
                    status="failed",
                    detail=error_detail,
                    error=error_detail,
                )
            )
            _append_batch_event(
                batch_events_path,
                {
                    "event_type": "send_failed",
                    "recipient": phone,
                    "message": message_to_send,
                    "detail": error_detail,
                },
            )
            _upsert_batch_record(
                batch_records_by_key,
                recipient=phone,
                message=message_to_send,
                transport_status="failed",
                delivery_status="failed",
                detail=error_detail,
                attempted_at=attempted_at,
                error=error_detail,
                raw_error=error_detail,
            )
            _save_batch_results(
                batch_results_path,
                batch_date=batch_date,
                batch_records_by_key=batch_records_by_key,
            )

        # 下一个收件人前等待，除非已经满额或最后一个
        if index < len(queued_phones) - 1 and sent_in_this_run < send_budget:
            delay_seconds = random.randint(
                rules.min_delay_between_messages_seconds,
                rules.max_delay_between_messages_seconds,
            )
            time.sleep(delay_seconds)

    return results

# 规范化手机号，仅保留数字，去除特殊字符。长度不足7位为无效号。
def _normalize_phone_number(phone: str | None) -> str | None:
    if not phone:
        return None
    stripped = phone.strip()
    if not stripped:
        return None
    has_plus = stripped.startswith("+")
    digits = "".join(ch for ch in stripped if ch.isdigit())
    if len(digits) < 7:
        return None
    return f"+{digits}" if has_plus else digits

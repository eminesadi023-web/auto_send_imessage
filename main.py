from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config import APP_CONFIG
from dingtalk_api import (
    init_bot as init_dingtalk_bot,
    send_markdown_to_group as dingtalk_send_markdown_to_group,
    send_markdown_to_user as dingtalk_send_markdown_to_user,
)
from imessage_sender import send_imessages_with_risk_control
from lingxing_result import (
    create_lingxing_session_from_login,
    export_and_download_order_management_report,
    extract_phone_numbers_from_order_records,
    parse_order_management_export_file,
    switch_lingxing_to_multi_platform,
)


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="自动发送 iMessage（支持指定历史批次重跑）",
    )
    parser.add_argument(
        "rerun_batch_date",
        nargs="?",
        help="可选：指定重跑批次日期（YYYY-MM-DD），例如 2026-04-14",
    )
    parser.add_argument(
        "--rerun-batch-date",
        dest="rerun_batch_date_flag",
        help="指定重跑批次日期（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--rerun-include-statuses",
        default="",
        help="重跑纳入状态，逗号分隔（例如 failed,sent_not_confirmed）",
    )
    return parser.parse_args()


def _resolve_rerun_batch_date_from_sources(
    *,
    cli_args: argparse.Namespace,
) -> str | None:
    candidate = cli_args.rerun_batch_date_flag or cli_args.rerun_batch_date
    if not candidate:
        return None
    candidate = candidate.strip()
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"批次日期格式必须为 YYYY-MM-DD，当前值: {candidate}") from exc
    return candidate


def _resolve_rerun_include_statuses_from_sources(
    *,
    cli_args: argparse.Namespace,
) -> tuple[str, ...]:
    if cli_args.rerun_include_statuses.strip():
        items = _parse_csv_values(cli_args.rerun_include_statuses)
        lowered = tuple(dict.fromkeys(item.lower() for item in items))
        if not lowered:
            raise ValueError("--rerun-include-statuses 不能为空")
        return lowered
    return ("failed",)


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _get_optional_int_env(name: str) -> int | None:
    raw = _get_optional_env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值: {raw}") from exc


def _get_optional_float_env(name: str) -> float | None:
    raw = _get_optional_env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字，当前值: {raw}") from exc


def _get_optional_bool_env(name: str) -> bool | None:
    raw = _get_optional_env(name)
    if raw is None:
        return None
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} 必须是布尔值（true/false），当前值: {raw}")


def _parse_csv_values(raw: str) -> list[str]:
    normalized = raw.replace("，", ",").replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _load_lingxing_web_login_config() -> dict[str, str] | None:
    account = _get_optional_env("LINGXING_WEB_ACCOUNT")
    password = _get_optional_env("LINGXING_WEB_PASSWORD")

    if not account and not password:
        return None
    if not account or not password:
        raise ValueError(
            "LINGXING_WEB_ACCOUNT 和 LINGXING_WEB_PASSWORD 需要同时配置，或同时留空"
        )

    return {
        "account": account,
        "password": password,
    }


def _load_imessage_test_usernames() -> list[str]:
    raw = os.getenv("IMESSAGE_TEST_USERNAMES", "").strip()
    if not raw:
        return []
    return _parse_csv_values(raw)


def _load_dingtalk_notify_config() -> dict[str, str] | None:
    app_key = _get_optional_env("DINGTALK_APP_KEY")
    app_secret = _get_optional_env("DINGTALK_APP_SECRET")
    robot_code = _get_optional_env("DINGTALK_ROBOT_CODE")
    group_conversation_id = _get_optional_env("DINGTALK_GROUP_CONVERSATION_ID")
    user_name = _get_optional_env("DINGTALK_NOTIFY_USER_NAME")
    if not user_name:
        # 兼容历史变量名
        user_name = _get_optional_env("DINGTALK_NOTIFY_USER")
    user_id = _get_optional_env("DINGTALK_NOTIFY_USER_ID")

    has_any = any([app_key, app_secret, robot_code, group_conversation_id, user_name, user_id])
    if not has_any:
        return None

    if not app_key or not app_secret or not robot_code:
        raise ValueError(
            "启用钉钉通知时，需要同时配置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET / DINGTALK_ROBOT_CODE"
        )

    if not group_conversation_id and not user_name and not user_id:
        raise ValueError(
            "启用钉钉通知时，至少配置一个通知目标："
            "DINGTALK_GROUP_CONVERSATION_ID 或 DINGTALK_NOTIFY_USER_NAME / DINGTALK_NOTIFY_USER_ID"
        )

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "robot_code": robot_code,
        "group_conversation_id": group_conversation_id or "",
        "notify_user_name": user_name or "",
        "notify_user_id": user_id or "",
    }


def _load_runtime_app_config() -> Any:
    overrides: dict[str, Any] = {}

    imessage_text_override = _get_optional_env("IMESSAGE_TEXT")
    if imessage_text_override:
        # 支持在 .env 中用 \n 表示换行
        overrides["imessage_text"] = imessage_text_override.replace("\\n", "\n")

    timeout_override = _get_optional_int_env("IMESSAGE_DELIVERY_CHECK_TIMEOUT_SECONDS")
    if timeout_override is not None:
        if timeout_override <= 0:
            raise ValueError("IMESSAGE_DELIVERY_CHECK_TIMEOUT_SECONDS 必须大于 0")
        overrides["imessage_delivery_check_timeout_seconds"] = timeout_override

    llm_rewrite_enabled_override = _get_optional_bool_env("IMESSAGE_LLM_REWRITE_ENABLED")
    if llm_rewrite_enabled_override is not None:
        overrides["imessage_llm_rewrite_enabled"] = llm_rewrite_enabled_override

    openai_base_url_override = _get_optional_env("OPENAI_BASE_URL")
    if openai_base_url_override:
        overrides["openai_base_url"] = openai_base_url_override

    openai_api_key_override = _get_optional_env("OPENAI_API_KEY")
    if openai_api_key_override:
        overrides["openai_api_key"] = openai_api_key_override

    openai_model_override = _get_optional_env("OPENAI_MODEL")
    if openai_model_override:
        overrides["openai_model"] = openai_model_override

    openai_timeout_override = _get_optional_int_env("OPENAI_TIMEOUT_SECONDS")
    if openai_timeout_override is not None:
        if openai_timeout_override <= 0:
            raise ValueError("OPENAI_TIMEOUT_SECONDS 必须大于 0")
        overrides["openai_timeout_seconds"] = openai_timeout_override

    openai_temperature_override = _get_optional_float_env("OPENAI_TEMPERATURE")
    if openai_temperature_override is not None:
        if not (0 <= openai_temperature_override <= 2):
            raise ValueError("OPENAI_TEMPERATURE 必须在 0 到 2 之间")
        overrides["openai_temperature"] = openai_temperature_override

    if not overrides:
        return APP_CONFIG
    return replace(APP_CONFIG, **overrides)


def write_json(path: str | Path, payload: object) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_runtime_config() -> dict[str, object]:
    load_dotenv()
    return {
        "web_login": _load_lingxing_web_login_config(),
        "imessage_test_usernames": _load_imessage_test_usernames(),
        "dingtalk_notify": _load_dingtalk_notify_config(),
        "app_config": _load_runtime_app_config(),
    }


def _build_order_export_range(lookback_days: int) -> tuple[str, str]:
    """
    对齐 HAR：
    - 开始时间：向前回溯 N 天的 00:00:00
    - 结束时间：当天 23:59:59
    """
    today = datetime.now()
    days_back = max(lookback_days - 1, 0)
    start_dt = today - timedelta(days=days_back)
    return (
        start_dt.strftime("%Y-%m-%d 00:00:00"),
        today.strftime("%Y-%m-%d 23:59:59"),
    )


def _build_batch_data_paths(app_config: object, *, batch_date: str | None = None) -> tuple[Path, Path, Path, Path]:
    if not batch_date:
        batch_date = datetime.now().strftime("%Y-%m-%d")
    batch_dir = Path(str(getattr(app_config, "imessage_batch_root_dir"))) / batch_date
    batch_dir.mkdir(parents=True, exist_ok=True)

    order_export_name = Path(str(getattr(app_config, "order_export_out"))).name
    orders_json_name = Path(str(getattr(app_config, "orders_out"))).name
    phones_json_name = Path(str(getattr(app_config, "phones_out"))).name

    return (
        batch_dir,
        batch_dir / order_export_name,
        batch_dir / orders_json_name,
        batch_dir / phones_json_name,
    )


def _load_recipients_from_batch_results(
    *,
    batch_root_dir: str | Path,
    batch_date: str,
    include_statuses: tuple[str, ...],
) -> tuple[list[str], str | None]:
    replay_batch_dir = Path(batch_root_dir) / batch_date
    replay_results_path = replay_batch_dir / "results.json"
    if not replay_results_path.exists():
        raise FileNotFoundError(f"未找到指定批次结果文件：{replay_results_path}")

    payload = json.loads(replay_results_path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise ValueError(f"批次结果文件格式异常：{replay_results_path}")

    target_statuses = {item.lower() for item in include_statuses}
    recipients: list[str] = []
    messages: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("delivery_status", "")).strip().lower()
        if status not in target_statuses:
            continue
        recipient = str(record.get("recipient", "")).strip()
        if not recipient:
            continue
        recipients.append(recipient)
        message = str(record.get("message", "") or "")
        if message:
            messages.append(message)

    unique_recipients = list(dict.fromkeys(recipients))
    if not unique_recipients:
        raise ValueError(
            "指定批次中未找到可重跑收件人。"
            f"批次={batch_date}，筛选状态={','.join(include_statuses)}"
        )

    unique_messages = list(dict.fromkeys(messages))
    inferred_message = unique_messages[0] if len(unique_messages) == 1 else None
    return unique_recipients, inferred_message


def _build_failure_reason_lines(send_results: list[Any], top_n: int = 5) -> list[str]:
    reasons: Counter[str] = Counter()
    for result in send_results:
        status = str(getattr(result, "status", ""))
        if status != "failed":
            continue
        error = str(getattr(result, "error", "") or "").strip()
        detail = str(getattr(result, "detail", "") or "").strip()
        reason = error or detail or "未知错误"
        reason = reason[:140]
        reasons[reason] += 1
    if not reasons:
        return []
    lines: list[str] = []
    for reason, count in reasons.most_common(top_n):
        lines.append(f"- `{count}` 次：{reason}")
    return lines


def _extract_phone_lists(send_results: list[Any]) -> tuple[list[str], list[str]]:
    """
    从发送结果中提取成功和失败的手机号列表
    返回: (成功手机号列表, 失败手机号列表)
    """
    success_phones = []
    failed_phones = []
    
    for result in send_results:
        phone = str(getattr(result, "phone", "")).strip()
        status = str(getattr(result, "status", ""))
        
        if status in ("delivered", "sent_not_confirmed"):
            success_phones.append(phone)
        elif status == "failed":
            failed_phones.append(phone)
    
    return success_phones, failed_phones


def _build_dingtalk_markdown_report(
    *,
    batch_date: str,
    batch_dir: Path,
    recipients_count: int,
    send_results: list[Any],
    dry_run: bool,
    imessage_enabled: bool,
) -> tuple[str, str]:
    status_counter = Counter(str(getattr(item, "status", "unknown")) for item in send_results)
    delivered_count = status_counter.get("delivered", 0)
    sent_not_confirmed_count = status_counter.get("sent_not_confirmed", 0)
    failed_count = status_counter.get("failed", 0)
    skipped_processed_count = status_counter.get("skipped_processed", 0)
    skipped_count = status_counter.get("skipped", 0)
    dry_run_count = status_counter.get("dry_run", 0)

    success_count = delivered_count + sent_not_confirmed_count
    processed_non_failed = success_count + skipped_processed_count + dry_run_count
    failure_rate = (failed_count / max(len(send_results), 1)) * 100

    title = f"iMessage批次结果通知 | {batch_date}"
    mode_text = "Dry Run（仅模拟）" if dry_run else ("正式发送" if imessage_enabled else "未执行发送")
    markdown_lines = [
        f"### iMessage批次执行汇总（{batch_date}）",
        "",
        f"> 执行模式：**{mode_text}**",
        f"> 批次目录：`{batch_dir}`",
        "",
        "#### 总览",
        f"- 收件目标总数：**{recipients_count}**",
        f"- 结果记录总数：**{len(send_results)}**",
        f"- 成功（已发送且非失败）：**{success_count}**",
        f"- 失败：**{failed_count}**（失败率 {failure_rate:.1f}%）",
        f"- 跳过（本批次已处理）：**{skipped_processed_count}**",
        f"- 跳过（额度/规则）：**{skipped_count}**",
        f"- Dry Run：**{dry_run_count}**",
        "",
        "#### 状态明细",
        f"- `delivered`：**{delivered_count}**",
        f"- `sent_not_confirmed`：**{sent_not_confirmed_count}**",
        f"- `failed`：**{failed_count}**",
        "",
    ]
    if processed_non_failed > 0:
        markdown_lines.extend(
            [
                "#### 执行判断",
                "- 本次策略：仅 `failed` 会参与后续重跑，"
                "`delivered` / `sent_not_confirmed` / `skipped_processed` 都视为已处理。",
                "",
            ]
        )

    failure_reason_lines = _build_failure_reason_lines(send_results)
    if failure_reason_lines:
        markdown_lines.append("#### 失败原因（Top）")
        markdown_lines.extend(failure_reason_lines)
    else:
        markdown_lines.append("#### 失败原因（Top）")
        markdown_lines.append("- 本次无失败记录")
    
    markdown_lines.append("")

    # 添加成功和失败手机号列表
    success_phones, failed_phones = _extract_phone_lists(send_results)
    
    if success_phones:
        markdown_lines.append("#### 成功手机号")
        markdown_lines.append(f"`{', '.join(success_phones)}`")
        markdown_lines.append("")
    
    if failed_phones:
        markdown_lines.append("#### 失败手机号")
        markdown_lines.append(f"`{', '.join(failed_phones)}`")
        markdown_lines.append("")

    return title, "\n".join(markdown_lines)


def _send_dingtalk_summary_if_configured(
    *,
    dingtalk_config: dict[str, str] | None,
    title: str,
    markdown_text: str,
) -> None:
    if not dingtalk_config:
        print("未配置钉钉通知，跳过发送批次汇总。")
        return

    init_dingtalk_bot(
        app_key=dingtalk_config["app_key"],
        app_secret=dingtalk_config["app_secret"],
        robot_code=dingtalk_config["robot_code"],
    )

    group_conversation_id = dingtalk_config.get("group_conversation_id", "").strip()
    notify_user_id = dingtalk_config.get("notify_user_id", "").strip()
    notify_user_name = dingtalk_config.get("notify_user_name", "").strip()

    if group_conversation_id:
        dingtalk_send_markdown_to_group(
            conversation_id=group_conversation_id,
            title=title,
            text=markdown_text,
        )
        print("钉钉批次汇总已发送到群聊。")
        return

    target = notify_user_id or notify_user_name
    if not target:
        print("钉钉通知未配置接收人/群，跳过发送。")
        return

    dingtalk_send_markdown_to_user(
        user_id_or_name=target,
        title=title,
        text=markdown_text,
        by_name=not bool(notify_user_id),
    )
    print("钉钉批次汇总已发送到单聊。")


def main() -> int:
    cli_args = _parse_cli_args()
    try:
        config = load_runtime_config()
    except Exception as exc:  # noqa: BLE001
        print(f"配置读取失败: {exc}")
        return 2

    batch_dir: Path | None = None
    recipients_count = 0
    send_results: list[Any] = []
    app_config: Any = APP_CONFIG
    dingtalk_config: dict[str, str] | None = None
    rerun_batch_date = _resolve_rerun_batch_date_from_sources(
        cli_args=cli_args,
    )
    rerun_include_statuses = _resolve_rerun_include_statuses_from_sources(
        cli_args=cli_args,
    )

    try:
        dingtalk_config = config.get("dingtalk_notify") if isinstance(config, dict) else None
        app_config = config["app_config"]  # type: ignore[assignment]
        if not isinstance(rerun_include_statuses, tuple):
            rerun_include_statuses = ("failed",)
        execution_batch_date = (
            rerun_batch_date.strip()
            if isinstance(rerun_batch_date, str) and rerun_batch_date.strip()
            else datetime.now().strftime("%Y-%m-%d")
        )
        batch_dir, order_export_path_in_batch, orders_json_path_in_batch, phones_json_path_in_batch = (
            _build_batch_data_paths(app_config, batch_date=execution_batch_date)
        )
        print(f"当前批次目录: {batch_dir.resolve()}")

        recipients: list[str]
        sending_message = app_config.imessage_text
        normalize_phone_numbers = True

        if isinstance(rerun_batch_date, str) and rerun_batch_date.strip():
            recipients, inferred_message = _load_recipients_from_batch_results(
                batch_root_dir=app_config.imessage_batch_root_dir,
                batch_date=rerun_batch_date,
                include_statuses=rerun_include_statuses,
            )
            print(
                "检测到重跑批次参数，进入指定批次重跑模式："
                f" {rerun_batch_date}（筛选状态：{','.join(rerun_include_statuses)}）"
            )
            print(f"指定批次可重跑收件人数: {len(recipients)}")
            if inferred_message:
                sending_message = inferred_message
                print("重跑模式自动复用该批次记录中的消息文案。")
            else:
                print("重跑记录存在多种消息文案，改用当前配置的 IMESSAGE_TEXT。")
        else:
            web_login = config.get("web_login")
            if not isinstance(web_login, dict):
                print(
                    "未检测到网页登录配置，请在 .env 中同时配置 "
                    "LINGXING_WEB_ACCOUNT 和 LINGXING_WEB_PASSWORD"
                )
                return 2

            web_session = create_lingxing_session_from_login(
                account=str(web_login["account"]),
                password=str(web_login["password"]),
                debug=app_config.print_request_debug,
            )
            print(
                "领星网页登录成功，已获取会话Cookie："
                f" {sorted(cookie.name for cookie in web_session.cookies)}"
            )

            switch_lingxing_to_multi_platform(
                web_session,
                debug=app_config.print_request_debug,
            )
            print("领星登录环境切换成功")

            start_time, end_time = _build_order_export_range(app_config.lookback_days)
            export_path = export_and_download_order_management_report(
                session=web_session,
                start_time=start_time,
                end_time=end_time,
                save_path=str(order_export_path_in_batch),
                debug=app_config.print_request_debug,
            )
            print(f"订单管理导出成功: {Path(export_path).resolve()}")

            orders = parse_order_management_export_file(export_path)
            phones = extract_phone_numbers_from_order_records(orders)
            write_json(orders_json_path_in_batch, orders)
            write_json(phones_json_path_in_batch, phones)

            print(f"订单解析完成: {len(orders)} 条")
            print(f"提取到不重复手机号: {len(phones)}")
            print(f"订单JSON已保存至: {orders_json_path_in_batch.resolve()}")
            print(f"手机号JSON已保存至: {phones_json_path_in_batch.resolve()}")

            imessage_test_usernames = config.get("imessage_test_usernames")
            test_usernames = (
                imessage_test_usernames
                if isinstance(imessage_test_usernames, list)
                else []
            )
            recipients = test_usernames if test_usernames else phones
            normalize_phone_numbers = not bool(test_usernames)
            if test_usernames:
                print(
                    "检测到 IMESSAGE_TEST_USERNAMES，进入测试用户名发送模式，"
                    f"将按指定列表发送（{len(test_usernames)}个目标）"
                )

        if not app_config.imessage_send_enabled and not app_config.imessage_dry_run:
            return 0

        recipients_count = len(recipients)

        send_results = send_imessages_with_risk_control(
            recipients,
            message=sending_message,
            dry_run=app_config.imessage_dry_run,
            max_send_count=app_config.imessage_max_send_count,
            rules=app_config.imessage_risk_control,
            state_path=app_config.imessage_state_path,
            normalize_phone_numbers=normalize_phone_numbers,
            batch_root_dir=app_config.imessage_batch_root_dir,
            batch_date=execution_batch_date,
            delivery_check_timeout_seconds=app_config.imessage_delivery_check_timeout_seconds,
            delivery_check_interval_seconds=app_config.imessage_delivery_check_interval_seconds,
            delivery_check_lookback_seconds=app_config.imessage_delivery_check_lookback_seconds,
            llm_rewrite_enabled=app_config.imessage_llm_rewrite_enabled,
            openai_base_url=app_config.openai_base_url,
            openai_api_key=app_config.openai_api_key,
            openai_model=app_config.openai_model,
            openai_timeout_seconds=app_config.openai_timeout_seconds,
            openai_temperature=app_config.openai_temperature,
        )
        for result in send_results:
            if result.error:
                print(f"[{result.status}] {result.phone} - {result.detail} (error={result.error})")
            else:
                print(f"[{result.status}] {result.phone} - {result.detail}")

        if batch_dir is not None:
            batch_date = batch_dir.name
            title, markdown_text = _build_dingtalk_markdown_report(
                batch_date=batch_date,
                batch_dir=batch_dir,
                recipients_count=recipients_count,
                send_results=send_results,
                dry_run=app_config.imessage_dry_run,
                imessage_enabled=app_config.imessage_send_enabled,
            )
            try:
                _send_dingtalk_summary_if_configured(
                    dingtalk_config=dingtalk_config,
                    title=title,
                    markdown_text=markdown_text,
                )
            except Exception as notify_exc:  # noqa: BLE001
                print(f"钉钉批次汇总发送失败: {notify_exc}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"领星流程执行失败: {exc}")
        if batch_dir is not None:
            try:
                fail_title = f"iMessage批次执行失败 | {batch_dir.name}"
                fail_markdown = (
                    f"### iMessage批次执行失败（{batch_dir.name}）\n\n"
                    f"- 批次目录：`{batch_dir}`\n"
                    f"- 错误信息：`{exc}`\n"
                    "- 请检查领星登录、导出链路与iMessage发送环境。"
                )
                _send_dingtalk_summary_if_configured(
                    dingtalk_config=dingtalk_config,
                    title=fail_title,
                    markdown_text=fail_markdown,
                )
            except Exception as notify_exc:  # noqa: BLE001
                print(f"钉钉失败通知发送失败: {notify_exc}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

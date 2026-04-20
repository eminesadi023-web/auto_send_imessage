from __future__ import annotations

from dataclasses import dataclass, field

from imessage_sender import IMessageRiskControl


@dataclass(frozen=True)
class AppConfig:
    lookback_days: int = 31
    order_statuses: tuple[int, ...] = (4, 5)
    orders_out: str = "领星待审核待发货订单.json"
    phones_out: str = "领星待审核待发货手机号.json"
    order_export_out: str = "订单管理导出.xlsx"
    print_request_debug: bool = True
    imessage_text: str = "Hi"
    imessage_image_path: str = "img/send_img.png"
    imessage_llm_rewrite_enabled: bool = False
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gemini-2.0-flash"
    openai_timeout_seconds: int = 20
    openai_temperature: float = 0.9
    imessage_send_enabled: bool = True
    imessage_dry_run: bool = False
    imessage_max_send_count: int | None = None
    imessage_state_path: str = ".领星待审核待发货iMessage发送历史.json"
    imessage_batch_root_dir: str = "imessage_batches"
    imessage_delivery_check_timeout_seconds: int = 90
    imessage_delivery_check_interval_seconds: int = 3
    imessage_delivery_check_lookback_seconds: int = 600
    imessage_risk_control: IMessageRiskControl = field(default_factory=IMessageRiskControl)
    api_host: str = "127.0.0.1"
    api_port: int = 8787


APP_CONFIG = AppConfig()

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import AppConfig

log = logging.getLogger(__name__)


def main() -> int:
    config = AppConfig()
    print("🚀 开始初始化飞书多维表格字段...")

    token = get_token(config)
    if not token:
        print("❌ 获取 Token 失败")
        return 1
    print("✅ 获取 Token 成功")

    fields: list[tuple[str, int]] = [
        ("标题", 1),
        ("作者", 1),
        ("发布时间", 1),
        ("点赞数", 2),
        ("收藏数", 2),
        ("评论数", 2),
        ("帖子链接", 15),
        ("搜索关键词", 1),
        ("AI匹配评分", 2),
        ("匹配理由", 1),
        ("正文摘要", 1),
        ("采集时间", 1),
    ]

    base_url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{config.get_feishu_app_token()}/tables/{config.get_feishu_table_id()}/fields"
    )

    session = requests.Session()
    for field_name, field_type in fields:
        try:
            response = session.post(
                base_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"field_name": field_name, "type": field_type},
                timeout=(10, 15),
            )
            data: dict[str, Any] = response.json()
            if data.get("code", -1) == 0:
                print(f"  ✅ 字段创建成功: {field_name}")
            else:
                print(f"  ⚠️  {field_name}: {data.get('msg', '未知错误')}（可能已存在）")
        except Exception as exc:
            print(f"  ❌ {field_name}: {exc}")

    print("\n🎉 初始化完成！")
    print("   现在可以将 settings.yaml 中的 feishu.enabled 设为 true")
    return 0


def get_token(config: AppConfig) -> str:
    try:
        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": config.get_feishu_app_id(),
                "app_secret": config.get_feishu_app_secret(),
            },
            timeout=(10, 15),
        )
        data = response.json()
        return str(data.get("tenant_access_token", "")) if data.get("code", -1) == 0 else ""
    except Exception as exc:
        log.error("获取飞书 Token 失败: %s", exc)
        return ""


if __name__ == "__main__":
    raise SystemExit(main())

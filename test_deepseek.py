import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ============================================================
# 加载项目根目录下的 .env
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"

print(f"项目目录: {ROOT_DIR}")
print(f".env 路径: {ENV_FILE}")
print(f".env 是否存在: {ENV_FILE.exists()}")

load_dotenv(ENV_FILE)


def mask_key(key: str) -> str:
    """隐藏 API Key，避免输出完整密钥。"""
    if len(key) <= 12:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com/v1",
    )
    model = os.getenv(
        "DEEPSEEK_MODEL",
        "deepseek-chat",
    )

    print()
    print("=" * 60)
    print("DeepSeek API 检查")
    print("=" * 60)

    # --------------------------------------------------------
    # 检查 Key
    # --------------------------------------------------------

    if not api_key:
        print("❌ 未发现 DEEPSEEK_API_KEY")
        print()
        print("请检查：")
        print(f"1. 文件是否存在：{ENV_FILE}")
        print("2. .env 是否包含：")
        print("   DEEPSEEK_API_KEY=你的key")
        sys.exit(1)

    print("✅ 成功从环境/.env读取 DEEPSEEK_API_KEY")
    print(f"   Key:      {mask_key(api_key)}")
    print(f"   Key长度:  {len(api_key)}")
    print(f"   Base URL: {base_url}")
    print(f"   Model:    {model}")

    # --------------------------------------------------------
    # DeepSeek API
    # --------------------------------------------------------

    endpoint = base_url.rstrip("/") + "/chat/completions"

    print()
    print(f"请求地址: {endpoint}")
    print("正在测试 DeepSeek API...")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "只回复：DeepSeek API 测试成功",
            }
        ],
        "max_tokens": 30,
        "temperature": 0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=30.0,
        )

    except httpx.TimeoutException:
        print("❌ 请求超时")
        sys.exit(1)

    except httpx.NetworkError as exc:
        print("❌ 网络错误：")
        print(exc)
        sys.exit(1)

    print()
    print(f"HTTP 状态码: {response.status_code}")

    # --------------------------------------------------------
    # 状态码
    # --------------------------------------------------------

    if response.status_code == 401:
        print("❌ API Key 无效")
        print("DeepSeek 返回 401 Unauthorized")
        return

    if response.status_code == 403:
        print("❌ API Key 没有权限")
        return

    if response.status_code == 402:
        print("❌ 账户余额/额度可能不足")
        print(response.text[:500])
        return

    if response.status_code == 429:
        print("⚠️ 请求过多或者账户受到限流")
        print(response.text[:500])
        return

    if response.status_code != 200:
        print("❌ DeepSeek API 请求失败")
        print()
        print(response.text[:1000])
        return

    # --------------------------------------------------------
    # 正常响应
    # --------------------------------------------------------

    try:
        data = response.json()

        content = data["choices"][0]["message"]["content"]

        print()
        print("=" * 60)
        print("✅ DeepSeek API 调用成功")
        print("=" * 60)

        print()
        print("模型返回：")
        print(content)

        if "usage" in data:
            print()
            print("Token 使用：")
            print(data["usage"])

        print()
        print("结论：")
        print("✅ .env 加载正常")
        print("✅ API Key 有效")
        print("✅ DeepSeek API 可以访问")
        print("✅ 模型可以正常调用")

    except Exception as exc:
        print("⚠️ HTTP 200，但是响应解析失败：")
        print(exc)
        print()
        print(response.text[:1000])


if __name__ == "__main__":
    main()
"""帳號管理工具：新增/移除/列出帳號、重設密碼。

一律用這支腳本操作 config/auth_config.yaml，不要手動編輯該檔案——
避免手改 yaml 縮排出錯，或不小心把明文密碼存進版控。
"""
import argparse
import getpass
import secrets
import sys
from pathlib import Path

import streamlit_authenticator as stauth
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "auth_config.yaml"
VALID_ROLES = {"admin", "staff"}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        config.setdefault("credentials", {}).setdefault("usernames", {})
        return config
    return {
        "credentials": {"usernames": {}},
        "cookie": {
            "name": "riva_agent_auth_cookie",
            "key": secrets.token_hex(32),
            "expiry_days": 7,
        },
    }


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def prompt_password() -> str:
    while True:
        pw1 = getpass.getpass("設定密碼（至少 8 碼，輸入時畫面不會顯示）：")
        if len(pw1) < 8:
            print("密碼太短，至少要 8 碼，請重新輸入。")
            continue
        pw2 = getpass.getpass("再輸入一次確認：")
        if pw1 != pw2:
            print("兩次輸入不一致，請重新輸入。")
            continue
        return pw1


def cmd_add(args: argparse.Namespace) -> None:
    config = load_config()
    users = config["credentials"]["usernames"]
    if args.username in users:
        print(f"帳號 {args.username} 已存在，若要改密碼請用 reset-password。")
        sys.exit(1)
    password = prompt_password()
    users[args.username] = {
        "email": args.email,
        "name": args.name,
        "password": stauth.Hasher.hash(password),
        "roles": [args.role],
        "failed_login_attempts": 0,
        "logged_in": False,
    }
    save_config(config)
    print(f"已新增帳號 {args.username}（角色：{args.role}）。")


def cmd_remove(args: argparse.Namespace) -> None:
    config = load_config()
    users = config["credentials"]["usernames"]
    if args.username not in users:
        print(f"找不到帳號 {args.username}。")
        sys.exit(1)
    del users[args.username]
    save_config(config)
    print(f"已移除帳號 {args.username}。")


def cmd_list(args: argparse.Namespace) -> None:
    config = load_config()
    users = config["credentials"]["usernames"]
    if not users:
        print("目前沒有任何帳號。")
        return
    for username, info in users.items():
        roles = ", ".join(info.get("roles") or [])
        print(f"- {username}｜{info.get('name')}｜角色：{roles}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    config = load_config()
    users = config["credentials"]["usernames"]
    if args.username not in users:
        print(f"找不到帳號 {args.username}。")
        sys.exit(1)
    password = prompt_password()
    users[args.username]["password"] = stauth.Hasher.hash(password)
    save_config(config)
    print(f"已重設 {args.username} 的密碼。")


def main() -> None:
    parser = argparse.ArgumentParser(description="飲料店系統帳號管理工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="新增帳號")
    p_add.add_argument("--username", required=True)
    p_add.add_argument("--name", required=True, help="畫面上顯示用的名稱，不要用真實全名")
    p_add.add_argument("--email", required=True)
    p_add.add_argument("--role", required=True, choices=sorted(VALID_ROLES))
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="移除帳號")
    p_remove.add_argument("--username", required=True)
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="列出所有帳號")
    p_list.set_defaults(func=cmd_list)

    p_reset = sub.add_parser(
        "reset-password", help="管理者手動重設密碼（員工忘記密碼時使用）"
    )
    p_reset.add_argument("--username", required=True)
    p_reset.set_defaults(func=cmd_reset_password)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

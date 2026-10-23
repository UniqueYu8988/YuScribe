import argparse
import json
from pathlib import Path
import re
import sys
from . import PROTOCOL
from .common import Problem, read


def main():
    parser = argparse.ArgumentParser(description="YuScribe 原稿：把影音，变成原稿。")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "submit"):
        p = sub.add_parser(name)
        p.add_argument("url")
        p.add_argument("--page", type=int, required=True)
        p.add_argument("--allow-proxy-fake-ip", action="store_true", help="仅固定官方域名允许本机代理198.18.0.0/15合成地址，仍校验TLS")
        if name == "submit":
            p.add_argument("--language", required=True, help="明确字幕代码，如 zh-CN 或 ai-zh")
            p.add_argument("--reprocess", action="store_true")
            p.add_argument("--fallback-asr", action="store_true", help="明确允许无字幕/需登录时使用本地模型；须先配置local.json")
    p = sub.add_parser("local")
    p.add_argument("file", type=Path)
    p.add_argument("--reprocess", action="store_true")
    for name in ("status", "cancel", "_supervise", "_work", "verify"):
        p = sub.add_parser(name)
        p.add_argument("id")
    args = parser.parse_args()
    root = args.data_dir.resolve()
    try:
        from . import tasks, bilibili
        if args.command == "_supervise":
            tasks.supervise(root, args.id)
            return 0
        if args.command == "_work":
            return tasks.work(root, args.id)
        if args.command in ("inspect", "submit"):
            bilibili.ALLOW_PROXY_FAKE_IP = args.allow_proxy_fake_ip
            source = bilibili.parse_source(args.url, args.page)
            if args.command == "inspect":
                metadata = bilibili.inspect(source)
                try:
                    rows, binding = bilibili.tracks(metadata)
                    value = {"source": metadata, "tracks": [{"language": r.get("lan"), "label": r.get("lan_doc")} for r in rows], "binding": binding}
                except Problem as error:
                    value = {"source": metadata, "tracks": [], "subtitle_error": {"code": error.code, "message": error.message}}
            else:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.language):
                    raise Problem("INVALID_LANGUAGE", "字幕语言代码无效")
                request = {"kind": "bilibili", "source": source, "language": args.language,
                           "processor": "subtitle-v1", "proxy_fake_ip": args.allow_proxy_fake_ip,
                           "fallback_asr": args.fallback_asr}
                if args.fallback_asr:
                    from .transcribe import configuration, fingerprint
                    request["runtime"] = configuration(root)
                    request["model_files"] = fingerprint(request["runtime"])
                value = tasks.submit(root, request, args.reprocess)
        elif args.command == "local":
            from .transcribe import local_request
            value = tasks.submit(root, local_request(root, args.file), args.reprocess)
        elif args.command == "status":
            value = tasks.status(root, args.id)
        elif args.command == "cancel":
            value = tasks.cancel(root, args.id)
        elif args.command == "verify":
            from .artifacts import verify
            manifest = verify(root, args.id)
            value = {"valid": True, "artifact_id": manifest["artifact_id"], "source_id": manifest["source_id"]}
        print(json.dumps({"protocol": PROTOCOL, **value}, ensure_ascii=False))
        return 0
    except (Problem, OSError, ValueError, KeyError) as error:
        print(json.dumps({"protocol": PROTOCOL, "error": {
            "code": error.code if isinstance(error, Problem) else "LOCAL_IO_ERROR",
            "message": error.message if isinstance(error, Problem) else "本地配置或文件不可用；未修改学习数据"}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

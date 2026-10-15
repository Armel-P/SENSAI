import argparse
import sys

from src.core.chat import ChatService, Finished, Started
from src.core.config import load_config
from src.core.ollama_client import OllamaError, Thinking, Token
from src.core.storage import Storage

DIM, RESET = "\033[2m", "\033[0m"
HELP = ("Commands: /list  /new  /resume <id>  /history  /title <text>  /help  /quit")


def show_history(storage, conv_id):
    if conv_id is None:
        return
    for m in storage.get_path(conv_id):
        who = "you" if m["role"] == "user" else "sensai"
        flag = " [interrupted]" if m["interrupted"] else ""
        print(f"{who}> {m['content']}{flag}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sensai CLI chatbot")
    ap.add_argument("--model", help="override the model from config.yaml")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--resume", type=int, metavar="ID", help="resume a conversation")
    ap.add_argument("--last", action="store_true", help="resume the most recent conversation")
    ap.add_argument("--think", action="store_true", help="enable thinking mode and show it")
    args = ap.parse_args()

    cfg = load_config(args.config)
    storage = Storage(cfg["db_path"])
    chat = ChatService(storage, cfg)

    conv_id = args.resume
    if args.last:
        convs = storage.list_conversations()
        conv_id = convs[0]["id"] if convs else None
    if conv_id is not None and storage.get_conversation(conv_id) is None:
        print(f"[error] conversation {conv_id} not found", file=sys.stderr)
        return 1
    if conv_id is not None:
        print(f"Resumed conversation {conv_id}:\n")
        show_history(storage, conv_id)

    think = True if args.think else None
    print(f"Sensai | conversation {conv_id or 'new'} | {HELP}")

    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user:
            continue
        if user.startswith("/"):
            cmd, _, arg = user.partition(" ")
            if cmd == "/quit":
                return 0
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/list":
                for c in storage.list_conversations():
                    mark = "*" if c["id"] == conv_id else " "
                    print(f"{mark} {c['id']:>3}  {c['title']}  ({c['model']})")
            elif cmd == "/new":
                conv_id = None
                print("New conversation (saved on your first message).")
            elif cmd == "/resume":
                if arg.strip().isdigit() and storage.get_conversation(int(arg)):
                    conv_id = int(arg)
                    show_history(storage, conv_id)
                else:
                    print("Usage: /resume <id>  (see /list)")
            elif cmd == "/history":
                show_history(storage, conv_id)
            elif cmd == "/title":
                if arg.strip() and conv_id is not None:
                    storage.update_conversation(conv_id, title=arg.strip())
            else:
                print(f"Unknown command. {HELP}")
            continue

        if conv_id is None:
            conv_id = storage.create_conversation(model=args.model or cfg["model"])
        was_thinking = False
        print("sensai> ", end="", flush=True)
        try:
            for ev in chat.send(conv_id, user, model=args.model, think=think):
                if isinstance(ev, Thinking):
                    was_thinking = True
                    print(f"{DIM}{ev.text}{RESET}", end="", flush=True)
                elif isinstance(ev, Token):
                    if was_thinking:
                        print("\n", end="")
                        was_thinking = False
                    print(ev.text, end="", flush=True)
                elif isinstance(ev, Finished):
                    print(f"\n[{ev.prompt_tokens} prompt + {ev.gen_tokens} gen tokens, "
                          f"{ev.duration_s:.1f}s]")
        except KeyboardInterrupt:
            print("\n[interrupted]")
        except OllamaError as e:
            print(f"\n[error] {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

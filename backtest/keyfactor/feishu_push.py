#!/usr/bin/env python3
"""飞书群机器人 webhook 推送 (里程碑通知用)。

可靠性增强 (2026-07-16):
  - 解析响应体 `code` 字段, 非 0 视为失败 (飞书限频返回 HTTP 200 + code=11232, 旧版会误判成功)
  - 对 code=11232 (频率限制) 做指数退避重试 (1s/2s/4s, 默认最多 3 次)
  - 令牌桶限速 (共享状态文件), 主动避免触发限频
  - 每次发送落本地日志 (送达率代理监控)
  - --critical 跳过限速 (紧急告警立即发); 独立备份通道(SMS/邮件)暂无凭据, 留 hook

用法:
  python feishu_push.py "消息文本"
  python feishu_push.py "消息文本" --critical
  python feishu_push.py "消息文本" --retries 5
或 import: from feishu_push import push
"""
import sys, os, json, time, urllib.request

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/1d241455-447b-4017-b9a3-4ecb61912369"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".feishu_push_state.json")
LOG_FILE = os.path.join(HERE, ".feishu_push_log.jsonl")

# 令牌桶: 最多 1 条/4s (保守, 远低于飞书群机器人限频阈值)
TOKEN_INTERVAL = 4.0
TOKEN_CAP = 1.0
FREQ_CODE = 11232          # 飞书: 频率限制
DEFAULT_RETRIES = 3
BACKOFF = [1, 2, 4]      # 指数退避 (秒), 超出后用最后一项


def _now():
    return time.time()


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tokens": TOKEN_CAP, "last": 0.0}


def _save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def _throttle(state, critical):
    """令牌桶: 非紧急消息需等到有令牌; 紧急消息跳过限速但照常记录。"""
    if critical:
        return
    now = _now()
    elapsed = now - state.get("last", 0.0)
    tokens = min(TOKEN_CAP, state.get("tokens", TOKEN_CAP) + elapsed / TOKEN_INTERVAL)
    if tokens < 1.0:
        wait = (1.0 - tokens) * TOKEN_INTERVAL
        time.sleep(wait)
        tokens = 1.0
    state["tokens"] = tokens - 1.0
    state["last"] = _now()
    _save_state(state)


def _log(entry):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _post(text):
    body = json.dumps({"msg_type": "text", "content": {"text": text}},
                     ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", errors="replace")
    # 飞书群机器人: HTTP 200 也可能携带 code!=0 (如 11232 限频)
    try:
        j = json.loads(raw)
        code = j.get("code", 0)
        msg = j.get("msg", "")
    except Exception:
        # 非 JSON 响应: 视为成功 (兼容老接口)
        return {"ok": True, "code": 0, "msg": "non-json", "raw": raw[:200]}
    return {"ok": (code == 0), "code": code, "msg": msg, "raw": raw[:200]}


def push(text, critical=False, retries=DEFAULT_RETRIES):
    """发送文本到飞书群机器人。返回最终状态字符串。

    - 限频(code=11232)自动指数退避重试
    - 其他非 0 code 视为失败并告警
    - 全部尝试失败则返回 ERR 摘要 (不再静默误判成功)
    """
    state = _load_state()
    _throttle(state, critical)

    last = None
    for attempt in range(1 + retries):
        try:
            res = _post(text)
        except Exception as e:
            res = {"ok": False, "code": -1, "msg": f"EXC {e}", "raw": ""}
        _log({"ts": int(_now()), "attempt": attempt + 1,
              "critical": critical, "code": res["code"],
              "ok": res["ok"], "msg": res["msg"]})
        if res["ok"]:
            return f"OK code={res['code']} ({res['msg']})"
        # 限频 -> 退避后重试
        if res["code"] == FREQ_CODE and attempt < retries:
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            time.sleep(wait)
            last = res
            continue
        last = res
        # 非限频失败或非限频重试耗尽: 不再重试
        break
    return f"ERR code={last['code']} msg={last['msg']} retries={retries}"


if __name__ == "__main__":
    args = sys.argv[1:]
    critical = "--critical" in args
    if "--critical" in args:
        args.remove("--critical")
    retries = DEFAULT_RETRIES
    if "--retries" in args:
        i = args.index("--retries")
        try:
            retries = int(args[i + 1])
            args.pop(i); args.pop(i)
        except Exception:
            pass
    msg = " ".join(args) if args else "keyfactor 测试推送"
    print(push(msg, critical=critical, retries=retries))

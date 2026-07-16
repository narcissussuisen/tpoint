#!/usr/bin/env python3
"""飞书群机器人 webhook 推送 (里程碑通知用)。
用法: python feishu_push.py "消息文本"
或 import: from feishu_push import push
"""
import sys, json, urllib.request

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/1d241455-447b-4017-b9a3-4ecb61912369"

def push(text):
    body = json.dumps({"msg_type": "text", "content": {"text": text}},
                     ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        WEBHOOK, data=body,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return f"ERR {e}"

if __name__ == '__main__':
    msg = sys.argv[1] if len(sys.argv) > 1 else "keyfactor 测试推送"
    print(push(msg))

"""2026-11-12 (owner-authorized): streamed text must arrive once.

Offline: synthetic upstream SSE payloads modelled on the F-F LIVE capture; no network, no account.
"""
import json
import unittest

from services.protocol.conversation import iter_conversation_payloads


def append(text):
    return json.dumps({"p": "/message/content/parts/0", "o": "append", "v": text}, ensure_ascii=False)


def snapshot(text):
    return json.dumps({"v": {"message": {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [text]}, "recipient": "all"}}}, ensure_ascii=False)


def chan_snapshot(c, mid, text=""):
    return json.dumps({"v": {"message": {"id": mid, "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [text]}, "status": "in_progress", "recipient": "all"}}, "c": c}, ensure_ascii=False)


def chan_append(c, text):
    return json.dumps({"p": "/message/content/parts/0", "o": "append", "v": text, "c": c}, ensure_ascii=False)


def chan_patch(c, text):
    ops = [{"p": "/message/update_time", "o": "replace", "v": 1}, {"p": "/message/content/parts/0", "o": "append", "v": text}]
    return json.dumps({"p": "", "o": "patch", "v": ops, "c": c}, ensure_ascii=False)


def marker(mid, kind="user_visible_token"):
    return json.dumps({"type": "message_marker", "message_id": mid, "marker": kind})


def deltas(payloads):
    out = []
    for event in iter_conversation_payloads(iter(payloads)):
        if event.get("type") == "conversation.delta":
            out.append(event.get("delta") or "")
    return "".join(out)


class SnapshotLagTest(unittest.TestCase):
    def test_lagging_snapshot_does_not_duplicate_or_drop_text(self):
        payloads = [snapshot("【M3】\n\n私は、"), append("美紀に返事"), snapshot("【M3】\n\n私は、美紀"),
                    append("をする前に、"), append("台所の椅子へ腰を下ろしました。"), "[DONE]"]
        self.assertEqual(deltas(payloads), "【M3】\n\n私は、美紀に返事をする前に、台所の椅子へ腰を下ろしました。")

    def test_snapshot_that_extends_the_text_is_used(self):
        payloads = [snapshot("はじめ"), append("に"), snapshot("はじめに、母は言った。"), "[DONE]"]
        self.assertEqual(deltas(payloads), "はじめに、母は言った。")

    def test_plain_append_stream_unchanged(self):
        payloads = [snapshot(""), append("一"), append("二"), append("三"), "[DONE]"]
        self.assertEqual(deltas(payloads), "一二三")


class MultiChannelTest(unittest.TestCase):
    """F-F LIVE capture: a streaming parent (c=9), the visible answer (c=10) and a continuation (c=12)
    carried copies of the same text; mixing them duplicated every chunk."""

    def test_only_the_visible_channel_is_emitted(self):
        payloads = [chan_snapshot(9, "parent"), chan_snapshot(10, "visible"),
                    chan_append(9, "【M3】\n\n次の日の午後、"), marker("visible"), marker("visible", "final_channel_token"),
                    chan_patch(10, "【M3】\n\n次の日の午後、"), chan_append(9, "私は公園へ"), chan_patch(10, "私は公園へ"),
                    chan_append(9, "行きました。"), chan_patch(10, "行きました。"),
                    chan_snapshot(12, "continuation"), chan_append(12, "---LEDGER---{}"), chan_patch(10, "---LEDGER---{}"), "[DONE]"]
        self.assertEqual(deltas(payloads), "【M3】\n\n次の日の午後、私は公園へ行きました。---LEDGER---{}")

    def test_single_channel_stream_unchanged(self):
        payloads = [chan_snapshot(5, "only"), chan_append(5, "一"), chan_append(5, "二"), "[DONE]"]
        self.assertEqual(deltas(payloads), "一二")


if __name__ == "__main__":
    unittest.main()

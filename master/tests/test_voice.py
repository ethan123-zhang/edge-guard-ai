"""语音线单测：VAD 切句 / 识别链回退 / 播报门控 / 接待入口接线。

不碰真实声卡、不碰网络：采集、识别、播放都是假的，验证的是"路怎么走"。
"""
from __future__ import annotations

import copy
import json
import os
import struct
import tempfile
import threading
import unittest

from master import config as master_config
from master.voice.asr import (AsrChain, BAIDU_ASR_URL, BaiduAsr, CloudAsrError,
                              complete_prefix, normalize_text)
from master.voice.capture import FakeCapture
from master.voice.engine import VoiceEngine
from master.voice.playback import PhraseBook
from master.voice.vad import VadSegmenter

FRAME_SAMPLES = 640          # 40 ms @ 16 kHz


def frame(db: float, samples: int = FRAME_SAMPLES) -> bytes:
    """生成一段 S16_LE 单声道方波，RMS 等于给定 dBFS。"""
    amp = int(round(32768.0 * (10.0 ** (db / 20.0))))
    amp = max(1, min(32767, amp))
    return struct.pack("<%dh" % samples, *([amp] * samples))


def silence(samples: int = FRAME_SAMPLES) -> bytes:
    return b"\x00\x00" * samples


def speech_blob(speech_frames: int = 20, silence_frames: int = 20) -> bytes:
    return (b"".join(silence() for _ in range(5)) +
            b"".join(frame(-20.0) for _ in range(speech_frames)) +
            b"".join(silence() for _ in range(silence_frames)))


class FakeReception(object):
    def __init__(self, reply: str = "好的，已经帮您记下了，我会转告家里人的。"):
        self.reply = reply
        self.calls = []

    def on_utterance(self, visit_id, text, utterance_id, source="keyboard"):
        self.calls.append({"visit_id": visit_id, "text": text,
                           "utterance_id": utterance_id, "source": source})
        return {"visit_id": visit_id, "reply": self.reply, "alert": False,
                "purpose": None, "closed": False, "replayed": False}


class FakeAsr(object):
    def __init__(self, text: str = "把门打开", path: str = "offline"):
        self.text = text
        self.path = path
        self.calls = 0

    def transcribe(self, pcm: bytes):
        self.calls += 1
        return {"text": self.text, "path": self.path, "error": None}


class FakePlayer(object):
    def __init__(self):
        self.played = []

    def play(self, path: str) -> bool:
        self.played.append(path)
        return True


class FakeTransport(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, data, headers, timeout_s):
        self.calls.append({"method": method, "url": url, "data": data,
                           "headers": headers, "timeout_s": timeout_s})
        return self.responses.pop(0)


class VadTest(unittest.TestCase):
    def test_utterance_is_extracted(self):
        vad = VadSegmenter()
        events = vad.feed(speech_blob())
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["type"], "utterance")
        self.assertGreaterEqual(events[0]["duration_ms"], 800)

    def test_short_burst_is_dropped(self):
        vad = VadSegmenter()
        blob = (b"".join(silence() for _ in range(5)) +
                b"".join(frame(-20.0) for _ in range(2)) +
                b"".join(silence() for _ in range(20)))
        self.assertEqual(vad.feed(blob), [])

    def test_ambient_is_learned_not_recognized(self):
        """房间底噪 -45 dB 时不能一开机就当成说话。"""
        vad = VadSegmenter()
        noise = b"".join(frame(-45.0) for _ in range(100))
        self.assertEqual(vad.feed(noise), [])

    def test_long_speech_is_forced_out(self):
        vad = VadSegmenter()
        blob = (b"".join(silence() for _ in range(5)) +
                b"".join(frame(-20.0) for _ in range(220)))
        events = vad.feed(blob)
        self.assertGreaterEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["duration_ms"], 8000)


class AsrChainTest(unittest.TestCase):
    def test_cloud_first_uses_cloud(self):
        chain = AsrChain(cloud=FakeAsr("开门", "cloud"), offline=FakeAsr("把门打开"),
                         mode="cloud_first")
        result = chain.transcribe(b"\x00\x00" * 10)
        self.assertEqual(result["path"], "cloud")
        self.assertEqual(result["text"], "开门")

    def test_cloud_failure_falls_back_to_offline(self):
        class Boom(object):
            def transcribe(self, pcm):
                raise CloudAsrError("network down")

        offline = FakeAsr("把门打开")
        chain = AsrChain(cloud=Boom(), offline=offline, mode="cloud_first")
        result = chain.transcribe(b"\x00\x00" * 10)
        self.assertEqual(result["path"], "offline")
        self.assertEqual(result["text"], "把门打开")
        self.assertIn("network down", result.get("error") or "")
        self.assertEqual(offline.calls, 1)

    def test_cloud_empty_result_falls_back(self):
        chain = AsrChain(cloud=FakeAsr("", "cloud"), offline=FakeAsr("把门打开"),
                         mode="cloud_first")
        self.assertEqual(chain.transcribe(b"\x00\x00" * 10)["path"], "offline")

    def test_offline_mode_never_calls_cloud(self):
        cloud = FakeAsr("不该用", "cloud")
        chain = AsrChain(cloud=cloud, offline=FakeAsr("把门打开"), mode="offline")
        self.assertEqual(chain.transcribe(b"\x00\x00" * 10)["path"], "offline")
        self.assertEqual(cloud.calls, 0)

    def test_both_unavailable_returns_empty_not_raise(self):
        class Boom(object):
            def transcribe(self, pcm):
                raise CloudAsrError("x")

        chain = AsrChain(cloud=Boom(), offline=Boom(), mode="cloud_first")
        result = chain.transcribe(b"\x00\x00" * 10)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["path"], "none")

    def test_normalize_text_drops_spaces(self):
        self.assertEqual(normalize_text("我 来 送 点 菜"), "我来送点菜")


class PrefixCompletionTest(unittest.TestCase):
    """2026-11-15 现场直说：「把门打开」被 VAD 切成「把门打」，越权告警没触发。"""

    PHRASES = ["把 门 打 开", "开 门", "我 来 送 点 菜", "你 好"]

    def test_truncated_phrase_is_completed(self):
        self.assertEqual(complete_prefix("把门打", self.PHRASES), "把门打开")
        self.assertEqual(complete_prefix("把门打。", self.PHRASES), "把门打开")

    def test_long_prefix_completed(self):
        self.assertEqual(complete_prefix("把门", self.PHRASES), "把门打开")
        self.assertEqual(complete_prefix("我来送点", self.PHRASES), "我来送点菜")

    def test_exact_match_unchanged(self):
        self.assertEqual(complete_prefix("把门打开", self.PHRASES), "把门打开")
        self.assertEqual(complete_prefix("你好", self.PHRASES), "你好")

    def test_unrelated_or_too_short_not_rewritten(self):
        self.assertEqual(complete_prefix("随便说说", self.PHRASES), "随便说说")
        self.assertEqual(complete_prefix("门", self.PHRASES), "门")


class BaiduAsrTest(unittest.TestCase):
    def test_request_shape_and_result(self):
        transport = FakeTransport([
            json.dumps({"access_token": "tok", "expires_in": 3600}).encode(),
            json.dumps({"err_no": 0, "result": ["我 来 送 点 菜"]}).encode(),
        ])
        asr = BaiduAsr("api-key", "secret", transport=transport)
        pcm = b"\x01\x00" * 160
        result = asr.transcribe(pcm)
        self.assertEqual(result["text"], "我来送点菜")
        self.assertEqual(result["path"], "cloud")
        self.assertTrue(transport.calls[0]["url"].endswith("grant_type=client_credentials"
                                                          "&client_id=api-key&client_secret=secret"))
        asr_call = transport.calls[1]
        self.assertEqual(asr_call["url"], BAIDU_ASR_URL)
        payload = json.loads(asr_call["data"].decode("utf-8"))
        self.assertEqual(payload["token"], "tok")
        self.assertEqual(payload["len"], len(pcm))
        self.assertEqual(payload["format"], "pcm")

    def test_service_error_raises(self):
        transport = FakeTransport([
            json.dumps({"access_token": "tok", "expires_in": 3600}).encode(),
            json.dumps({"err_no": 3301, "err_msg": "speech quality error"}).encode(),
        ])
        asr = BaiduAsr("k", "s", transport=transport)
        with self.assertRaises(CloudAsrError):
            asr.transcribe(b"\x01\x00" * 160)


class PhraseBookTest(unittest.TestCase):
    def test_exact_match_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "reply.wav"), "wb").write(b"RIFF")
            book = PhraseBook({"好的": "reply.wav", "没有": "missing.wav"}, root=tmp)
            self.assertTrue(book.wav_for("好的").endswith("reply.wav"))
            self.assertIsNone(book.wav_for("没有"))
            self.assertIsNone(book.wav_for("没说过的"))

    def test_no_dir_means_silent(self):
        book = PhraseBook({"好的": "reply.wav"})
        self.assertIsNone(book.wav_for("好的"))


class VoiceEngineTest(unittest.TestCase):
    def make_engine(self, visit="v9", text="把门打开", reply="好的，已经帮您记下了，我会转告家里人的。",
                    capture_chunks=None):
        self.tmp = tempfile.TemporaryDirectory()
        open(os.path.join(self.tmp.name, "reply.wav"), "wb").write(b"RIFF")
        cfg = {"capture": {"rate": 16000}, "vad": {},
               "asr": {"offline": {"grammar": ["把 门 打 开", "你 好"]}},
               "playback": {"post_delay_ms": 0}}
        reception = FakeReception(reply)
        asr = FakeAsr(text)
        player = FakePlayer()
        book = PhraseBook({reply: "reply.wav"}, root=self.tmp.name)
        chunks = capture_chunks if capture_chunks is not None else [speech_blob()]
        engine = VoiceEngine(
            cfg=cfg, reception=reception, current_visit=lambda: visit,
            capture=FakeCapture(chunks, chunk_size=len(chunks[0]) or 640),
            asr=asr, player=player, phrasebook=book,
            lock=threading.RLock(), log=lambda message: None,
            sleep=lambda seconds: None)
        return engine, reception, asr, player

    def tearDown(self):
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def drain(self, engine, limit=5):
        out = None
        for _ in range(limit):
            out = engine.step()
            if out:
                return out
        return out

    def test_utterance_reaches_reception_with_voice_source(self):
        engine, reception, asr, player = self.make_engine()
        result = self.drain(engine)
        self.assertIsInstance(result, list)
        self.assertEqual(len(reception.calls), 1)
        call = reception.calls[0]
        self.assertEqual(call["visit_id"], "v9")
        self.assertEqual(call["text"], "把门打开")
        self.assertEqual(call["source"], "voice")
        self.assertTrue(call["utterance_id"].startswith("voice:"))
        self.assertEqual(player.played[-1], os.path.join(self.tmp.name, "reply.wav"))
        self.assertEqual(engine.status()["utterances"], 1)

    def test_truncated_speech_is_completed_before_reception(self):
        engine, reception, asr, player = self.make_engine(text="把门打")
        self.drain(engine)
        self.assertEqual(len(reception.calls), 1)
        self.assertEqual(reception.calls[0]["text"], "把门打开")

    def test_clipped_audio_is_dropped_without_recognizing(self):
        """现场实录里敲麦/插拔产生的满幅段不能被当成一句话送进接待。"""
        clipped = (b"".join(silence() for _ in range(5)) +
                   b"".join(frame(0.0) for _ in range(20)) +
                   b"".join(silence() for _ in range(20)))
        engine, reception, asr, player = self.make_engine(capture_chunks=[clipped])
        self.drain(engine)
        self.assertEqual(reception.calls, [])
        self.assertEqual(asr.calls, 0)
        self.assertEqual(engine.status()["dropped_noise"], 1)

    def test_short_noise_text_is_dropped(self):
        engine, reception, asr, player = self.make_engine(text="不知道")
        self.drain(engine)
        self.assertEqual(reception.calls, [])
        self.assertEqual(engine.status()["dropped_noise"], 1)

    def test_short_grammar_phrase_still_passes(self):
        engine, reception, asr, player = self.make_engine(text="你好")
        self.drain(engine)
        self.assertEqual(len(reception.calls), 1)
        self.assertEqual(reception.calls[0]["text"], "你好")

    def test_no_active_visit_drops_audio(self):
        engine, reception, asr, player = self.make_engine(visit=None)
        self.drain(engine)
        self.assertEqual(reception.calls, [])
        self.assertEqual(asr.calls, 0)
        self.assertEqual(engine.status()["dropped_no_visit"], 1)

    def test_speaking_state_blocks_recognition(self):
        engine, reception, asr, player = self.make_engine()
        engine._speaking = True
        self.assertIsNone(engine.step())
        self.assertEqual(reception.calls, [])

    def test_empty_text_only_plays_not_heard_cue(self):
        engine, reception, asr, player = self.make_engine(text="")
        engine._not_heard_file = ""
        self.drain(engine)
        self.assertEqual(reception.calls, [])
        self.assertEqual(player.played, [])


class VoiceConfigTest(unittest.TestCase):
    def base(self):
        cfg = copy.deepcopy(master_config.DEFAULTS)
        cfg["security"]["token_salt"] = "salt"
        return cfg

    def test_disabled_by_default(self):
        master_config.validate(self.base())

    def test_enabled_requires_offline_model(self):
        cfg = self.base()
        cfg["voice"]["enabled"] = True
        with self.assertRaises(master_config.ConfigError):
            master_config.validate(cfg)
        cfg["voice"]["asr"]["offline"]["model_dir"] = "/tmp/vosk-model"
        master_config.validate(cfg)

    def test_bad_mode_rejected(self):
        cfg = self.base()
        cfg["voice"]["enabled"] = True
        cfg["voice"]["asr"]["offline"]["model_dir"] = "/tmp/vosk-model"
        cfg["voice"]["asr"]["mode"] = "magic"
        with self.assertRaises(master_config.ConfigError):
            master_config.validate(cfg)


if __name__ == "__main__":
    unittest.main()
# -*- coding: utf-8 -*-
"""唤醒词检测（sherpa-onnx KeywordSpotter，中文关键词"木木"）。

2026-06-11 由 openwakeword(hey_jarvis) 替换而来：openwakeword 没有中文预训练模型，
sherpa-onnx 的 kws-zipformer-wenetspeech 纯中文模型可用 ppinyin token 定义任意中文
关键词，零训练。关键词在 voice/keywords.txt（行格式：`m ù m ù :boost #threshold @木木`，
:/# 可省略），改词/调参后重启 voice 服务生效，无需动模型。

模型目录 <root>/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
（不进 git；缺失时构造抛错并附下载命令）。macOS 上 CPU 推理即可（M4 实测单次
decode 2-3ms），不必 coreml。
"""
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = _ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KEYWORDS_FILE = Path(__file__).resolve().parent / "keywords.txt"
_DOWNLOAD_HINT = (
    "模型缺失：%s\n下载（走代理）：\n"
    "  curl -sL -x http://192.168.50.4:7890 -o /tmp/kws.tar.bz2 "
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2\n"
    "  mkdir -p %s && tar -xjf /tmp/kws.tar.bz2 -C %s"
)


class WakeDetector:
    """"木木"唤醒检测：feed 一帧返回 1.0（本帧命中）/ 0.0，阈值比较由调用方做。

    threshold 直接用作 sherpa 的 keywords_threshold（默认 0.25；调大→更难触发，
    误唤醒往 0.35-0.5 调；漏唤醒可在 keywords.txt 行内加 :1.5 提升 boost）。
    feed 返回值保持旧接口的 score 语义：命中 1.0、未命中 0.0——daemon 的
    `score > wake_threshold` 比较恒成立/恒不成立，触发判定完全由 KWS 内部完成。
    """

    def __init__(self, threshold: float, model_dir=None, keywords_file=None):
        import sherpa_onnx  # 懒加载，避免 import 即拉起 onnxruntime

        m = Path(model_dir) if model_dir else MODEL_DIR
        kw = Path(keywords_file) if keywords_file else KEYWORDS_FILE
        # 模型四件全查（半残目录的报错没有下载提示，这里统一兜住）
        required = ("tokens.txt",
                    "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                    "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                    "joiner-epoch-12-avg-2-chunk-16-left-64.onnx")
        if any(not (m / f).exists() for f in required):
            raise FileNotFoundError(_DOWNLOAD_HINT % (m, m.parent, m.parent))
        self._validate_keywords(m / "tokens.txt", kw)
        self.threshold = float(threshold)
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(m / "tokens.txt"),
            encoder=str(m / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            decoder=str(m / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(m / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            keywords_file=str(kw),
            num_threads=2,
            provider="cpu",
            keywords_score=1.0,
            keywords_threshold=self.threshold,
        )
        self._stream = self._kws.create_stream()

    def feed(self, chunk: np.ndarray) -> float:
        """喂一帧 16k 音频（int16 或 float -1..1，推荐 1280 样本=80ms）。

        必须转 float32 归一 [-1,1] 再 accept_waveform——int16 原值直接喂会静默失效。
        命中后立即 reset_stream（不 reset 则后续无法再次触发，sherpa 源码明确）。
        """
        arr = np.asarray(chunk).reshape(-1)
        if np.issubdtype(arr.dtype, np.floating):
            f32 = np.clip(arr, -1.0, 1.0).astype(np.float32)
        else:
            f32 = arr.astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, f32)
        hit = False
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            if self._kws.get_result(self._stream):
                hit = True
                self._kws.reset_stream(self._stream)
        return 1.0 if hit else 0.0

    def reset(self) -> None:
        """清解码状态（唤醒成功转入录音前调用，防扬声器回声二次触发）。"""
        self._kws.reset_stream(self._stream)

    @staticmethod
    def _validate_keywords(tokens_path: Path, keywords_path: Path) -> None:
        """构造 KeywordSpotter 前预校验 keywords 的每个 token 都在模型词表里。

        OOV token 会让 sherpa C++ 层 InitKeywords 直接 exit(-1) 杀死整个进程
        （Python 捕获不到），launchd 场景会变成 KeepAlive 崩溃循环。最常见错法
        是直接写汉字（如 `木木 @木木`）——必须是 ppinyin token（`m ù m ù @木木`）。
        """
        vocab = {line.split()[0] for line in
                 tokens_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        for lineno, line in enumerate(
                keywords_path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            for tok in line.split():
                if tok.startswith(("@", ":", "#")):
                    continue  # 行内修饰符：@原文 :boost #threshold
                if tok not in vocab:
                    raise ValueError(
                        f"{keywords_path}:{lineno} token {tok!r} 不在模型词表 "
                        f"tokens.txt 中——关键词必须写成 ppinyin token 形式，"
                        f"如 `m ù m ù @木木`（直接写汉字会让 sherpa 杀死进程）")

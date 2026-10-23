"""已知人脸库：`vision/face_db/<face_id>.npy` + `registry.json`。

设计取舍：
- **特征以 .npy 为准**（与 docs/02 §6.5 一致），registry.json 只存名字等元数据
- 一个 face_id 一个文件，删人就是删文件，不需要动数据库
- 目录本身在 .gitignore 里（人脸特征属于敏感个人信息，绝不入库）
- 纯 numpy 比对，不依赖 cv2 —— 所以这块逻辑能在任何机器上单测
"""
import json
import os
import time
from collections import namedtuple
from pathlib import Path

import numpy as np

Match = namedtuple("Match", "face_id name score")


class GalleryError(RuntimeError):
    pass


def _l2(vec):
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-6:
        raise GalleryError("特征向量长度为 0，无法入库")
    return arr / norm


class FaceGallery(object):
    def __init__(self, directory, log=None):
        self.dir = Path(str(directory))
        self.log = log or (lambda msg: None)
        self._registry = {}      # face_id -> {"name": str, "created_at": int}
        self._vectors = {}       # face_id -> np.ndarray(归一化)

    # ---------------------------------------------------------------- 读写

    def load(self):
        """从磁盘加载；缺失目录视为空库（首次运行）。坏文件跳过但**不静默**。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        registry_path = self.dir / "registry.json"
        if registry_path.exists():
            try:
                with open(str(registry_path), "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._registry = dict(data.get("faces", {}))
            except (ValueError, OSError) as exc:
                raise GalleryError("registry.json 损坏：%s（修不好就改名让它重建）" % exc)

        self._vectors = {}
        for face_id in list(self._registry):
            path = self.dir / ("%s.npy" % face_id)
            if not path.exists():
                self.log("[gallery] 警告：%s 在 registry 里但没有特征文件，已跳过" % face_id)
                continue
            try:
                self._vectors[face_id] = _l2(np.load(str(path)))
            except (ValueError, OSError) as exc:
                self.log("[gallery] 警告：%s 读取失败（%s），已跳过" % (face_id, exc))
        return self

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "registry.json.tmp"
        with open(str(tmp), "w", encoding="utf-8") as f:
            json.dump({"faces": self._registry}, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(self.dir / "registry.json"))

    # ---------------------------------------------------------------- 增删

    def add(self, face_id, name, embedding, save=True):
        vec = _l2(embedding)
        self.dir.mkdir(parents=True, exist_ok=True)
        np.save(str(self.dir / ("%s.npy" % face_id)), vec)
        self._registry[str(face_id)] = {
            "name": str(name),
            "created_at": int(time.time() * 1000),
        }
        self._vectors[str(face_id)] = vec
        if save:
            self.save()
        return vec

    def remove(self, face_id, save=True):
        face_id = str(face_id)
        existed = face_id in self._vectors
        self._vectors.pop(face_id, None)
        self._registry.pop(face_id, None)
        npy = self.dir / ("%s.npy" % face_id)
        if npy.exists():
            npy.unlink()
        if save and existed:
            self.save()
        return existed

    # ---------------------------------------------------------------- 比对

    def best(self, embedding):
        """返回最相似的 `(face_id, name, score)`；空库返回 `(None, None, 0.0)`。"""
        if embedding is None or not self._vectors:
            return (None, None, 0.0)
        vec = _l2(embedding)
        best_id, best_score = None, -1.0
        for face_id, ref in self._vectors.items():
            score = float(np.dot(ref, vec))
            if score > best_score:
                best_id, best_score = face_id, score
        name = self._registry.get(best_id, {}).get("name") if best_id else None
        return (best_id, name, best_score)

    def match(self, embedding, threshold):
        """达到阈值才算认识（docs/02 §7.1 `match_threshold`），否则返回 None。"""
        face_id, name, score = self.best(embedding)
        if face_id is None or score < float(threshold):
            return None
        return Match(face_id=face_id, name=name, score=score)

    def names(self):
        return {fid: self._registry[fid].get("name") for fid in self._registry}

    def __len__(self):
        return len(self._vectors)

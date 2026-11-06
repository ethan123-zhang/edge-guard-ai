"""TensorRT 引擎封装：模型/引擎哈希绑定 + 动态批量 + 不可用时回退 CPU。

背景（2026-11-06 板端实测 `docs/Jetson-TRT加速评估-2026-11-06.md`）：
板端 `cv2` 4.1.1 没编 CUDA，检测/SFace 走 CPU（84 ms + 219 ms/帧）；
同一 ONNX 用 TensorRT FP16 后是 11 ms + 18 ms/脸，约 8.8 倍。本模块把
「engine 怎么加载、怎么喂、坏了怎么办」收成一处，生产侧只做二选一。

约定（与关联层一样，默认不启用，接线前先过数值回归）：

1. **哈希绑定**：engine 旁边必须有 `<engine>.json`（schema `sentinel/trt-engine@1`），
   记录 engine 与源模型 sha256、TensorRT 版本、fp16、输入 profile、输出形状。
   加载时三项都核对；任何一项对不上就抛 `BackendUnavailable`，由调用方回退 CPU，
   不允许"版本漂了但还能跑"这种静默降级。
2. **动态批量**：SFace 一张脸一次前向太浪费，engine 按 `min/opt/max` 批量建 profile；
   `run()` 按实际输入数组的形状 `set_binding_shape`，输出按实际形状重新分配。
3. **只依赖 numpy**：`tensorrt` / `pycuda` 都在函数内 import，PC 上 import 本模块
   不会失败；没有 TensorRT 的机器上 `create_backend()` 返回 None。
4. **不碰 cv2**：检测解码、五点对齐仍在调用方；这里只做张量搬运。

最小用法：

    backend = create_backend(engine_path, model_path, log=log)
    if backend is None:
        ...  # 走原来的 CPU 路径
    else:
        out = backend.run({"input": blob})        # blob: (1, 3, 320, 320) float32
        out = backend.run({"data": crops})        # crops: (N, 3, 112, 112) float32
"""
import hashlib
import json
import os

import numpy as np

ENGINE_SCHEMA = "sentinel/trt-engine@1"
REQUIRED_SIDECAR_KEYS = ("schema", "engine_sha256", "model_sha256", "tensorrt", "input")


class BackendUnavailable(RuntimeError):
    """TensorRT/pycuda/engine/sidecar 任一不满足。调用方应回退 CPU。"""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_path(engine_path):
    return str(engine_path) + ".json"


def tensorrt_version():
    """返回 TensorRT 版本字符串；没有模块时抛 BackendUnavailable（PC 上就是这条）。"""
    try:
        import tensorrt
    except ImportError as exc:
        raise BackendUnavailable("没有 tensorrt 模块：%s" % exc)
    return str(getattr(tensorrt, "__version__", "unknown"))


def _load_sidecar(engine_path):
    path = sidecar_path(engine_path)
    if not os.path.exists(path):
        raise BackendUnavailable(
            "engine 缺少 sidecar：%s（先跑 build_sidecar 记录哈希与 profile）" % path
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            sidecar = json.load(fh)
    except (ValueError, OSError) as exc:
        raise BackendUnavailable("sidecar 读不了：%s：%s" % (path, exc))
    missing = [key for key in REQUIRED_SIDECAR_KEYS if key not in sidecar]
    if missing:
        raise BackendUnavailable("sidecar 缺字段：%s" % ", ".join(missing))
    if sidecar["schema"] != ENGINE_SCHEMA:
        raise BackendUnavailable(
            "sidecar schema 不对：%r（期望 %r）" % (sidecar["schema"], ENGINE_SCHEMA)
        )
    return sidecar


def build_sidecar(engine_path, model_path, input_name, profile, fp16,
                  outputs=None, trt_version=None, engine_profile=None):
    """生成并写入 sidecar，返回内容。`profile` = {"min": [...], "opt": [...], "max": [...]}。"""
    engine_path = str(engine_path)
    if not os.path.exists(engine_path):
        raise BackendUnavailable("engine 不存在：%s" % engine_path)
    if not os.path.exists(model_path):
        raise BackendUnavailable("源模型不存在：%s" % model_path)
    sidecar = {
        "schema": ENGINE_SCHEMA,
        "engine": os.path.basename(engine_path),
        "engine_sha256": sha256_file(engine_path),
        "model": os.path.basename(model_path),
        "model_sha256": sha256_file(model_path),
        "tensorrt": trt_version or tensorrt_version(),
        "fp16": bool(fp16),
        "input": {"name": input_name, "profile": profile},
        "outputs": list(outputs or []),
    }
    if engine_profile:
        sidecar["engine_profile"] = engine_profile
    with open(sidecar_path(engine_path), "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, ensure_ascii=False, indent=1)
    return sidecar


def verify_engine(engine_path, model_path=None, expected_tensorrt=None):
    """核对 sidecar、engine 哈希、模型哈希、TensorRT 版本；通过则返回 sidecar。"""
    engine_path = str(engine_path)
    if not os.path.exists(engine_path):
        raise BackendUnavailable("engine 不存在：%s" % engine_path)
    sidecar = _load_sidecar(engine_path)
    actual_engine = sha256_file(engine_path)
    if actual_engine != sidecar["engine_sha256"]:
        raise BackendUnavailable(
            "engine 哈希不匹配（可能被替换或损坏）：%s" % os.path.basename(engine_path)
        )
    if model_path is not None:
        model_path = str(model_path)
        if not os.path.exists(model_path):
            raise BackendUnavailable("源模型不存在：%s" % model_path)
        actual_model = sha256_file(model_path)
        if actual_model != sidecar["model_sha256"]:
            raise BackendUnavailable(
                "模型哈希不匹配（engine 不是从这份模型建的）：%s" % os.path.basename(model_path)
            )
    expected = expected_tensorrt if expected_tensorrt is not None else tensorrt_version()
    if sidecar["tensorrt"] != expected:
        raise BackendUnavailable(
            "TensorRT 版本不匹配：engine 建于 %s，当前 %s；请重建 engine 或使用 CPU 路径"
            % (sidecar["tensorrt"], expected)
        )
    return sidecar


def _prod(shape):
    total = 1
    for value in shape:
        total *= int(value)
    return total


def check_profile(profile, shape):
    """静态维必须相等，动态维必须在 min/max 内。返回 None 或抛 BackendUnavailable。"""
    if profile is None:
        return
    lowest = [int(v) for v in profile.get("min", [])]
    highest = [int(v) for v in profile.get("max", [])]
    shape = [int(v) for v in shape]
    if len(lowest) == len(shape):
        for dim, low, high in zip(shape, lowest, highest):
            if dim < low or dim > high:
                raise BackendUnavailable(
                    "输入形状 %s 超出 engine profile min=%s max=%s" % (shape, lowest, highest)
                )


class TensorRTBackend(object):
    """一个 engine 的加载与推理；不可用时由 create_backend 回退。"""

    def __init__(self, engine_path, model_path=None, log=None):
        self.log = log or (lambda message: None)
        self.engine_path = str(engine_path)
        self.sidecar = verify_engine(self.engine_path, model_path)
        try:
            import tensorrt as trt
            import pycuda.driver as cuda
            import pycuda.autoinit  # noqa: F401  建好当前 CUDA context
        except ImportError as exc:
            raise BackendUnavailable("pycuda/tensorrt 导入失败：%s" % exc)
        self._trt = trt
        self._cuda = cuda
        with open(self.engine_path, "rb") as fh:
            runtime = trt.Runtime(trt.Logger(trt.Logger.WARNING))
            self.engine = runtime.deserialize_cuda_engine(fh.read())
        if self.engine is None:
            raise BackendUnavailable("engine 反序列化失败：%s" % self.engine_path)
        self.context = self.engine.create_execution_context()
        self.stream = cuda.Stream()
        self._bindings = []          # [(index, name, is_input, dtype)]
        self._static_shapes = {}
        self._buffers = {}           # index -> {"host": ndarray, "dev": DeviceAllocation, "nbytes": int}
        for index in range(self.engine.num_bindings):
            name = self.engine.get_binding_name(index)
            is_input = bool(self.engine.binding_is_input(index))
            dtype = trt.nptype(self.engine.get_binding_dtype(index))
            self._bindings.append((index, name, is_input, dtype))
            self._static_shapes[index] = tuple(int(v) for v in self.engine.get_binding_shape(index))
        self._profile = self.sidecar["input"]["profile"] if self.sidecar.get("input") else None
        self._input_name = self.sidecar["input"]["name"]

    def _ensure_buffer(self, index, dtype, nbytes):
        entry = self._buffers.get(index)
        if entry is not None and entry["nbytes"] >= nbytes:
            return entry
        host = np.empty(nbytes // np.dtype(dtype).itemsize, dtype=dtype)
        device = self._cuda.mem_alloc(host.nbytes)
        entry = {"host": host, "dev": device, "nbytes": host.nbytes}
        self._buffers[index] = entry
        return entry

    def run(self, inputs):
        """inputs: {name: ndarray}；返回 {output_name: ndarray}。"""
        if self._input_name not in inputs:
            raise BackendUnavailable("缺少输入 %r（engine 期望 %r）" % (self._input_name, self._input_name))
        for index, name, is_input, dtype in self._bindings:
            if not is_input:
                continue
            array = np.ascontiguousarray(np.asarray(inputs[name], dtype=np.float32))
            check_profile(self._profile, array.shape)
            if self._static_shapes.get(index) != tuple(array.shape):
                self.context.set_binding_shape(index, array.shape)
            entry = self._ensure_buffer(index, dtype, array.nbytes)
            entry["host"][: array.size] = array.reshape(-1)
            self._cuda.memcpy_htod_async(entry["dev"], entry["host"], stream=self.stream)

        output_shapes = {}
        for index, name, is_input, dtype in self._bindings:
            if is_input:
                continue
            shape = tuple(int(v) for v in self.context.get_binding_shape(index))
            if any(dim < 0 for dim in shape):
                raise BackendUnavailable("输出 %r 形状仍是动态值 %s" % (name, shape))
            entry = self._ensure_buffer(index, dtype, _prod(shape) * np.dtype(dtype).itemsize)
            output_shapes[index] = shape

        bindings = [int(self._buffers[index]["dev"]) for index, _, _, _ in self._bindings]
        self.context.execute_async_v2(bindings=bindings, stream_handle=self.stream.handle)
        for index, name, is_input, dtype in self._bindings:
            if not is_input:
                self._cuda.memcpy_dtoh_async(self._buffers[index]["host"], self._buffers[index]["dev"],
                                             stream=self.stream)
        self.stream.synchronize()
        out = {}
        for index, name, is_input, dtype in self._bindings:
            if not is_input:
                out[name] = self._buffers[index]["host"][: _prod(output_shapes[index])].reshape(
                    output_shapes[index])
        return out

    def describe(self):
        return "tensorrt(%s, %s)" % (os.path.basename(self.engine_path),
                                     "fp16" if self.sidecar.get("fp16") else "fp32")

    def close(self):
        self._buffers.clear()
        self.context = None


def create_backend(engine_path, model_path=None, log=None):
    """有 TensorRT 且校验通过则返回 backend，否则记一行日志并返回 None（调用方走 CPU）。"""
    log = log or (lambda message: None)
    try:
        return TensorRTBackend(engine_path, model_path)
    except BackendUnavailable as exc:
        log("[vision] TensorRT 不可用，回退 CPU：%s" % exc)
        return None